"""W1: the adaptive window of the write path (call 1 of the decided write design).

A conversation arrives turn by turn; the write-time LLM call wants a few turns of context
(coreference, relative dates) but not the whole session. A *window* is a run of consecutive
turns of ONE session, closed at the first of four boundaries:

1. ``topic``: the similarity of the new turn to the one before it falls below the store's
   own running ``mean - k * sd`` of that similarity. The level is learned per store (one
   :class:`WindowSegmenter` per namespace) from the similarities it has seen, so it carries
   no benchmark-fitted number; ``k`` is the textbook two-sigma outlier line. No LLM.
2. ``turns``: the window holds ``max_turns`` turns.
3. ``tokens``: adding the turn would pass ``max_tokens`` (4 characters per token).
4. ``session_end`` / ``idle`` / ``flush``: the owner closes the open window (a new session
   starts, the writer went quiet, or the caller asked for everything to be processed).

The segmenter is pure: it takes turns with their embedding vectors and returns closed
windows. It makes no model call and never reads the clock; idleness is the owner's timer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from memspine.config import constants

__all__ = [
    "CLOSE_REASONS",
    "Window",
    "WindowSegmenter",
    "WindowSettings",
    "WindowTurn",
    "cosine",
    "estimate_tokens",
    "segment",
]

#: Float noise floor for the topic test: a store whose consecutive similarities are all equal
#: (sd = 0) must not cut on rounding.
_EPSILON = 1e-9

CLOSE_REASONS = ("topic", "turns", "tokens", "session_end", "idle", "flush")


def estimate_tokens(text: str) -> int:
    """Four characters per token (the engine-wide estimate); at least 1."""
    return max(1, len(text) // 4)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 when either vector is empty or all zero."""
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


@dataclass(frozen=True)
class WindowSettings:
    """The four boundary settings (``write.window_*``)."""

    max_turns: int = 6
    max_tokens: int = 1500
    topic_k: float = 2.0
    idle_s: float = 120.0


@dataclass
class WindowTurn:
    """One turn as the segmenter sees it."""

    record_id: str
    text: str
    session: str | None = None
    when: datetime | None = None
    #: The turn's embedding; None disables the topic boundary around this turn.
    vector: Sequence[float] | None = None
    tokens: int = 0

    def __post_init__(self) -> None:
        if self.tokens <= 0:
            self.tokens = estimate_tokens(self.text)


@dataclass
class Window:
    """A closed window: consecutive turns of one session and why it closed."""

    turns: list[WindowTurn]
    reason: str
    session: str | None = None

    @property
    def tokens(self) -> int:
        return sum(t.tokens for t in self.turns)

    @property
    def record_ids(self) -> list[str]:
        return [t.record_id for t in self.turns]


@dataclass
class _Running:
    """Welford running mean and variance of the consecutive-turn similarity."""

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)

    @property
    def sd(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0


@dataclass
class WindowSegmenter:
    """Streaming segmenter for ONE store (namespace). Feed turns in order with :meth:`add`.

    ``add`` returns the windows the turn closed (zero, one or two); :meth:`close` closes the
    open one. The topic level (``mean - k * sd`` of the similarities seen so far in this
    store) is exposed as :attr:`topic_level` once ``WINDOW_TOPIC_MIN_PAIRS`` similarities
    exist, else None. Similarities across a session change are never counted or tested."""

    settings: WindowSettings = field(default_factory=WindowSettings)
    _open: list[WindowTurn] = field(default_factory=list)
    _stats: _Running = field(default_factory=_Running)

    @property
    def open_turns(self) -> list[WindowTurn]:
        return list(self._open)

    @property
    def pairs(self) -> int:
        return self._stats.n

    @property
    def topic_level(self) -> float | None:
        s = self.settings
        if s.topic_k <= 0.0 or self._stats.n < constants.WINDOW_TOPIC_MIN_PAIRS:
            return None
        return self._stats.mean - s.topic_k * self._stats.sd

    def add(self, turn: WindowTurn) -> list[Window]:
        closed: list[Window] = []
        s = self.settings
        previous = self._open[-1] if self._open else None
        if previous is not None and previous.session != turn.session:
            closed.append(self._cut("session_end"))
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
        if len(self._open) >= s.max_turns:
            closed.append(self._cut("turns"))
        return closed

    def close(self, reason: str = "flush") -> Window | None:
        """Close the open window (None when it is empty)."""
        return self._cut(reason) if self._open else None

    def discard(self, record_id: str) -> bool:
        """Drop a (forgotten) turn from the open window. True when it was there."""
        kept = [t for t in self._open if t.record_id != record_id]
        changed = len(kept) != len(self._open)
        self._open = kept
        return changed

    def _cut(self, reason: str) -> Window:
        turns, self._open = self._open, []
        return Window(turns=turns, reason=reason, session=turns[0].session if turns else None)


def segment(turns: Iterable[WindowTurn], settings: WindowSettings | None = None) -> list[Window]:
    """Segment a whole stream of ONE store at once (offline statistics, tests): the same
    result as feeding :class:`WindowSegmenter` turn by turn and flushing at the end."""
    segmenter = WindowSegmenter(settings or WindowSettings())
    out: list[Window] = []
    for turn in turns:
        out.extend(segmenter.add(turn))
    tail = segmenter.close("flush")
    if tail is not None:
        out.append(tail)
    return out
