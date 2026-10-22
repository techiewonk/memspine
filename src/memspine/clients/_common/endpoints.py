"""Environment-backed endpoint parsing for remote connectors (credentials never in the URL)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from memspine.exceptions import ConfigError

__all__ = ["Endpoint", "endpoint_from_env", "is_loopback", "parse_endpoint"]

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    scheme: str
    port: int
    secure: bool


def parse_endpoint(endpoint: str, *, allowed_schemes: tuple[str, ...]) -> Endpoint:
    """An absolute host URL without credentials, path, query or fragment."""
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigError("remote provider endpoint is invalid") from None
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ConfigError(
            "remote provider endpoint must be an absolute host URL without credentials or path"
        )
    secure = parsed.scheme.lower() in {"https", "grpcs"}
    return Endpoint(host, parsed.scheme.lower(), port or (443 if secure else 80), secure)


def endpoint_from_env(env_name: str, *, allowed_schemes: tuple[str, ...]) -> Endpoint:
    """:func:`parse_endpoint` over the URL in environment variable ``env_name``."""
    endpoint = os.environ.get(env_name)
    if not endpoint:
        raise ConfigError(f"remote provider needs environment variable {env_name}")
    return parse_endpoint(endpoint, allowed_schemes=allowed_schemes)


def is_loopback(host: str) -> bool:
    """Whether ``host`` names this machine (``host`` locality rather than ``cluster``)."""
    return host.lower() in _LOOPBACK
