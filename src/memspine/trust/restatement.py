"""Conservative validation for system-authored RM-22 independent restatements."""

from __future__ import annotations

import hashlib
from typing import Any, cast

import orjson

from memspine.schemas.durable.semantic import SemanticEnvelope
from memspine.store.records import MemoryRecord, RestatementSupportProof

_LOCAL_FIELDS = {
    "write_id",
    "record_id",
    "occurrence_id",
    "provenance",
    "known_at",
    "source_binding_hash",
    "evidence",
    "parents",
    "producer",
    "epoch",
    "erasure_generation",
    "grounding",
    "checks",
    "prompt_hash",
    "derivation",
    "graph",
    "verdict",
    "cardinality_conflict",
    "extras",
}


def _material(raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        env = SemanticEnvelope.model_validate(raw).model_dump(mode="json", exclude_none=False)
    except (TypeError, ValueError):
        return None

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in value.items()
                if key not in {"mention_id", "binding_id", "evidence"}
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    material = normalize({key: value for key, value in env.items() if key not in _LOCAL_FIELDS})
    # Source clocks describe when this copy was said/recorded, not the claim's resolved event
    # time. Preserve ``time.event`` and all other temporal interpretation/precision.
    time = material.get("time")
    if isinstance(time, dict):
        time.pop("source_time", None)
        time.pop("recorded_at", None)
    assessment = material.get("assessment")
    if isinstance(assessment, dict):
        assessment.pop("support", None)
    return cast(dict[str, Any], material)


def claim_digest(raw: dict[str, Any]) -> str | None:
    material = _material(raw)
    if material is None:
        return None
    return hashlib.sha256(orjson.dumps(material, option=orjson.OPT_SORT_KEYS)).hexdigest()


def independent_source(record: MemoryRecord) -> str | None:
    """Return the sole source cited by all non-context evidence, else fail closed."""
    raw = record.fingerprint.envelope
    if not isinstance(raw, dict):
        return None
    try:
        env = SemanticEnvelope.model_validate(raw)
    except (TypeError, ValueError):
        return None
    cited: set[str] = set()
    has_support = False
    for evidence in [
        *env.evidence,
        *(item for binding in env.role_bindings for item in binding.evidence),
    ]:
        if evidence.turn is not None:
            cited.add(evidence.turn.source_id)
        if evidence.role == "context":
            continue
        if evidence.turn is None:
            return None
        has_support |= evidence.role == "support"
    if len(cited) != 1 or not has_support:
        return None
    source_id = next(iter(cited))
    if record.source.parents and set(record.source.parents) != {source_id}:
        return None
    return source_id


def valid_proof(record: MemoryRecord) -> tuple[str, ...]:
    """Validate proof digest and per-source evidence coverage against the current envelope."""
    proof = record.fingerprint.restatement_support
    raw = record.fingerprint.envelope
    if not isinstance(proof, RestatementSupportProof) or not isinstance(raw, dict):
        return ()
    digest = claim_digest(raw)
    if digest is None or digest != proof.claim_digest:
        return ()
    try:
        env = SemanticEnvelope.model_validate(raw)
    except (TypeError, ValueError):
        return ()
    covered: set[str] = set()
    support_sources: set[str] = set()
    for evidence in [
        *env.evidence,
        *(item for binding in env.role_bindings for item in binding.evidence),
    ]:
        if evidence.role != "context" and evidence.turn is not None:
            covered.add(evidence.turn.source_id)
            if evidence.role == "support":
                support_sources.add(evidence.turn.source_id)
    sources = tuple(dict.fromkeys(proof.source_ids))
    if len(sources) < 2 or not set(sources).issubset(set(record.source.parents)):
        return ()
    if not set(sources).issubset(covered) or not set(sources).issubset(support_sources):
        return ()
    if any(
        not set(sources).issubset(
            {
                item.turn.source_id
                for item in binding.evidence
                if item.role != "context" and item.turn is not None
            }
        )
        for binding in env.role_bindings
    ):
        return ()
    return sources
