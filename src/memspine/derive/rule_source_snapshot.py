"""The complete source inputs of a deterministic rule at preparation time."""

from __future__ import annotations

from memspine.store.events import fingerprint_payload
from memspine.store.records import MemoryRecord
from memspine.store.source_binding import source_binding


def rule_input_snapshot(record: MemoryRecord) -> str:
    """Pin all source fields a rule may read, beyond the generic text/message binding.

    The rule worker reads time anchors, order, tags and eligibility as well as text and
    message identity. This hash is system-authored during compilation and checked again
    under the namespace write lock immediately before the claim deposit.
    """
    return fingerprint_payload(
        {
            "binding": source_binding(record).binding_hash,
            "source": record.source.model_dump(mode="json"),
            "source_at": record.source_at.isoformat() if record.source_at is not None else None,
            "source_tz": record.source_tz,
            "source_precision": record.source_precision,
            "valid_from": record.valid_from.isoformat(),
            "valid_to": record.valid_to.isoformat() if record.valid_to is not None else None,
            "recorded_at": record.recorded_at.isoformat(),
            "tags": list(record.tags),
            "status": record.status.value,
            "quarantined": record.quarantined,
            "instruction_flag": record.instruction_flag,
        }
    )
