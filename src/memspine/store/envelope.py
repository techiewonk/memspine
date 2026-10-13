# ruff: noqa: E501
"""Mapping between the stored MemoryRecord and the durable AssertionEnvelope (F-02a, M-06).

``to_envelope`` is lossless: whatever the envelope cannot express is carried in ``extras`` as one JSON
string (a string, not a dict: ``extras`` may not hold reserved key names at any depth), and ``from_envelope``
restores the record from it, letting the envelope's own fields win where both exist. Without that string
(an envelope written by the new write path later) ``from_envelope`` builds a record from the envelope alone.

Also registers the ``memory_record`` kind at schema version 1 with the upcaster registry. Records before
F-02a carry no ``schema_version``; :func:`upcast_record_payload` stamps them v1 (the envelope fields are
derived when absent, so v0 to v1 changes no value) and runs the registered chain, so a later v2 only adds an
``@upcaster("memory_record", 1)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import orjson

from memspine.schemas.durable.assertion import AssertionEnvelope, AssertionStatus, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import Fingerprint, OwnerScope, Provenance
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey, occurrence_id, write_id
from memspine.schemas.durable.temporal import TimeAxes
from memspine.schemas.versioning import declare_kind, upcast
from memspine.store.records import MemoryRecord, RecordStatus

__all__ = [
    "RECORD_KIND",
    "RECORD_SCHEMA_VERSION",
    "from_envelope",
    "to_envelope",
    "upcast_record_payload",
]

RECORD_KIND = "memory_record"
RECORD_SCHEMA_VERSION = 1
declare_kind(RECORD_KIND, RECORD_SCHEMA_VERSION)

#: Key of the lossless record copy inside ``AssertionEnvelope.extras``.
_EXTRAS_KEY = "legacy_record_json"

_STATUS_OUT: dict[RecordStatus, AssertionStatus] = {
    RecordStatus.ACTIVATED: "active",
    RecordStatus.RESOLVING: "contested",
    RecordStatus.QUARANTINED: "hidden",
    RecordStatus.ARCHIVED: "superseded",
    RecordStatus.DELETED: "hidden",
}
_STATUS_IN: dict[str, RecordStatus] = {
    "active": RecordStatus.ACTIVATED,
    "tentative": RecordStatus.ACTIVATED,
    "contested": RecordStatus.RESOLVING,
    "superseded": RecordStatus.ARCHIVED,
    "hidden": RecordStatus.QUARANTINED,
}


def upcast_record_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Upcast a stored record payload to the latest ``memory_record`` version (pre-F-02a: v1 by stamping)."""
    stamped = dict(payload)
    stamped.setdefault("schema_version", RECORD_SCHEMA_VERSION)
    return upcast(RECORD_KIND, stamped)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def to_envelope(record: MemoryRecord) -> AssertionEnvelope:
    """The durable assertion envelope for ``record``."""
    ns = record.namespace
    scope = OwnerScope(tenant=ns.split("/", 1)[0], owner=ns)
    src = record.source
    conversation = src.message_id or record.record_id
    recorded = _aware(record.recorded_at)
    key = AssertionKey(
        src=record.entity or record.record_id,
        rel=record.attribute or "states",
        kind=record.type_tag or record.memory_type,
    )
    parents = list(src.parents)
    derived_with_parents = record.provenance_class == "derived" and bool(parents)
    evidence: list[Evidence] = []
    if not derived_with_parents:
        evidence.append(
            Evidence(
                role="support",
                turn=TurnRef(source_id=conversation, source_version=1, seg_index=0),
                source_family=f"{record.provenance_class}:{record.axes.speaker or src.role}",
            )
        )
    subject = record.entity or (record.axes.subject[0] if record.axes.subject else None)
    prompt_hash = record.fingerprint.prompt_hash
    model_id = record.fingerprint.model_id
    derivation = (
        Fingerprint(
            prompt_hash=prompt_hash,
            schema_v=str(record.schema_version),
            model_id=model_id,
            quant="",
            decoding="",
            input_blocks=record.fingerprint.derivation or "",
            runtime_libs="",
        )
        if prompt_hash and model_id
        else None
    )
    return AssertionEnvelope(
        write_id=write_id(scope, conversation, recorded, record.content),
        assertion_key=key,
        occurrence_id=occurrence_id(conversation, parents or [record.record_id], key),
        record_id=record.record_id,
        scope=scope,
        provenance=Provenance(session_id=src.message_id, principal=src.principal),
        text=record.content,
        kind=record.type_tag or record.memory_type,
        subject=(
            Mention(surface=subject, mention_id=f"m_{record.record_id}", entity_id=record.entity)
            if subject
            else None
        ),
        attribute=record.attribute,
        speaker=record.axes.speaker,
        time=TimeAxes(source_time=_aware(record.valid_from), recorded_at=recorded),
        evidence=evidence,
        assessment=Assessment(
            source_trust=record.trust,
            support=Support(independent=record.corroborations),
            binding="bound",
            modality="asserted",
        ),
        src_class=record.provenance_class,
        parents=parents,
        status=_STATUS_OUT[record.status],
        prompt_hash=prompt_hash,
        derivation=derivation,
        extras={_EXTRAS_KEY: record.model_dump_json()},
    )


def from_envelope(envelope: AssertionEnvelope) -> MemoryRecord:
    """The record for ``envelope``: its lossless copy when present, else built from the envelope alone."""
    raw = envelope.extras.get(_EXTRAS_KEY)
    base: dict[str, Any] = orjson.loads(raw) if isinstance(raw, str) else {}
    source = dict(base.get("source") or {})
    if not source:
        mine = envelope.src_class in ("assistant_proposed", "derived")
        source = {"role": "assistant" if mine else "user", "channel": "envelope"}
    source["parents"] = list(envelope.parents)
    if envelope.provenance.session_id is not None:
        source["message_id"] = envelope.provenance.session_id
    if envelope.provenance.principal is not None:
        source["principal"] = envelope.provenance.principal
    base.update(
        {
            "namespace": envelope.scope.owner,
            "memory_type": base.get("memory_type") or envelope.kind,
            "content": envelope.text,
            "source": source,
            "status": _STATUS_IN[envelope.status].value,
            "trust": envelope.assessment.source_trust,
            "corroborations": envelope.assessment.support.independent,
            "provenance_class": envelope.src_class,
            "type_tag": envelope.kind,
        }
    )
    if envelope.record_id is not None:
        base["record_id"] = envelope.record_id
    if envelope.attribute is not None:
        base["attribute"] = envelope.attribute
    if envelope.subject is not None and envelope.subject.entity_id is not None:
        base["entity"] = envelope.subject.entity_id
    if envelope.time.recorded_at is not None:
        base["recorded_at"] = envelope.time.recorded_at
    if "valid_from" not in base and envelope.time.source_time is not None:
        base["valid_from"] = envelope.time.source_time
    record = MemoryRecord.model_validate(upcast_record_payload(base))
    if envelope.speaker is not None and envelope.speaker != record.axes.speaker:
        axes = record.axes.model_copy(update={"speaker": envelope.speaker})
        record = record.model_copy(update={"axes": axes})
    return record
