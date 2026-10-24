"""Shared helpers and task-local state for the canonical message write path.

This module is deliberately independent of ``ingest.write`` so the apply loop can
be imported without a cycle while both paths keep using the same ContextVars.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from memspine.config import constants
from memspine.ingest.index_view import PENDING_INDEX_PREFIX, plan_prefixes  # noqa: F401
from memspine.ingest.stamping import (
    ASSENT_TAG_PREFIX,
    PENDING_STAMP,  # noqa: F401
    ConversationStamper,
    PendingStamp,
)
from memspine.observability import tap
from memspine.read.pipeline import scope_guard
from memspine.store.scope import validate_namespace

_RECALL_MARKERS = (
    constants.UNTRUSTED_NOTE_MARKER,
    constants.CURRENT_STATE_MARKER,
    constants.HISTORY_MARKER,
    constants.DISPUTED_MARKER,
    constants.INSTRUCTION_FLAG_MARKER,
    constants.TIMELINE_MARKER,
    constants.STANDING_MARKER,
    constants.CLAIM_MARKER,
    constants.CARDS_MARKER,
    constants.PROFILE_MARKER,
    constants.COUNT_MARKER,
)

IDEM_TAG_PREFIX = "idem:"
INTAKE_LANDED: ContextVar[dict[str, str] | None] = ContextVar("intake_landed", default=None)


def _looks_like_recall(content: str) -> bool:
    return any(marker in content for marker in _RECALL_MARKERS)


def _reply_target(turn: Mapping[str, Any], index: int, written: Mapping[int, str]) -> str | None:
    """Resolve a write-door reply to a record id, or skip a non-written parent."""
    raw: object = turn.get("reply_to")
    if raw is None:
        return None
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw < index:
        return written.get(raw)
    raise ValueError(
        f"messages[{index}]['reply_to'] must be a record id or the index of an earlier message"
    )


def _speaker_of(turn: Mapping[str, Any]) -> str:
    return str(turn.get("speaker") or turn.get("name") or turn.get("role") or "")


def _turn_content(turn: Mapping[str, Any]) -> str:
    content = turn["content"]
    caption = turn.get("caption")
    return f"{content} [image: {caption}]".strip() if caption else content


def _index_view_prefixes(
    eng: Any, messages: Sequence[Mapping[str, Any]], valid_from: datetime | None
) -> list[str]:
    """R14-INDEXVIEW: one index prefix per turn (empty when the feature is off)."""
    wcfg = eng._config().write
    if not wcfg.index_view:
        return []

    def stamp_of(turn: Mapping[str, Any]) -> datetime | None:
        value = turn.get("timestamp")
        if isinstance(value, datetime):
            return value
        if value is not None:
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                return valid_from
        return valid_from

    prefixes = plan_prefixes(
        [t if isinstance(t, Mapping) else {} for t in messages],
        speaker_of=_speaker_of,
        stamp_of=stamp_of,
        content_of=_turn_content,
        max_extra_tokens=wcfg.index_view_max_extra_tokens,
        elliptical_threshold=wcfg.index_view_elliptical_threshold,
        elliptical_max_words=wcfg.index_view_elliptical_max_words,
        carry=wcfg.index_view_carry,
    )
    tap.event(
        "write.index_view.plan",
        turns=len(prefixes),
        prefixed=sum(1 for p in prefixes if p),
        extra_tokens=sum(len(p.split()) for p in prefixes),
    )
    return prefixes


def _stamp_turn(
    eng: Any,
    conv: ConversationStamper,
    index: int,
    turn: Mapping[str, Any],
    role: str,
    content: str,
    session_tags_on: bool,
) -> PendingStamp:
    pending = conv.turn(index, turn, content, recorded_at=datetime.now(UTC))
    st = pending.stamped
    extra: list[str] = []
    verdict, proposal_id = conv.assent_for(role, content)
    if verdict is not None:
        if tap.enabled():
            tap.event(
                "write.stamp.assent",
                turn=index,
                kind=verdict.kind,
                binds=verdict.binds,
                matched=list(verdict.matched),
                reasons=list(verdict.reasons),
                proposal=proposal_id,
            )
        if verdict.confirms and proposal_id is not None:
            extra.append(f"{ASSENT_TAG_PREFIX}{proposal_id}")
    if tap.enabled():
        tap.event(
            "write.stamp.turn",
            turn=index,
            role=role,
            src_class=st.src_class,
            spk=st.spk,
            addr=st.addr,
            participants=len(st.participants),
            source_tz=st.source_tz,
            tz_unknown=st.tz_unknown,
            precision=st.precision,
            dated=st.source_at is not None,
            flags=list(st.flags),
        )
    return PendingStamp(st, tuple(extra))


async def _participants_mode(eng: Any, namespace: str) -> str:
    configured = str(eng._config().write.participants)
    if configured != "off" or not scope_guard.viewer_required(eng):
        return configured
    if await scope_guard.is_multi_party(eng, validate_namespace(namespace)):
        return "session"
    return configured
