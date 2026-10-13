"""Segmentation of incoming turns into extraction windows (LIB-14, decision W-11 "E").

Pure and unwired (wiring is W2-05): no I/O, no clock reads, no model calls. The token counter is a
callable passed in (default: ``ceil(chars / 4)``).

What a plan promises:

* **Coverage.** Every unit (a turn, or a sub-piece of an oversize turn) is a NEW line in exactly
  one window. Context lines are reference only (ER-11: citeable as binding evidence, never a new
  occurrence) and never become NEW twice.
* **Cuts.** A window never spans a session change, a source-time gap above ``gap_s`` or
  (optionally) a calendar-day change. Inside such a run, windows close at ``max_turns`` or
  ``max_new_tokens``.
* **Oversize turns.** A turn above ``max_new_tokens`` on its own is split on sentence boundaries
  (then words, then characters) into pieces with stable sub-ids ``"<turn id>#<n>"``. A turn that
  fits is never split.
* **Tiny tail merge.** The last window of a run, if tiny, folds into the previous window of that
  run when the token budget allows (up to ``tail_slack`` turns over ``max_turns``). Hard cuts
  are never merged across.
* **Idempotent and resumable.** Planning is a pure function of (config, turns, state). The
  window id is a hash of (scope, first NEW id, last NEW id). After a partial failure, pass a
  state whose ``last_unit_id`` is the last committed NEW line (see :meth:`Segmenter.advance`);
  the remaining plans are exactly the suffix of the full plan when the commit fell on a window
  boundary, with no overlap and no gap in NEW lines.
* **now_hint.** With ``now_hint`` the still-open final window (closed only by end of input, not
  full, and whose last turn is less than ``idle_s`` before ``now_hint``) is withheld so that
  more turns can join it. Without ``now_hint`` everything is flushed.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field

from memspine.schemas.base import InternalModel

__all__ = [
    "PlanLine",
    "SegmentConfig",
    "SegmentState",
    "Segmenter",
    "TokenCounter",
    "Turn",
    "WindowPlan",
    "estimate_tokens",
    "hard_cut_reasons",
]

TokenCounter = Callable[[str], int]
CutReason = Literal["time_gap", "calendar_day", "session_end", "turns", "tokens", "flush"]


def estimate_tokens(text: str) -> int:
    """Default counter: about four characters per token, at least one."""
    return max(1, math.ceil(len(text) / 4))


class Turn(InternalModel):
    """One incoming turn (an L0 segment, in stream order)."""

    id: str
    speaker: str | None = None
    text: str
    source_at: datetime | None = None
    session: str | None = None


class SegmentConfig(InternalModel):
    """Tunables (``derive.window.*``, W-11 defaults)."""

    gap_s: float = 1800.0
    calendar_day_cut: bool = True
    min_turns: int = Field(default=2, ge=1)
    max_turns: int = Field(default=6, ge=1)
    max_new_tokens: int = Field(default=1500, ge=1)
    tail_slack: int = Field(default=2, ge=0)
    context_lines: int = Field(default=4, ge=0)
    max_context_tokens: int = Field(default=600, ge=0)
    context_across_cut: bool = False
    idle_s: float = 120.0


class SegmentState(InternalModel):
    """Resume point: the last committed NEW line id and the tail of earlier lines (context)."""

    last_unit_id: str | None = None
    tail: tuple[Turn, ...] = ()


class PlanLine(InternalModel):
    """One numbered prompt line. ``no`` runs 1..n over CONTEXT lines then NEW lines."""

    no: int
    kind: Literal["context", "new"]
    id: str
    speaker: str | None = None
    text: str
    source_at: datetime | None = None
    session: str | None = None
    tokens: int


class WindowPlan(InternalModel):
    window_id: str
    scope: str
    session: str | None
    new_ids: tuple[str, ...]
    context_ids: tuple[str, ...]
    lines: tuple[PlanLine, ...]
    tokens_new: int
    tokens_context: int
    reasons: tuple[CutReason, ...]
    split_turn_ids: tuple[str, ...] = ()

    @property
    def tokens(self) -> int:
        return self.tokens_new + self.tokens_context


@dataclass(frozen=True)
class _Unit:
    id: str
    parent: str
    speaker: str | None
    text: str
    source_at: datetime | None
    session: str | None
    tokens: int


def hard_cut_reasons(
    a_session: str | None,
    a_at: datetime | None,
    b_session: str | None,
    b_at: datetime | None,
    *,
    gap_s: float,
    calendar_day_cut: bool = True,
) -> list[CutReason]:
    """Why a window must not span two neighbouring units (empty: it may). Shared by the batch
    planner and the streaming segmenter (``derive.segmenter``, W2-05); pure."""
    r: list[CutReason] = []
    if a_session != b_session:
        r.append("session_end")
    if a_at is not None and b_at is not None:
        try:
            if abs((b_at - a_at).total_seconds()) > gap_s:
                r.append("time_gap")
            if calendar_day_cut and a_at.date() != b_at.date():
                r.append("calendar_day")
        except TypeError:  # naive vs aware: no comparison possible
            pass
    return r


_SENT = re.compile(r"[^.!?]*[.!?]+(?:\s+|$)|[^.!?]+$")


class Segmenter:
    def __init__(
        self,
        config: SegmentConfig | None = None,
        *,
        scope: str = "",
        counter: TokenCounter = estimate_tokens,
    ) -> None:
        self.config = config or SegmentConfig()
        if self.config.min_turns > self.config.max_turns:
            raise ValueError("min_turns must not exceed max_turns")
        self.scope = scope
        self._count = counter

    # -- public ------------------------------------------------------------------------------

    def plan(
        self,
        turns: Sequence[Turn],
        state: SegmentState | None = None,
        *,
        now_hint: datetime | None = None,
    ) -> list[WindowPlan]:
        """Plan windows for ``turns`` (stream order).

        Units up to and including ``state.last_unit_id`` are treated as already committed (usable as
        context only). If that id is not among ``turns``, all of ``turns`` is new.
        """
        state = state or SegmentState()
        seen: set[str] = set()
        for t in turns:
            if t.id in seen:
                raise ValueError(f"duplicate turn id {t.id!r}")
            seen.add(t.id)
        units = [u for t in turns for u in self._units(t)]
        ids = [u.id for u in units]
        k = 0
        if state.last_unit_id is not None and state.last_unit_id in ids:
            k = ids.index(state.last_unit_id) + 1
        fresh = units[k:]
        if not fresh:
            return []
        in_input = {u.id for u in units[:k]}
        history = [self._unit_from_turn(t) for t in state.tail if t.id not in in_input] + units[:k]
        plans: list[WindowPlan] = []
        for run, closing in self._runs(fresh):
            groups = self._pack(run)
            for gi, grp in enumerate(groups):
                if gi == len(groups) - 1:
                    reasons = closing
                else:
                    reasons = self._soft_reason(grp, groups[gi + 1][0])
                plans.append(self._build(history, grp, reasons))
                history.extend(grp)
        if now_hint is not None and plans and self._is_open(plans[-1], fresh[-1], now_hint):
            plans.pop()
        return plans

    def advance(self, state: SegmentState, plan: WindowPlan) -> SegmentState:
        """State after ``plan`` has been committed (its last NEW line is the new offset)."""
        keep = self.config.context_lines
        lines = list(state.tail) + [
            Turn(
                id=ln.id,
                speaker=ln.speaker,
                text=ln.text,
                source_at=ln.source_at,
                session=ln.session,
            )
            for ln in plan.lines
            if ln.kind == "new"
        ]
        tail = tuple(lines[-keep:]) if keep else ()
        return SegmentState(last_unit_id=plan.new_ids[-1], tail=tail)

    # -- units ---------------------------------------------------------------------------------

    def _unit_from_turn(self, t: Turn) -> _Unit:
        return _Unit(
            t.id, t.id.split("#")[0], t.speaker, t.text, t.source_at, t.session, self._count(t.text)
        )

    def _units(self, t: Turn) -> list[_Unit]:
        cap = self.config.max_new_tokens
        n = self._count(t.text)
        if n <= cap:
            return [_Unit(t.id, t.id, t.speaker, t.text, t.source_at, t.session, n)]
        pieces = self._split_text(t.text, cap)
        return [
            _Unit(f"{t.id}#{i}", t.id, t.speaker, p, t.source_at, t.session, self._count(p))
            for i, p in enumerate(pieces)
        ]

    def _split_text(self, text: str, cap: int) -> list[str]:
        atoms: list[str] = []
        for sent in (s.strip() for s in _SENT.findall(text)):
            if not sent:
                continue
            if self._count(sent) <= cap:
                atoms.append(sent)
            else:
                atoms.extend(self._hard_split(sent, cap))
        out: list[str] = []
        cur = ""
        for a in atoms:
            cand = f"{cur} {a}" if cur else a
            if cur and self._count(cand) > cap:
                out.append(cur)
                cur = a
            else:
                cur = cand
        if cur:
            out.append(cur)
        return out or [text]

    def _hard_split(self, s: str, cap: int) -> list[str]:
        out: list[str] = []
        rest = s
        while rest:
            if self._count(rest) <= cap:
                out.append(rest)
                break
            lo, hi = 1, len(rest)
            while lo < hi:  # largest prefix that fits
                mid = (lo + hi + 1) // 2
                if self._count(rest[:mid]) <= cap:
                    lo = mid
                else:
                    hi = mid - 1
            cut = lo
            sp = rest.rfind(" ", 1, cut + 1)
            if sp > 0:
                cut = sp
            out.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        return [o for o in out if o]

    # -- cuts and packing ------------------------------------------------------------------------

    def _hard_between(self, a: _Unit, b: _Unit) -> list[CutReason]:
        return hard_cut_reasons(
            a.session,
            a.source_at,
            b.session,
            b.source_at,
            gap_s=self.config.gap_s,
            calendar_day_cut=self.config.calendar_day_cut,
        )

    def _runs(self, fresh: list[_Unit]) -> list[tuple[list[_Unit], tuple[CutReason, ...]]]:
        runs: list[tuple[list[_Unit], tuple[CutReason, ...]]] = []
        cur = [fresh[0]]
        for u in fresh[1:]:
            r = self._hard_between(cur[-1], u)
            if r:
                runs.append((cur, tuple(r)))
                cur = [u]
            else:
                cur.append(u)
        runs.append((cur, ("flush",)))
        return runs

    def _pack(self, run: list[_Unit]) -> list[list[_Unit]]:
        c = self.config
        groups: list[list[_Unit]] = []
        cur: list[_Unit] = []
        tok = 0
        for u in run:
            if cur and (len(cur) >= c.max_turns or tok + u.tokens > c.max_new_tokens):
                groups.append(cur)
                cur, tok = [], 0
            cur.append(u)
            tok += u.tokens
        groups.append(cur)
        if len(groups) > 1:
            tail, prev = groups[-1], groups[-2]
            if (
                len(tail) < c.min_turns
                and len(prev) + len(tail) <= c.max_turns + c.tail_slack
                and sum(x.tokens for x in prev + tail) <= c.max_new_tokens
            ):
                groups[-2:] = [prev + tail]
        return groups

    def _soft_reason(self, grp: list[_Unit], nxt: _Unit) -> tuple[CutReason, ...]:
        r: list[CutReason] = []
        if len(grp) >= self.config.max_turns:
            r.append("turns")
        if sum(x.tokens for x in grp) + nxt.tokens > self.config.max_new_tokens:
            r.append("tokens")
        return tuple(r) or ("turns",)

    # -- one window ----------------------------------------------------------------------------

    def _context(self, history: list[_Unit], first: _Unit) -> list[_Unit]:
        c = self.config
        out: list[_Unit] = []
        toks = 0
        nxt = first
        for u in reversed(history):
            if len(out) >= c.context_lines:
                break
            if not c.context_across_cut and self._hard_between(u, nxt):
                break
            if toks + u.tokens > c.max_context_tokens:
                break
            out.append(u)
            toks += u.tokens
            nxt = u
        out.reverse()
        return out

    def _build(
        self, history: list[_Unit], grp: list[_Unit], reasons: tuple[CutReason, ...]
    ) -> WindowPlan:
        ctx = self._context(history, grp[0])
        lines = [
            PlanLine(
                no=i,
                kind=kind,
                id=u.id,
                speaker=u.speaker,
                text=u.text,
                source_at=u.source_at,
                session=u.session,
                tokens=u.tokens,
            )
            for i, (kind, u) in enumerate(
                [("context", x) for x in ctx] + [("new", x) for x in grp], 1
            )
        ]
        key = f"{self.scope}\x1f{grp[0].id}\x1f{grp[-1].id}".encode()
        return WindowPlan(
            window_id="win_" + hashlib.sha256(key).hexdigest()[:16],
            scope=self.scope,
            session=grp[0].session,
            new_ids=tuple(u.id for u in grp),
            context_ids=tuple(u.id for u in ctx),
            lines=tuple(lines),
            tokens_new=sum(u.tokens for u in grp),
            tokens_context=sum(u.tokens for u in ctx),
            reasons=reasons,
            split_turn_ids=tuple(dict.fromkeys(u.parent for u in grp if u.id != u.parent)),
        )

    def _is_open(self, last: WindowPlan, last_unit: _Unit, now: datetime) -> bool:
        if last.reasons != ("flush",) or last_unit.source_at is None:
            return False
        c = self.config
        if len(last.new_ids) >= c.max_turns or last.tokens_new >= c.max_new_tokens:
            return False
        try:
            return (now - last_unit.source_at).total_seconds() < c.idle_s
        except TypeError:
            return False
