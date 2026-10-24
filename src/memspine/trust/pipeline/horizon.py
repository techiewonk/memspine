"""Trust: horizon moved out of the engine (F-12)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from memspine.core.policies.retention import RetentionPolicy
from memspine.store.records import MemoryRecord
from memspine.trust.pipeline import retention
from memspine.trust.pipeline.state import _as_options_dict


async def expire_retention(eng: Any, now: datetime | None = None) -> dict[str, object]:
    """Run the current retention planner when the feature is enabled."""
    eng._require_started()
    return await retention.expire(eng, now)


def _retention(eng: Any, record: MemoryRecord) -> RetentionPolicy:
    """The ``retention`` policy bound for ``record``'s memory type."""
    return RetentionPolicy.bind(
        _as_options_dict(eng._memory_policy(eng._config(), record.memory_type).get("retention"))
    )
