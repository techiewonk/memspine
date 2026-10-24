"""Streaming segmenter for stream segmentation (W2-05, decision W-11 "E"; reuses LIB-14).

The shared segmenter (:class:`memspine.core.window.WindowSegmenter`) cuts at a topic shift, a turn
cap, a token cap and a session change. This one adds the two W-11 rules on top and keeps the rest:

* **Time gap.** A window never spans a source-time gap above ``gap_s`` (nor, with
  ``calendar_day_cut``, a calendar-day change). The rule is LIB-14's
  (:func:`memspine.ingest.segment.hard_cut_reasons`), so the streaming and the batch planner agree.
* **Adaptive growth.** The turn cap is not fixed. A window that reached ``max_turns`` but is
  still below ``grow_tokens`` keeps growing (short turns of one session), up to ``max_turns_cap``;
  the token cap ``max_tokens`` always binds.

Pure: no clock reads, no model calls. Same ``add`` / ``close`` / ``discard`` surface as the shared
segmenter, so the window pipeline uses either.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from memspine.config import constants
from memspine.core.window import Window, WindowSegmenter, WindowTurn, cosine
from memspine.ingest.segment import hard_cut_reasons
from memspine.observability import tap

__all__ = ["SegmentationSettings", "StreamSegmenter"]

_EPSILON = 1e-9


@dataclass(frozen=True)
class SegmentationSettings:
    """stream segmentation and its tunables (see ``config/schema/write.py``)."""

    gap_s: float = 1800.0
    calendar_day_cut: bool = True
    grow_tokens: int = 400
    max_turns_cap: int = 24
    batch_max_tokens: int = 3000
    batch_max_windows: int = 8
    batch_max_age_s: float = 30.0

    @property
    def batching(self) -> bool:
        return self.batch_max_windows > 1 and self.batch_max_tokens > 0


@dataclass
class StreamSegmenter(WindowSegmenter):
    """A :class:`WindowSegmenter` with the time-gap cut and the adaptive turn cap."""

    segmentation: SegmentationSettings = field(default_factory=SegmentationSettings)

    def _turn_limit_reached(self) -> bool:
        base = self.settings.max_turns
        cap = max(self.segmentation.max_turns_cap, base)
        n = len(self._open)
        if n >= cap:
            return True
        if n < base:
            return False
        return sum(t.tokens for t in self._open) >= self.segmentation.grow_tokens

    def add(self, turn: WindowTurn) -> list[Window]:
        closed: list[Window] = []
        s = self.settings
        seg = self.segmentation
        previous = self._open[-1] if self._open else None
        if previous is not None and previous.session != turn.session:
            closed.append(self._cut("session_end"))
            previous = None
        if previous is not None and seg.gap_s > 0 and previous.when and turn.when:
            reasons = hard_cut_reasons(
                previous.session,
                previous.when,
                turn.session,
                turn.when,
                gap_s=seg.gap_s,
                calendar_day_cut=seg.calendar_day_cut,
            )
            if reasons:
                if tap.enabled():
                    tap.event(
                        "write.segment.gap",
                        reason=reasons[0],
                        gap_s=round(abs((turn.when - previous.when).total_seconds()), 1),
                        open_turns=len(self._open),
                    )
                closed.append(self._cut(str(reasons[0])))
                previous = None
        similarity: float | None = None
        if previous is not None and previous.vector is not None and turn.vector is not None:
            similarity = cosine(previous.vector, turn.vector)
            level = self.topic_level
            if (
                level is not None
                and similarity < level - _EPSILON
                and len(self._open) >= constants.WINDOW_TOPIC_MIN_TURNS
            ):
                closed.append(self._cut("topic"))
        if similarity is not None:
            self._stats.add(similarity)  # after the test: a turn never moves its own bar
        if self._open and sum(t.tokens for t in self._open) + turn.tokens > s.max_tokens:
            closed.append(self._cut("tokens"))
        self._open.append(turn)
        if self._turn_limit_reached():
            closed.append(self._cut("turns"))
        return closed
