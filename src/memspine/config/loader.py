"""Config loader: layering + ``extends:`` + per-key source tracking (D-11/D-12).

Layer order (lowest to highest precedence)::

    schema defaults -> template chain (extends:) -> user YAML -> env -> runtime kwargs

``${secret:NAME}`` references anywhere in string values resolve through the
injected resolver during the two-phase bootstrap (D-22). Every dotted key
remembers which layer set it, so ``memspine config resolve`` can annotate the
effective config line by line.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from memspine.config import constants, presets
from memspine.config.schema import MemspineConfig
from memspine.config.schema.base import ConfigInputError, validate_config_input
from memspine.exceptions import ConfigError

__all__ = [
    "ResolvedConfig",
    "default_template",
    "flatten_dotted",
    "load_config",
    "template_dir",
]

_SECRET_REF = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")
_ENV_PREFIX = "MEMSPINE_"

SecretResolver = Callable[[str], str | None]


def template_dir() -> Path:
    return Path(__file__).parent / "templates"


@dataclass
class ResolvedConfig:
    """The effective config plus per-key provenance (layer name per dotted path)."""

    config: MemspineConfig
    sources: dict[str, str] = field(default_factory=dict)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _provider_settings_resets(
    base: Mapping[str, Any], overlay: Mapping[str, Any], prefix: str = ""
) -> set[str]:
    """Return settings paths whose selected provider changes in this layer.

    Provider settings belong to the selected descriptor. Carrying settings
    from a different provider across layers would feed unrelated constructor
    options to strict validation. Same-provider layers retain ordinary deep
    merge behavior.
    """
    # Storage (ADR-081) and model (ADR-082) slots all use ``{provider, settings}``; a list of
    # slots (``model.rerank.chain``, escalation targets) is replaced whole by a layer.
    reset: set[str] = set()
    if "provider" in overlay and overlay.get("provider") != base.get("provider"):
        reset.add(f"{prefix}settings" if prefix else "settings")

    for key, value in overlay.items():
        base_value = base.get(key)
        if isinstance(value, Mapping) and isinstance(base_value, Mapping):
            reset.update(_provider_settings_resets(base_value, value, f"{prefix}{key}."))
    return reset


def _merge_layer(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep merge a config layer while isolating settings on provider changes."""
    resets = _provider_settings_resets(base, overlay)
    if not resets:
        return _deep_merge(base, overlay)
    adjusted_base = _deepcopy_mapping(base)
    for path in resets:
        node = adjusted_base
        parts = path.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                break
            node = child
        else:
            node[parts[-1]] = {}
    return _deep_merge(adjusted_base, overlay)


def _deepcopy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _deepcopy_value(item) for key, item in value.items()}


def _deepcopy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _deepcopy_mapping(value)
    if isinstance(value, list):
        return [_deepcopy_value(item) for item in value]
    return value


