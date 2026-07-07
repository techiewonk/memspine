"""Reflective guards (M13.7): the depth cap and source-hygiene checks.

Pure functions — no I/O. The cap (``REFLECTION_DEPTH_CAP = 2``) stops
reflection-on-reflection loops: 0 = raw memory, 1 = reflection, 2 = meta,
and there is no 3 — an LLM left to reflect on its reflections drifts.
"""

from __future__ import annotations

from collections.abc import Sequence

from memspine.config.constants import REFLECTION_DEPTH_CAP
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConflictError

__all__ = ["reflection_depth_for"]


def reflection_depth_for(parents: Sequence[MemoryRecord]) -> int:
    """The depth a reflection over ``parents`` gets; raises if it is illegal.

    Refuses quarantined or deleted parents outright: reflecting on quarantined
    content would *launder* it — the reflection (an ordinary trusted-system
    write) would carry the poison into retrieval while the original stays
    locked up (E1).
    """
    if not parents:
        raise ConflictError("a reflection needs at least one source record")
    for parent in parents:
        if parent.quarantined or parent.status in (
            RecordStatus.QUARANTINED,
            RecordStatus.DELETED,
        ):
            raise ConflictError(
                f"cannot reflect on record {parent.record_id}: it is "
                f"{'quarantined' if parent.quarantined else parent.status.value} — "
                "reflection must never launder held-back content (E1)"
            )
    depth = max(parent.reflection_depth for parent in parents) + 1
    if depth > REFLECTION_DEPTH_CAP:
        raise ConflictError(
            f"reflection depth {depth} exceeds the cap ({REFLECTION_DEPTH_CAP}) — "
            "meta-reflections are terminal (M13.7)"
        )
    return depth
