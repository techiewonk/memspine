"""Trust: consent moved out of the engine (F-12)."""

from __future__ import annotations

from typing import Any

from memspine.store.records import MemoryRecord
from memspine.trust.privacy import current_purpose, purpose_allows


def _consent_ok(eng: Any, record: MemoryRecord) -> bool:
    """#50: whether the current read's purpose may see ``record``."""
    return purpose_allows(record, current_purpose(), eng._config().consent)
