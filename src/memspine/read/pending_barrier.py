"""``read.PendingBarrier`` (W2-06c, W-12): a read may wait, bounded, for pending extraction.

The write path extracts facts asynchronously after the receipt (outbox window stage). A reader that
wants its own just-written turns reflected calls the barrier first: it waits until the namespace's
window capability covers everything enqueued so far (``ready``), or gives up at the timeout and
reports ``partial`` so the read goes ahead on what exists. It never blocks past the timeout and
never raises into the read. The readiness call is injected (``ingest.windows.readiness``, over the F-04
``InlineRunner.wait``), so this module imports nothing from ``ingest`` or ``runtime``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from memspine.observability import tap
from memspine.observability.logging import get_logger

__all__ = ["BarrierResult", "PendingBarrier"]

_log = get_logger(__name__)

#: ``(namespace, capabilities, timeout_s) -> BarrierOutcome | None`` (None: nothing queued)
ReadinessFn = Callable[[str, Sequence[str], float], Awaitable[Any]]

#: slack on top of the timeout for the hard ``asyncio.wait_for`` cap (the runner polls)
_GRACE_S = 0.25


@dataclass(frozen=True)
class BarrierResult:
    #: ``ready`` (all pending extraction done, or nothing queued), ``partial`` (timed out or the
    #: wait failed; ``missing`` names what is pending), ``off`` (timeout 0: no wait was made)
    status: Literal["ready", "partial", "off"]
    waited_ms: int = 0
    missing: tuple[str, ...] = ()


class PendingBarrier:
    def __init__(self, readiness: ReadinessFn, capabilities: Sequence[str] = ("window",)) -> None:
        self._readiness = readiness
        self._capabilities = tuple(capabilities)

    async def wait(self, namespace: str, timeout_s: float) -> BarrierResult:
        if timeout_s <= 0:
            return BarrierResult("off")
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(
                self._readiness(namespace, self._capabilities, timeout_s), timeout_s + _GRACE_S
            )
        except TimeoutError:
            result = BarrierResult("partial", _ms(started), self._capabilities)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failing wait must not fail the read
            _log.warning("read.pending_barrier_failed", error=type(exc).__name__)
            result = BarrierResult("partial", _ms(started), self._capabilities)
        else:
            if outcome is None:  # no durable queue: nothing to wait for
                result = BarrierResult("ready", _ms(started))
            else:
                missing = tuple(getattr(outcome, "missing", ()))
                status = "ready" if outcome.status == "ready" else "partial"
                result = BarrierResult(status, _ms(started), missing)
        tap.event(
            "read.pending",
            status=result.status,
            waited_ms=result.waited_ms,
            missing=list(result.missing),
            namespace=namespace,
        )
        return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
