"""Offline settings/default discovery from each selected provider's schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, get_args
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel

from memspine.providers.contracts import Category
from memspine.providers.registry import (
    KIND_TO_CATEGORY,
    ProviderRegistryError,
    get_registry,
    validate_settings,
)

_ABSENT = object()
_SECRET_NAMES = frozenset(
    {
        "apikey",
        "password",
        "secret",
        "token",
        "accesstoken",
        "authtoken",
        "clientsecret",
        "privatekey",
        "accesskey",
        "credential",
        "credentials",
        "authorization",
    }
)


def _is_secret_name(name: str) -> bool:
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    return normalized in _SECRET_NAMES or any(
        normalized.endswith(suffix)
        for suffix in ("secret", "password", "token", "apikey", "privatekey", "accesskey")
    )


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = "<redacted>@" + netloc.rsplit("@", 1)[1]
    query = urlencode(
        [
            (key, "<redacted>" if _is_secret_name(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _safe_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if _is_secret_name(key) and item is not None else _safe_values(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_values(item) for item in value]
    if isinstance(value, str):
        return _safe_url(value)
    return value


def _safe_schema(value: Any, *, field_name: str = "") -> Any:
    if isinstance(value, list):
        return [_safe_schema(item) for item in value]
    if not isinstance(value, dict):
        return _safe_values(value)
    result = {key: _safe_schema(item) for key, item in value.items()}
    if "default" in result:
        result["default"] = _safe_values(result["default"])
    if _is_secret_name(field_name) or value.get("writeOnly") or value.get("format") == "password":
        for key in ("default", "const", "examples", "enum"):
            if key in result and result[key] is not None:
                result[key] = "<redacted>"
    if isinstance(value.get("properties"), dict):
        result["properties"] = {
            name: _safe_schema(child, field_name=name)
            for name, child in value["properties"].items()
        }
    return result


def _defaults(node: dict[str, Any], definitions: dict[str, Any], seen: frozenset[str]) -> Any:
    if "default" in node:
        return deepcopy(node["default"])
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.rsplit("/", 1)[-1]
        if name in seen or name not in definitions:
            return _ABSENT
        return _defaults(definitions[name], definitions, seen | {name})
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return _ABSENT
    defaults = {}
    for name, child in properties.items():
        if name in node.get("required", ()):
            continue
        default = _defaults(child, definitions, seen)
        if default is not _ABSENT:
            defaults[name] = default
    return defaults


def _nested_models(annotation: Any) -> tuple[type[BaseModel], ...]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    nested: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        nested.extend(_nested_models(argument))
    return tuple(nested)


def _has_default_factory(model: type[BaseModel], seen: frozenset[type[BaseModel]]) -> bool:
    if model in seen:
        return False
    nested_seen = seen | {model}
    for field in model.model_fields.values():
        if field.default_factory is not None:
            return True
        if any(
            _has_default_factory(nested, nested_seen) for nested in _nested_models(field.annotation)
        ):
            return True
    return False


def provider_settings_reference(category: Category, provider: str) -> dict[str, Any]:
    """Return schema defaults and required fields without building any provider.

    These are schema defaults, not a running Engine's settings. Model-dependent
    validation can refine them when the selected settings model is validated.
    Required fields are not invented, and default factories are never executed.
    """
    kind = next((kind for kind, value in KIND_TO_CATEGORY.items() if value == category), None)
    if kind is None:
        raise ProviderRegistryError(f"unknown provider category {category!r}")
    descriptor = get_registry(kind).get(provider)
    schema = _safe_schema(descriptor.settings_model.model_json_schema())
    defaults = _defaults(schema, schema.get("$defs", {}), frozenset())
    complete = False
    if not schema.get("required") and not _has_default_factory(
        descriptor.settings_model, frozenset()
    ):
        # Pure selected-settings validation resolves model-dependent defaults.
        # Do not execute default factories or construct a native provider.
        try:
            validated = validate_settings(descriptor, {})
        except ProviderRegistryError:
            pass  # Conditional requirements remain visible in the schema/docs.
        else:
            values = validated.model_dump(mode="json")
            defaults = _safe_values(values)
            for key, field_schema in schema.get("properties", {}).items():
                if field_schema.get("writeOnly") and defaults.get(key) is not None:
                    defaults[key] = "<redacted>"
            complete = True
    return {
        "category": category,
        "provider": descriptor.ref.name,
        "contract_version": descriptor.ref.contract_version,
        "defaults": {} if defaults is _ABSENT else defaults,
        "defaults_complete": complete,
        "required": list(schema.get("required", ())),
        "schema": schema,
    }


__all__ = ["provider_settings_reference"]
