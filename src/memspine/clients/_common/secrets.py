"""Secrets for connectors: read from named environment variables, digested for identity,
never logged, never stored on a public attribute or rendered by ``repr``."""

from __future__ import annotations

import hashlib
import os

from memspine.exceptions import ConfigError

__all__ = ["Secret", "auth_digest", "env_or_empty", "required_env"]


class Secret:
    """Holds a credential so that no ``repr``/``str``/log rendering can show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def auth_digest(*parts: str) -> str:
    """SHA-256 of the NUL-joined credential parts: distinguishes principals, reveals nothing."""
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def env_or_empty(name: str | None) -> str:
    """The value of environment variable ``name`` ("" when unset or when no name is given)."""
    return os.environ.get(name, "") if name else ""


def required_env(name: str, what: str) -> str:
    """The value of ``name``; a ``ConfigError`` naming the variable (never a value) otherwise."""
    value = os.environ.get(name)
    if value is None:
        raise ConfigError(f"{what} needs environment variable {name}")
    return value
