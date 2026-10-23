"""``NoopVision``: the vision backend of ``model.vision: null`` (no description)."""

from __future__ import annotations

from memspine.services.vision.base import VisionResult

__all__ = ["NoopVision"]


class NoopVision:
    """No vision available: evidence stays ``skipped`` and the context stays text-only."""

    name = "none"

    async def describe(self, data: bytes, mime: str) -> VisionResult:
        return VisionResult(text="")
