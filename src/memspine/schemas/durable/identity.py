"""Identity helpers (schema session 3). All ids are system-authored (S2-1); the model never writes them.

Five distinct identities (ER-13): write (retry) id, mention id, assertion key, occurrence id, record id.
Only the two hash-derived ones are computed here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from memspine.schemas.base import DurablePart
from memspine.schemas.durable.common import OwnerScope

__all__ = ["AssertionKey", "occurrence_id", "write_id"]


def _digest(domain: str, parts: list[Any]) -> str:
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{domain}_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


class AssertionKey(DurablePart):
    """What an assertion is about: ``(src, rel, dst, kind)`` under the envelope's owner scope (M-17).

    ``key_v`` versions the normalisation rules. The scope lives on the envelope, not here.
    """

    src: str = Field(min_length=1)
    rel: str = Field(min_length=1)
    dst: str = ""
    kind: str = Field(min_length=1)
    key_v: int = Field(default=1, ge=1)

    def digest(self) -> str:
        return _digest("ak", [self.src, self.rel, self.dst, self.kind, self.key_v])


def write_id(
    scope: OwnerScope,
    conversation_id: str,
    position: int | datetime,
    content: str,
) -> str:
    """S3-3 retry id: ``hash(scope, conversation/session id, turn index or source time, content)``.

    Client idempotency keys are deliberately not an input. The same turn sent twice collapses; the same
    words in a later session (or at a later position) are a new write.
    """
    if isinstance(position, datetime):
        if position.tzinfo is None:
            raise ValueError("a source-time position must be timezone-aware")
        pos: list[Any] = ["t", position.astimezone(UTC).isoformat()]
    else:
        pos = ["i", int(position)]
    return _digest("wr", [scope.tenant, scope.owner, conversation_id, pos, content])


def occurrence_id(
    source_event_id: str, cited_turn_ids: Iterable[str], assertion_key: AssertionKey
) -> str:
    """S3-2: ``hash(source event id, sorted cited turn ids, assertion key)``.

    Re-extracting the same turns gives the same id (idempotent); the same fact stated in a later turn is
    a new occurrence of the same assertion.
    """
    turns = sorted(set(cited_turn_ids))
    if not turns:
        raise ValueError("an occurrence needs at least one cited turn")
    return _digest("oc", [source_event_id, turns, assertion_key.digest()])