def flatten_dotted(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested dict -> {"a.b.c": leaf}. The one flattening used for source
    tracking AND ``config resolve`` display — they must never diverge."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(flatten_dotted(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"top level of {path} must be a mapping")
    return raw


def _load_template_chain(name: str, search_dirs: list[Path]) -> dict[str, Any]:
    """Resolve one template plus its ``extends:`` ancestry (cycles rejected)."""
    chain: list[dict[str, Any]] = []
    seen: list[str] = []
    current: str | None = name
    while current is not None:
        if current in seen:
            cycle = " -> ".join([*seen, current])
            raise ConfigError(f"template extends: cycle detected: {cycle}")
        seen.append(current)
        path = _find_template(current, search_dirs)
        data = _read_yaml(path)
        current = data.pop("extends", None)
        if current is not None and not isinstance(current, str):
            raise ConfigError(f"extends: must be a template name string in {path}")
        chain.append(data)
    merged: dict[str, Any] = {}
    for layer in reversed(chain):  # base first, leaf last
        # A template that changes a slot's provider (``server`` over ``base``) must not inherit
        # the parent's settings for the old provider (ADR-081).
        merged = _merge_layer(merged, layer)
    return merged


def _find_template(name: str, search_dirs: list[Path]) -> Path:
    for directory in search_dirs:
        candidate = directory / f"{name}.yaml"
        if candidate.exists():
            return candidate
    searched = ", ".join(str(d) for d in search_dirs)
    raise ConfigError(f"template {name!r} not found (searched: {searched})")


def _env_layer(env: Mapping[str, str]) -> dict[str, Any]:
    """``MEMSPINE_EVENT_LOG__MODE=rolling`` -> ``{"event_log": {"mode": "rolling"}}``.

    Only vars whose first segment matches a real config field enter the layer —
    unrelated tooling vars (MEMSPINE_HOME, MEMSPINE_DEBUG, …) must not crash a
    strict (``extra="forbid"``) schema. Values that are not valid YAML scalars
    fall back to the raw string rather than raising a parser error.
    """
    known_roots = set(MemspineConfig.model_fields)
    layer: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        dotted = key.removeprefix(_ENV_PREFIX).lower().split("__")
        if dotted[0] not in known_roots:
            continue
        try:
            value = yaml.safe_load(raw)  # scalars: "true" -> bool, "30" -> int
        except yaml.YAMLError:
            value = raw
        node = layer
        for part in dotted[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"env var {key} conflicts with a scalar config key")
        node[dotted[-1]] = value
    return layer


def _resolve_secrets(data: Any, resolver: SecretResolver | None) -> Any:
    if isinstance(data, dict):
        return {k: _resolve_secrets(v, resolver) for k, v in data.items()}
    if isinstance(data, list):
        return [_resolve_secrets(v, resolver) for v in data]
    if isinstance(data, str):

        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            value = resolver(name) if resolver else None
            if value is None:
                raise ConfigError(
                    f"unresolved secret reference ${{secret:{name}}} "
                    "(no resolver configured or secret missing)"
                )
            return value

        return _SECRET_REF.sub(_sub, data)
    return data


def default_template(
    template: str | None,
    user_config: str | Path | dict[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> str | None:
    """ADR-032: the template a caller gets.

    A named ``template`` wins. Otherwise a caller that names a ``profile`` (in the
    overrides or the user config) gets the ``base`` template, which every profile
    extends, so ``Engine(profile="simple")`` keeps meaning the simple profile and
    never picks up the assistant settings. Otherwise the shipped
    :data:`constants.DEFAULT_TEMPLATE`.
    """
    if template is not None:
        return template
    if overrides and "profile" in overrides:
        return "base"
    if isinstance(user_config, dict):
        names_profile = "profile" in user_config
    elif user_config is not None and Path(user_config).is_file():
        names_profile = "profile" in _read_yaml(Path(user_config))
    else:
        names_profile = False
    return "base" if names_profile else constants.DEFAULT_TEMPLATE


def _with_data_presets(
    layers: list[tuple[str, dict[str, Any]]], *, has_template: bool
) -> list[tuple[str, dict[str, Any]]]:
    """I25: insert the data-profile presets right after the template layer, so they sit
    above template defaults and below every explicit user / env / kwargs setting."""
    probe: dict[str, Any] = {}
    for _, data in layers:
        probe = _merge_layer(probe, data)
    spec = probe.get("data_profile", "off")
    if not isinstance(spec, str) or spec.strip().lower() == "off":
        return layers
    shape = probe.get("data_shape")
    names = presets.resolve_presets(spec, shape if isinstance(shape, dict) else None)
    inserted = [(f"preset:{name}", presets.load_preset(name)) for name in names]
    at = 1 if has_template else 0
    return [*layers[:at], *inserted, *layers[at:]]


def load_config(
    template: str | None = None,
    user_config: str | Path | dict[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
    secret_resolver: SecretResolver | None = None,
    extra_template_dirs: list[Path] | None = None,
) -> ResolvedConfig:
    """Merge all layers (D-11), resolve secrets (D-22), validate the tree."""
    search_dirs = [*(extra_template_dirs or []), template_dir()]

    layers: list[tuple[str, dict[str, Any]]] = []
    if template is not None:
        layers.append((f"template:{template}", _load_template_chain(template, search_dirs)))
    if user_config is not None:
        if isinstance(user_config, dict):
            layers.append(("user", user_config))
        else:
            path = Path(user_config)
            layers.append((f"user:{path}", _read_yaml(path)))
    if env is not None:
        layers.append(("env", _env_layer(env)))
    if overrides:
        layers.append(("kwargs", overrides))

    layers = _with_data_presets(layers, has_template=template is not None)

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer_name, layer_data in layers:
        for settings_path in _provider_settings_resets(merged, layer_data):
            prefix = f"{settings_path}."
            for dotted in tuple(sources):
                if dotted == settings_path or dotted.startswith(prefix):
                    del sources[dotted]
        merged = _merge_layer(merged, layer_data)
        for dotted in flatten_dotted(layer_data):
            sources[dotted] = layer_name

    merged = _resolve_secrets(merged, secret_resolver)

    try:
        # Check scalar/container input types before Pydantic's normal coercion.
        # YAML and environment adapters have already parsed their string forms.
        validate_config_input(merged, MemspineConfig)
        config = MemspineConfig.model_validate(merged)
    except ConfigInputError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from None
    except ConfigError:
        raise
    except ValidationError as exc:
        # Pydantic's default string includes the rejected input. Config values
        # may contain credentials, URLs, or decrypted secrets, so expose only
        # field locations and stable error codes.
        issues = []
        for error in exc.errors(include_url=False, include_input=False, include_context=False):
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            issues.append(f"{location} ({error['type']})")
        detail = "; ".join(issues) or "validation failed"
        raise ConfigError(f"invalid configuration: {detail}") from None
    except Exception as exc:  # unexpected validation failure -> our error type
        raise ConfigError(f"invalid configuration ({type(exc).__name__})") from None

    # Anything not set by an explicit layer came from schema defaults.
    for dotted in flatten_dotted(config.model_dump(mode="json")):
        sources.setdefault(dotted, "default")
    return ResolvedConfig(config=config, sources=sources)
