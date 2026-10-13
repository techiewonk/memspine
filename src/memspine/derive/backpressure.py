"""Backpressure for derived work (RT-02; W-25 B, P-21 C, G-19): coalesce, never drop.

A pure policy over the outbox backlog of one ``(scope, stage)``: how many items sit beyond the
watermark and how old the oldest is. Three outcomes for new background work:

``accept``    under every soft cap: queued as usual.
``coalesce``  over a soft cap (size or age): the item is still persisted and queued, rides the
              job already waiting (the job book keeps at most one follow-up per scope and stage),
              and is marked *underived* so a reader knows derivation lags. Nothing is dropped.
``reject``    over a hard cap: the new work is refused *before* it is accepted (nothing is
              written) with a :class:`BackpressureRejected` carrying a receipt. The caller keeps
              the work and offers it again after ``retry_after_s``; the raw turn it came from is
              already durable, so only its derivation waits.

Pure: stdlib only, the clock is the caller's. ``runtime`` applies it at ``InlineRunner.enqueue``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "UNDERIVED_KEY",
    "Admission",
    "Backlog",
    "Backpressure",
    "BackpressureReceipt",
    "BackpressureRejected",
]

#: Payload key the outbox marker lives under (set by the runner on coalesced items).
UNDERIVED_KEY = "_underived"


@dataclass(frozen=True)
class Backlog:
    """Unfinished items of one ``(scope, stage)`` (or the whole outbox)."""

    items: int = 0
    oldest_age_s: float = 0.0


@dataclass(frozen=True)
class BackpressureReceipt:
    """Why new background work was refused, and when to try again."""

    scope: str
    stage: str
    reason: Literal["items", "age", "total_items"]
    items: int
    limit: float
    oldest_age_s: float
    retry_after_s: float


class BackpressureRejected(Exception):
    """Raised before the work is accepted. Nothing was persisted; the caller still holds it."""

    def __init__(self, receipt: BackpressureReceipt) -> None:
        super().__init__(
            f"backpressure: {receipt.stage} backlog over {receipt.reason} cap "
            f"({receipt.items} items, oldest {receipt.oldest_age_s:.1f}s); "
            f"retry after {receipt.retry_after_s:.1f}s"
        )
        self.receipt = receipt


@dataclass(frozen=True)
class Admission:
    decision: Literal["accept", "coalesce"]
    underived: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class Backpressure:
    """Caps. ``None`` or 0 disables one. Soft caps coalesce; hard caps reject."""

    coalesce_items: int | None = None
    coalesce_age_s: float | None = None
    max_items: int | None = None
    max_age_s: float | None = None
    max_total_items: int | None = None
    retry_after_s: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "coalesce_items",
            "coalesce_age_s",
            "max_items",
            "max_age_s",
            "max_total_items",
        ):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ValueError(f"{name} must be >= 0")

    def admit(self, scope: str, stage: str, backlog: Backlog, total_items: int = 0) -> Admission:
        """Decide for ONE new item. Raises :class:`BackpressureRejected` over a hard cap."""
        if self.max_total_items and total_items >= self.max_total_items:
            raise self._reject(scope, stage, "total_items", backlog, self.max_total_items)
        if self.max_items and backlog.items >= self.max_items:
            raise self._reject(scope, stage, "items", backlog, self.max_items)
        if self.max_age_s and backlog.items and backlog.oldest_age_s >= self.max_age_s:
            raise self._reject(scope, stage, "age", backlog, self.max_age_s)
        if self.coalesce_items and backlog.items >= self.coalesce_items:
            return Admission("coalesce", True, "items")
        if self.coalesce_age_s and backlog.items and backlog.oldest_age_s >= self.coalesce_age_s:
            return Admission("coalesce", True, "age")
        return Admission("accept")

    def _reject(
        self,
        scope: str,
        stage: str,
        reason: Literal["items", "age", "total_items"],
        backlog: Backlog,
        limit: float,
    ) -> BackpressureRejected:
        return BackpressureRejected(
            BackpressureReceipt(
                scope=scope,
                stage=stage,
                reason=reason,
                items=backlog.items,
                limit=float(limit),
                oldest_age_s=backlog.oldest_age_s,
                retry_after_s=self.retry_after_s,
            )
        )
