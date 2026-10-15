"""Derive: adapter from today's ``MemoryRecord`` to the LIB envelopes (O4-03, O4-04).

The offline libraries (``derive.reconcile``, ``derive.histories``, ``derive.integrate``) work on
:class:`~memspine.schemas.durable.assertion.AssertionEnvelope`. The engine still stores
``MemoryRecord``; this module projects a live semantic fact to an envelope (read-only, lossless for
what the libraries read) and maps library statuses back to record lifecycle deltas.

Nothing here does I/O. The record id is the envelope's ``record_id`` and ``occurrence_id``, so every
library decision maps straight back to a stored record.
"""

from __future__ import annotations

from datetime import datetime

from memspine.derive.fact_policy import FactPurpose, is_evidence_fact
from memspine.schemas.durable.assertion import AssertionEnvelope, AssertionStatus, Mention
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope, SrcClass
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes
from memspine.store.records import MemoryRecord

__all__ = ["FACT_KIND", "OWNER", "is_fact", "key_of", "to_envelope"]

OWNER = "owner"
FACT_KIND = "fact"

#: Tags that make a perspective part of the fact key (I55): another subject or scope coexists.
_KEY_TAG_PREFIXES = ("sub:", "scope:")


def is_fact(
    record: MemoryRecord,
    *,
    include_history: bool = False,
    purpose: FactPurpose = FactPurpose.ANY,
) -> bool:
    """A semantic fact the offline libraries may read: live (or, with ``include_history``, archived
    with a successor), not held, not a derived view, with some content. Thin wrapper over
    :func:`memspine.derive.fact_policy.is_evidence_fact` (the single policy, MF-10)."""
    return is_evidence_fact(record, purpose, include_history=include_history)


def key_of(record: MemoryRecord) -> tuple[str, str] | None:
    """The (entity, attribute) fact key, with perspective tags folded into the entity, or None."""
    if record.entity is None or record.attribute is None:
        return None
    suffix = "|".join(sorted(t for t in record.tags if t.startswith(_KEY_TAG_PREFIXES)))
    entity = record.entity if not suffix else f"{record.entity}|{suffix}"
    return entity, record.attribute


def _src_class(record: MemoryRecord) -> SrcClass:
    role = record.source.role
    if role == "user":
        return "user_stated"
    if role == "assistant":
        return "assistant_proposed"
    if role == "tool":
        return "tool"
    return "external"


def _ref(at: datetime) -> TemporalRef:
    return TemporalRef(at=at, precision="instant", basis="explicit")


def to_envelope(
    record: MemoryRecord, namespace: str, *, include_history: bool = False
) -> AssertionEnvelope:
    """Project ``record`` to an envelope. Keyed facts use (entity, attribute); unkeyed facts use the
    kind ``fact`` and no subject, so they only meet each other (near-duplicates)."""
    key = key_of(record)
    entity, attribute = key if key is not None else (None, None)
    rid = record.record_id
    start = _ref(record.valid_from)
    # The recorded end (valid_to, superseded_at, archived status) is a closure the write-time ladder
    # stamped. The fold recomputes it from event time, order-independently, so every record enters
    # open and "active": a closed past interval would coexist as history and never supersede.
    event = Interval(start=start)
    time = TimeAxes(
        event=event,
        source_time=record.valid_from,
        recorded_at=record.recorded_at,
    )
    status: AssertionStatus = "contested" if "disputed" in record.tags else "active"
    retract = "retract" in record.tags
    attr = attribute or FACT_KIND
    return AssertionEnvelope(
        write_id=rid,
        assertion_key=AssertionKey(src=entity or rid, rel=attr, dst="", kind=attr),
        occurrence_id=rid,
        record_id=rid,
        scope=OwnerScope(tenant=namespace, owner=OWNER),
        text=record.content,
        kind=attr,
        subject=Mention(surface=entity, mention_id=f"m-{rid}", entity_id=entity)
        if entity is not None
        else None,
        attribute=attribute,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(
                    source_id=record.source.message_id or rid, source_version=1, seg_index=0
                ),
                source_family=record.source.principal or record.source.role or "unknown",
            )
        ],
        assessment=Assessment(
            source_trust=record.trust,
            binding="bound",
            modality="negated" if retract else "asserted",
            time_precision=time.event_precision,  # type: ignore[arg-type]
        ),
        src_class=_src_class(record),
        status=status,
    )
