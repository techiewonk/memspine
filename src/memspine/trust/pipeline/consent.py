"""Trust: consent moved out of the engine (F-12)."""

from __future__ import annotations

from typing import Any

from memspine.store.records import MemoryRecord
from memspine.trust.pipeline.erasure_v2 import visible_id
from memspine.trust.privacy import current_purpose, purpose_allows


def _consent_ok(eng: Any, record: MemoryRecord) -> bool:
    """#50: whether the current read's purpose may see ``record``. Also the one gate every read
    leg ends in, so it carries the T5-04 / POL-1 erasure check: an erased or hidden id is never
    visible (a no-op until ``privacy.erasure_v2`` erased something)."""
    if not visible_id(eng, record.record_id):
        return False
    return purpose_allows(record, current_purpose(), eng._config().consent)
