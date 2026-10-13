# ruff: noqa: E501
"""Derive: shared adapters for the opt-in offline stages that read stored facts as assertions (OW-C).

``fact_envelope`` turns a stored semantic fact into the library input of ``derive/patterns.py`` and
``derive/standing.py`` (never stored). ``archive_records`` retires a derived record the way the list-card
step does (a ``DECAY_TRANSITION`` event through the write door).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from memspine.core.event_date import happened_of, label_start
from memspine.derive.resolve import normalise_name
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope, Provenance
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus

__all__ = [
    "DERIVED_CHANNELS",
    "OBSERVATION_CHANNEL",
    "STANDING_CHANNEL",
    "archive_records",
    "fact_envelope",
    "is_input_fact",
    "short_hash",
    "split_statement",
    "tag_value",
]

OBSERVATION_CHANNEL = "observation"
STANDING_CHANNEL = "standing"

#: Channels whose records are derived views, never inputs of a later derivation (no feedback loop).
DERIVED_CHANNELS = frozenset(
    {
        OBSERVATION_CHANNEL,
        STANDING_CHANNEL,
        "list_card",
        "consolidation",
        "reflection",
        "anticipation",
        "entity_summary",
    }
)

_PREFERENCE_WORDS = {
    "preference": "preference",
    "preferences": "preference",
    "prefers": "preference",
    "favorite": "preference",
    "favourite": "preference",
    "likes": "like",
    "like": "like",
    "loves": "like",
    "dislikes": "dislike",
    "dislike": "dislike",
    "hates": "dislike",
}


def short_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def is_input_fact(r: MemoryRecord) -> bool:
    """A stored fact the offline libraries may read: semantic, keyed by an entity, not a derived view."""
    return (
        r.memory_type == "semantic"
        and bool(r.entity)
        and not r.quarantined
        and not r.instruction_flag
        and r.source.channel not in DERIVED_CHANNELS
        and "anticipatory_cue" not in r.tags
    )


def split_statement(record: MemoryRecord) -> tuple[str, str]:
    """(attribute, value) of a stored fact. A mined fact reads ``"<entity> <attribute>: <value>"``; an
    event keeps no attribute field, so the attribute is the text between the entity and the colon."""
    text = " ".join(record.content.split())
    head, sep, tail = text.partition(":")
    value = tail.strip() if sep else text
    attribute = record.attribute or ""
    if not attribute and sep:
        entity = record.entity or ""
        rest = head[len(entity) :] if head.lower().startswith(entity.lower()) else head
        attribute = rest.strip()
    return attribute, value


def _session_of(record: MemoryRecord) -> str | None:
    for tag in record.tags:
        if tag.startswith("mined:"):
            return tag[len("mined:") :] or None
    return record.group_id


def _src_class(record: MemoryRecord) -> str:
    if record.source.channel in ("tool", "document", "external"):
        return record.source.channel
    if record.source.role == "assistant" and "atomic_fact" not in record.tags:
        return "assistant_proposed"
    # a mined fact is the user's statement unless it was mined only from assistant turns
    return "assistant_proposed" if "assistant_claim" in record.tags else "user_stated"


def fact_envelope(record: MemoryRecord) -> AssertionEnvelope | None:
    """The record as the libraries read it, or ``None`` when it has no usable attribute or text.

    Event time is the ``happened:`` day when the fact carries one, else ``valid_from``. The attribute is the
    mined attribute (or the event's head words); a preference attribute maps to kind ``preference`` / ``like``
    / ``dislike`` so the preference-stability miner sees it. Trust is the record's stored trust.
    """
    attribute, value = split_statement(record)
    if not attribute or not value or not record.entity:
        return None
    happened = happened_of(record)
    day = label_start(happened) if happened else None
    at = (
        datetime(day.year, day.month, day.day, tzinfo=UTC)
        if day is not None
        else _aware(record.valid_from)
    )
    start = TemporalRef(at=at, precision="instant", basis="explicit")
    end_at = record.invalid_at or record.valid_to
    end = (
        TemporalRef(at=_aware(end_at), precision="instant", basis="inferred")
        if end_at is not None
        else None
    )
    time = TimeAxes(event=Interval(start=start, end=end), recorded_at=_aware(record.recorded_at))
    if record.status is RecordStatus.ACTIVATED:
        status = "active"
    elif record.status is RecordStatus.RESOLVING:
        status = "tentative"
    elif record.status is RecordStatus.ARCHIVED and record.superseded_at is not None:
        status = "superseded"
    else:
        return None
    kind = _PREFERENCE_WORDS.get(normalise_name(attribute))
    if kind is None:
        kind = "state" if "kind:state" in record.tags else "event"
    family = record.source.principal or record.source.role or "unknown"
    return AssertionEnvelope(
        write_id=f"w:{record.record_id}",
        assertion_key=AssertionKey(src=record.entity, rel=attribute, kind="fact"),
        occurrence_id=f"o:{record.record_id}",
        record_id=record.record_id,
        scope=OwnerScope(tenant=record.namespace, owner=record.namespace),
        provenance=Provenance(session_id=_session_of(record), principal=record.source.principal),
        text=value,
        kind=kind,
        subject=Mention(surface=record.entity, mention_id=f"m:{record.record_id}"),
        attribute=attribute,
        value=value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=record.record_id, source_version=1, seg_index=0),
                source_family=family,
            )
        ],
        assessment=Assessment(
            source_trust=record.trust,
            binding="bound",
            modality="asserted",
            time_precision=time.event_precision,
            support=Support(),
        ),
        src_class=_src_class(record),  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
    )


async def archive_records(
    append_event: object, namespace: str, record_ids: Iterable[str], transition: str, reason: str
) -> int:
    """Retire derived records (status ARCHIVED, ``superseded_at`` now) through the write door."""
    count = 0
    for record_id in record_ids:
        await append_event(  # type: ignore[operator]
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": record_id,
                    "set": {
                        "status": RecordStatus.ARCHIVED.value,
                        "superseded_at": datetime.now(UTC).isoformat(),
                    },
                    "transition": transition,
                    "reason": reason,
                },
            )
        )
        count += 1
    return count


def tag_value(tags: Sequence[str], prefix: str) -> str | None:
    """The suffix of the first tag that starts with ``prefix`` (e.g. ``obs:``), or ``None``."""
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None
