"""Call batcher for ``write.segmentation_v2`` (W2-05, decision W-11 "B").

Closed windows accumulate until a token budget, a window count or a maximum age is reached; then
one extraction call carries them as separately numbered blocks. The batcher only *groups*: it
never merges windows into one segment, so a window stays the unit of citation.

Pure (``now`` is passed in, no clock reads). Windows keep their queue order, and a batch never
mixes keys (a key is the namespace plus its scope), so a key change always closes the batch before
it. The still-open last batch of the queue is withheld until it is full or its oldest window is
``max_age_s`` old, unless the caller says ``final`` (a flush, a session end, a stop).
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = ["Batch", "BatchItem", "CallBatcher"]

BatchReason = Literal["tokens", "windows", "key", "age", "final"]


@dataclass(frozen=True)
class BatchItem:
    """One closed window waiting for its call: ``ref`` is whatever the caller needs back."""

    key: Hashable
    ref: object
    tokens: int
    queued_at: float


@dataclass(frozen=True)
class Batch:
    key: Hashable
    items: tuple[BatchItem, ...]
    reason: BatchReason

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)


class CallBatcher:
    def __init__(self, *, max_tokens: int, max_windows: int, max_age_s: float) -> None:
        if max_windows < 1:
            raise ValueError("max_windows must be at least 1")
        self.max_tokens = max_tokens
        self.max_windows = max_windows
        self.max_age_s = max_age_s

    def plan(self, items: Sequence[BatchItem], now: float, *, final: bool = False) -> list[Batch]:
        """Ready batches, in queue order. Items not in any returned batch are held."""
        out: list[Batch] = []
        cur: list[BatchItem] = []
        tokens = 0

        def close(reason: BatchReason) -> None:
            nonlocal cur, tokens
            if cur:
                out.append(Batch(cur[0].key, tuple(cur), reason))
            cur, tokens = [], 0

        for item in items:
            if cur and item.key != cur[0].key:
                close("key")
            if cur and (len(cur) >= self.max_windows or tokens + item.tokens > self.max_tokens):
                close("windows" if len(cur) >= self.max_windows else "tokens")
            cur.append(item)
            tokens += item.tokens
        if cur:
            full = len(cur) >= self.max_windows or tokens >= self.max_tokens
            if final:
                close("final")
            elif full:
                close("windows" if len(cur) >= self.max_windows else "tokens")
            elif now - min(i.queued_at for i in cur) >= self.max_age_s:
                close("age")
        return out

    def next_wake(self, items: Sequence[BatchItem], now: float) -> float | None:
        """Seconds until the held tail turns ready by age (None: nothing is held)."""
        if not items:
            return None
        return max(0.0, self.max_age_s - (now - min(i.queued_at for i in items)))
