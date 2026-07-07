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

from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError

__all__ = ["ResolvedConfig", "flatten_dotted", "load_config", "template_dir"]

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
        merged = _deep_merge(merged, layer)
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

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer_name, layer_data in layers:
        merged = _deep_merge(merged, layer_data)
        for dotted in flatten_dotted(layer_data):
            sources[dotted] = layer_name

    merged = _resolve_secrets(merged, secret_resolver)

    try:
        config = MemspineConfig.model_validate(merged)
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError -> our error type
        raise ConfigError(f"invalid configuration: {exc}") from exc

    # Anything not set by an explicit layer came from schema defaults.
    for dotted in flatten_dotted(config.model_dump(mode="json")):
        sources.setdefault(dotted, "default")
    return ResolvedConfig(config=config, sources=sources)
