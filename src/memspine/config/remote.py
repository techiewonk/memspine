"""Environment-backed endpoint parsing shared by optional service clients."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from memspine.exceptions import ConfigError


def endpoint_from_env(
    env_name: str, *, allowed_schemes: tuple[str, ...]
) -> tuple[str, str, int, bool]:
    endpoint = os.environ.get(env_name)
    if not endpoint:
        raise ConfigError(f"remote provider needs environment variable {env_name}")
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
    default_port = 443 if secure else 80
    return host, parsed.scheme.lower(), port or default_port, secure
