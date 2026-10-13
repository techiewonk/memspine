"""``derive.OnlineExtractor`` (W2-06c, W-12): the one named entry for online extraction.

Turns in, envelopes out, deposited. The whole path runs over the outbox window stage; this class
names its parts so they are documented in one place:

    1. intake       ``Engine.add`` stores the raw turn and cuts windows (W2-05 segmentation); each
                    closed window is enqueued on the durable outbox (F-04) with a receipt (W2-01).
    2. extract      ``workers.windows.WindowPipeline`` makes ONE structured call per window or batch
                    (W2-06b ``PromptContextBuilder``) and ``process_items`` validates, repairs once
                    and dead-letters (W2-06a), producing :class:`AssertionEnvelope`s.
    3. stages       the envelope stage hook list (``derive/envelope_stages.py``, built by
                    ``ingest/windows.py:_envelope_stages``) runs in ``order`` over the envelopes:
                    W2-14 temporal (100), W2-08 grounding (200), W2-09 resolution (300),
                    W2-10 integration (400), W2-13 pre-pass hints (500). Stages run outside the
                    namespace lock; their drop / rewrite counters land in the window stats under
                    ``envelope_stages``.
    4. deposit      valid envelopes go through the mined-fact door (``derive/write.py:write_derived``).
    5. barrier      a read may wait, bounded, for pending extraction (``read.PendingBarrier``).

With no model configured there is no extraction: the L1/L2 stages do not run and no call is made.
This class holds no logic of its own beyond delegation, so the old paths are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["OnlineExtractor"]


class OnlineExtractor:
    """A thin facade over a window pipeline (``workers.windows.WindowPipeline``) or ``None``."""

    def __init__(self, pipeline: Any | None) -> None:
        self._pipeline = pipeline

    @property
    def active(self) -> bool:
        """False when no window extraction is configured (off, or no model)."""
        return self._pipeline is not None

    @property
    def stage_names(self) -> tuple[str, ...]:
        """The envelope stage hook list in run order (empty without ``write.envelope_v2``)."""
        env = getattr(self._pipeline, "_env", None)
        stages = getattr(env, "stages", ()) or ()
        return tuple(s.name for s in sorted(stages, key=lambda s: s.order))

    async def flush(self, namespace: str | None = None, *, final: bool = True) -> dict[str, Any]:
        """Run every closed window now; return the write-quality counters (``{}`` when inactive)."""
        if self._pipeline is None:
            return {}
        out: dict[str, Any] = await self._pipeline.flush(namespace, final=final)
        return out

    def stats(self, *, reset: bool = False) -> dict[str, Any]:
        if self._pipeline is None:
            return {}
        out: dict[str, Any] = self._pipeline.stats(reset=reset)
        return out

    async def readiness(
        self,
        namespace: str,
        capabilities: Sequence[str] = ("window",),
        timeout: float = 0.0,  # noqa: ASYNC109 - the barrier budget
    ) -> Any:
        """F-04 read barrier over the outbox (``None``: no outbox, nothing queued durably)."""
        if self._pipeline is None:
            return None
        return await self._pipeline.readiness(namespace, capabilities, timeout)
