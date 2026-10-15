"""Reserved system-field names (SESSIONS_01_02, condition 1).

The model never authors these (ER-32, S2-1). The extraction compiler (W2-06a) rejects any model output
item containing one, even though internal models allow unknown fields. This module only provides the list
and the detector.

Matching rules (decided here, owner may veto):

* exact names, compared case-insensitively (``Tenant`` and ``TENANT`` hit; camelCase is *not* split, so
  ``tenantId`` does not hit);
* the ``*_id`` pattern means: any name ending in ``_id`` (``user_id``, ``record_id``, ``_id``);
  a name that merely contains ``id`` (``identity_text``, ``valid``, ``idempotent``) does not hit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["RESERVED_ID_SUFFIX", "RESERVED_NAMES", "is_reserved", "reserved_hits"]

RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "id",
        "scope",
        "tenant",
        "user",
        "trust",
        "provenance",
        "verified",
        "status",
        "epoch",
        "generation",
        "recorded_at",
        "lineage",
        "fingerprint",
    }
)
RESERVED_ID_SUFFIX = "_id"


def is_reserved(name: str) -> bool:
    """True if ``name`` is a reserved system-field name."""
    lowered = name.lower()
    return lowered in RESERVED_NAMES or lowered.endswith(RESERVED_ID_SUFFIX)


def reserved_hits(payload: Mapping[str, Any]) -> list[str]:
    """Paths of every reserved key in ``payload``, recursing into nested dicts and lists.

    Paths look like ``items[0].user_id``; order is depth-first in insertion order, so output is stable.
    """
    hits: list[str] = []
    _walk(payload, "", hits)
    return hits


def _walk(node: Any, path: str, hits: list[str]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and is_reserved(key):
                hits.append(child)
            _walk(value, child, hits)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]", hits)
