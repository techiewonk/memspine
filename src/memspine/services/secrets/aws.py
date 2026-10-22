"""AWS Secrets Manager backend (D-22, ``[aws]``): a resolver tier for secrets.

Two lookup shapes, in order:

1. **One JSON-bundle secret** (``MEMSPINE_SECRETS_AWS_SECRET_ID``, default
   ``"memspine"``) whose ``SecretString`` is a JSON object of ``name -> value``.
   Fetched once and cached, so a whole config's worth of secrets costs one API
   call. This is the common deployment: put every memspine secret in one bundle.
2. **Per-name fallback**: ``GetSecretValue(SecretId=name)`` for a name absent
   from the bundle, so a secret stored under its own id still resolves. Misses
   are negatively cached to avoid re-hitting the API for a name that isn't there.

Synchronous by design (secrets resolve in bootstrap phase 1, before the event
loop owns anything — D-22). The boto3 client belongs to an
:class:`~memspine.clients.aws.AwsConnector` (RM-113), opened by the bootstrap; a missing
``[aws]`` extra fails there with the D-10 error.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from memspine.clients.aws import AwsSpec

__all__ = ["AwsSecrets", "secrets_manager_spec"]


def secrets_manager_spec(
    *, region_name: str | None = None, environ: Mapping[str, str] | None = None
) -> AwsSpec:
    """The connector spec of the Secrets Manager client: the region from ``AWS_REGION`` /
    ``AWS_DEFAULT_REGION`` unless given."""
    env = environ if environ is not None else os.environ
    region = region_name or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    return AwsSpec("secretsmanager", region_name=region)


class AwsSecrets:
    """Resolver over an opened Secrets Manager client (the
    :class:`~memspine.clients.aws.AwsConnector` handle, or a test stand-in)."""

    def __init__(
        self,
        *,
        client: Any,
        bundle_secret_id: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = environ if environ is not None else os.environ
        self._bundle_id = bundle_secret_id or env.get("MEMSPINE_SECRETS_AWS_SECRET_ID", "memspine")
        self._client = client
        self._bundle: dict[str, str] | None = None  # None => not yet loaded
        self._missing: set[str] = set()  # per-name negative cache

    def _load_bundle(self) -> dict[str, str]:
        if self._bundle is not None:
            return self._bundle
        self._bundle = _fetch_json_object(self._client, self._bundle_id)
        return self._bundle

    def get(self, name: str) -> str | None:
        bundle = self._load_bundle()
        if name in bundle:
            return bundle[name]
        if name in self._missing:
            return None
        value = _fetch_string(self._client, name)
        if value is None:
            self._missing.add(name)
        return value


def _fetch_string(client: Any, secret_id: str) -> str | None:
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except Exception:
        # ResourceNotFound / access denied / transient — a secrets *resolver*
        # returns "absent", never crashes bootstrap; a lower tier or a real
        # missing-config error surfaces the gap where it belongs.
        return None
    raw = response.get("SecretString")
    return str(raw) if raw is not None else None


def _fetch_json_object(client: Any, secret_id: str) -> dict[str, str]:
    raw = _fetch_string(client, secret_id)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}
