"""Strict raw-input validation shared by configuration models.

Pydantic intentionally coerces many Python inputs. Configuration values are
already parsed by YAML, environment, or CLI adapters, so coercion at this
boundary can hide mistakes such as ``true`` being used as an integer. This
base model rejects scalar coercion while retaining ordinary date-string,
enum-string, and list-to-tuple parsing.
"""

from __future__ import annotations

import math
import types
from collections.abc import Iterable, Mapping, MutableSequence, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = ["ConfigInputError", "ConfigModel", "validate_config_input"]


class ConfigInputError(ValueError):
    """A scalar or nested value has the wrong Python input type."""


def _validate_value(value: Any, annotation: Any, path: str) -> None:
    if annotation is Any or annotation is object:
        _reject_non_finite_any(value, path)
        return
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Annotated:
        _validate_value(value, args[0], path)
        return

    if annotation is type(None):
        if value is not None:
            raise ConfigInputError(f"{path} must be null")
        return

    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in args):
            raise ConfigInputError(f"{path} must be one of the declared literal values")
        return

    if origin in (Union, types.UnionType):
        errors: list[ConfigInputError] = []
        for option in args:
            try:
                _validate_value(value, option, path)
                return
            except ConfigInputError as exc:
                errors.append(exc)
        raise errors[0] if errors else ConfigInputError(f"{path} has an invalid value")

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        _validate_model_mapping(value, annotation, path)
        return

    if origin in (dict, Mapping) or annotation in (dict, Mapping):
        if not isinstance(value, Mapping):
            raise ConfigInputError(f"{path} must be a mapping")
        key_type, value_type = args if len(args) == 2 else (Any, Any)
        for key, item in value.items():
            key_path = f"{path}.{key}"
            _validate_value(key, key_type, f"{key_path} (key)")
            _validate_value(item, value_type, key_path)
        return

    if origin in (list, set, frozenset):
        expected = {
            list: list,
            set: set,
            frozenset: frozenset,
        }[origin]
        if not isinstance(value, expected):
            raise ConfigInputError(f"{path} must be a {origin.__name__}")
        item_type = args[0] if args else Any
        for index, item in enumerate(value):
            _validate_value(item, item_type, f"{path}[{index}]")
        return

    if origin in (Sequence, MutableSequence):
        accepted = (list, tuple) if origin is Sequence else (list,)
        if not isinstance(value, accepted):
            label = "sequence" if origin is Sequence else "mutable sequence"
            raise ConfigInputError(f"{path} must be a {label}")
        item_type = args[0] if args else Any
        for index, item in enumerate(value):
            _validate_value(item, item_type, f"{path}[{index}]")
        return

    if origin is tuple or annotation is tuple:
        if not isinstance(value, (tuple, list)):
            raise ConfigInputError(f"{path} must be a list or tuple")
        if not args:
            return
        if len(args) == 2 and args[1] is Ellipsis:
            for index, item in enumerate(value):
                _validate_value(item, args[0], f"{path}[{index}]")
            return
        if len(value) != len(args):
            raise ConfigInputError(f"{path} must contain {len(args)} items")
        for index, (item, item_type) in enumerate(zip(value, args, strict=True)):
            _validate_value(item, item_type, f"{path}[{index}]")
        return

    if annotation is bool:
        if type(value) is not bool:
            raise ConfigInputError(f"{path} must be a boolean")
        return
    if annotation is int:
        if type(value) is not int:
            raise ConfigInputError(f"{path} must be an integer")
        return
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigInputError(f"{path} must be a number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ConfigInputError(f"{path} must be finite")
        return
    if annotation is str:
        if not isinstance(value, str):
            raise ConfigInputError(f"{path} must be a string")
        return
    if annotation is date or annotation is datetime:
        # Pydantic's ISO date/time parsing is a documented config input form.
        if not isinstance(value, (str, annotation)):
            raise ConfigInputError(f"{path} must be an ISO date/time string")
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if isinstance(value, annotation):
            return
        if not any(
            type(value) is type(member.value) and value == member.value for member in annotation
        ):
            raise ConfigInputError(f"{path} must be a valid {annotation.__name__} value")
        return


def _reject_non_finite_any(value: Any, path: str) -> None:
    """Scan opaque policy/config payloads without imposing their value schema."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigInputError(f"{path} must contain only finite numbers")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}.{key}" if isinstance(key, str) else f"{path}[<key>]"
            _reject_non_finite_any(key, f"{child_path} (key)")
            _reject_non_finite_any(item, child_path)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _reject_non_finite_any(item, f"{path}[{index}]")


def _validate_model_mapping(value: Any, model_type: type[BaseModel], path: str) -> None:
    items: Iterable[tuple[str, Any]]
    if isinstance(value, model_type):
        items = ((name, getattr(value, name)) for name in model_type.model_fields)
    elif isinstance(value, Mapping):
        items = value.items()
    else:
        raise ConfigInputError(f"{path} must be a mapping")
    for name, item in items:
        field = model_type.model_fields.get(name)
        if field is not None:
            _validate_value(item, field.annotation, f"{path}.{name}" if path else str(name))


def validate_config_input(value: Any, model_type: type[BaseModel]) -> None:
    """Validate input scalar/container types without coercing or mutating values."""
    _validate_model_mapping(value, model_type, "")


class ConfigModel(BaseModel):
    """Base class for config models with strict scalar input semantics."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_coerced_scalars(cls, value: Any) -> Any:
        validate_config_input(value, cls)
        return value
