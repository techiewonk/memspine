"""Background budget and read-side freshness (LIB-26): ``covers_seq``, verdicts, read barrier.

Pure library, not wired (wiring is O4-01b / F-04). Standard library, ``memspine.schemas`` and
``memspine.runtime.jobs`` only. No I/O, no real clock (a clock or ``now`` is injected).

Binding decisions: O-05 (token budget per namespace, deferred work counted never dropped), O-06
(``covers_seq`` on every derived item; raw evidence outranks a stale derivation), ER-09 (contiguous
watermark plus explicit holes; a wait captures a fixed target that later writes cannot extend),
ER-06 / session 7 (readiness per capability; wait with timeout; a read after a timed-out wait is
partial and lists the missing capabilities), OF-2 / EX-4 (sleep at most ~0.2 model calls per turn).

* ``covers_seq`` of a derived layer is the stage's contiguous watermark; open holes below it are
  listed separately and never counted as covered.
* A capability that is not configured is ``unavailable``, never ``pending``: a reader must not
  wait for something that will never arrive.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from memspine.runtime.jobs import ScopeKey, scope_key
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.jobs import Progress

__all__ = [
    "CAPABILITIES",
    "Action",
    "BackgroundBudget",
    "BarrierResult",
    "BudgetConfig",
    "BudgetDecision",
    "BudgetStats",
    "FreshnessPolicy",
    "FreshnessVerdict",
    "Priority",
    "covers_seq_of",
    "derived_freshness",
    "freshness",
    "read_barrier",
]

Clock = Callable[[], datetime]

CAPABILITIES: tuple[str, ...] = ("raw_search", "facts", "graph", "summaries", "profile")


# --------------------------------------------------------------------------- covers_seq
def covers_seq_of(progress: Progress | None) -> int:
    """The contiguous watermark of source events a derived layer reflects (-1 = none)."""
    return -1 if progress is None else progress.watermark


def _open_hole_offsets(progress: Progress | None) -> tuple[int, ...]:
    if progress is None:
        return ()
    return tuple(h.offset for h in progress.holes if not h.waived)


# --------------------------------------------------------------------------- freshness
@dataclass(frozen=True)
class FreshnessPolicy:
    """Which capabilities are configured and when a lagging layer counts as stale."""

    configured: frozenset[str] = frozenset(CAPABILITIES)
    max_lag_events: int | None = None  # stale when more than this many events behind
    max_age: timedelta | None = None  # stale when behind and not advanced for longer than this
    max_lag_events_by_capability: Mapping[str, int] = field(default_factory=dict)
    max_age_by_capability: Mapping[str, timedelta] = field(default_factory=dict)

    def lag_limit(self, capability: str) -> int | None:
        return self.max_lag_events_by_capability.get(capability, self.max_lag_events)

    def age_limit(self, capability: str) -> timedelta | None:
        return self.max_age_by_capability.get(capability, self.max_age)


@dataclass(frozen=True)
class FreshnessVerdict:
    capability: str
    status: Literal["fresh", "lagging", "stale", "unavailable"]
    covers_seq: int  # contiguous watermark (-1 = nothing derived yet)
    log_head_seq: int
    behind: int  # events newer than covers_seq
    holes: tuple[int, ...] = ()  # open (unwaived) hole offsets at or below the watermark
    reason: str = ""
    age: timedelta | None = None  # time since the layer last advanced, when known

    @property
    def ok(self) -> bool:
        return self.status == "fresh"


def freshness(
    capability: str,
    log_head_seq: int,
    progress: Progress | None,
    *,
    policy: FreshnessPolicy | None = None,
    now: datetime | None = None,
    advanced_at: datetime | None = None,
) -> FreshnessVerdict:
    """Verdict for one capability against the log head.

    ``fresh``: covers the head with no open holes. ``lagging``: events behind and/or open holes.
    ``stale``: lagging beyond the configured max lag in events, or behind for longer than the max
    age (needs ``now`` and ``advanced_at``). ``unavailable``: capability not configured.
    """
    if log_head_seq < -1:
        raise ValueError("log_head_seq must be >= -1")
    pol = policy or FreshnessPolicy()
    if capability not in pol.configured:
        return FreshnessVerdict(
            capability, "unavailable", -1, log_head_seq, 0, (), "capability not configured"
        )
    covers = covers_seq_of(progress)
    behind = max(log_head_seq - covers, 0)
    holes = _open_hole_offsets(progress)
    age = None if now is None or advanced_at is None else max(now - advanced_at, timedelta(0))
    if behind == 0 and not holes:
        return FreshnessVerdict(capability, "fresh", covers, log_head_seq, 0, (), "", age)
    lag_lim, age_lim = pol.lag_limit(capability), pol.age_limit(capability)
    if behind > 0 and lag_lim is not None and behind > lag_lim:
        return FreshnessVerdict(
            capability, "stale", covers, log_head_seq, behind, holes, "max_lag_events", age
        )
    if behind > 0 and age_lim is not None and age is not None and age > age_lim:
        return FreshnessVerdict(
            capability, "stale", covers, log_head_seq, behind, holes, "max_age", age
        )
    return FreshnessVerdict(
        capability,
        "lagging",
        covers,
        log_head_seq,
        behind,
        holes,
        "behind" if behind else "open_holes",
        age,
    )


def derived_freshness(covers_seq: int | None, log_head_seq: int) -> tuple[bool, int]:
    """O-06 reader check on one derived record: ``(stale, newer_sources)``. ``covers_seq=None``
    (v1 records) is stale until re-derived; stale records rank below raw evidence."""
    if covers_seq is None:
        return True, max(log_head_seq + 1, 0)
    newer = max(log_head_seq - covers_seq, 0)
    return newer > 0, newer


# --------------------------------------------------------------------------- read barrier
@dataclass(frozen=True)
class BarrierResult:
    """Outcome of a read barrier. ``status``: ``ready`` (read may proceed, nothing missing),
    ``wait`` (pending, within the timeout; wait until ``wait_until_seq`` or ``deadline``),
    ``partial`` (proceed now without the ``missing`` capabilities)."""

    status: Literal["ready", "wait", "partial"]
    target_seq: int  # captured target; later writes do not extend it (ER-09)
    ready: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()  # not configured: never waited for
    failed: tuple[str, ...] = ()
    holes: Mapping[str, tuple[int, ...]] = field(default_factory=dict)  # open holes <= target
    wait_until_seq: int | None = None
    deadline: datetime | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        """Capabilities a partial read lacks (session 7)."""
        if self.status == "wait":
            return ()
        return tuple(sorted({*self.pending, *self.unavailable, *self.failed}))

    @property
    def may_proceed(self) -> bool:
        return self.status != "wait"


def read_barrier(
    requested_capabilities: Iterable[str],
    target_seq: int,
    progress_by_capability: Mapping[str, Progress | None],
    *,
    timeout: timedelta,
    clock: Clock,
    started_at: datetime | None = None,
    failed: frozenset[str] = frozenset(),
) -> BarrierResult:
    """Decide whether a read may proceed.

    A capability absent from ``progress_by_capability`` is not configured (``unavailable``, never
    waited for). A key mapped to ``None`` is configured with nothing handled yet. A capability is
    ready when its watermark reaches ``target_seq``; open holes at or below the target are reported
    but do not block (the watermark passed them visibly). ``started_at`` is when the wait began;
    omitted, the wait starts now. After ``started_at + timeout`` pending capabilities make the read
    ``partial``. ``failed`` capabilities never block: they are reported missing.
    """
    if timeout < timedelta(0):
        raise ValueError("timeout must be >= 0")
    now = clock()
    deadline = (started_at or now) + timeout
    ready: list[str] = []
    pending: list[str] = []
    unavailable: list[str] = []
    failed_out: list[str] = []
    holes: dict[str, tuple[int, ...]] = {}
    for cap in sorted(set(requested_capabilities)):
        if cap not in progress_by_capability:
            unavailable.append(cap)
            continue
        prog = progress_by_capability[cap]
        h = tuple(o for o in _open_hole_offsets(prog) if o <= target_seq)
        if h:
            holes[cap] = h
        if cap in failed:
            failed_out.append(cap)
        elif covers_seq_of(prog) >= target_seq:
            ready.append(cap)
        else:
            pending.append(cap)
    if pending and now < deadline:
        return BarrierResult(
            "wait",
            target_seq,
            tuple(ready),
            tuple(pending),
            tuple(unavailable),
            tuple(failed_out),
            holes,
            wait_until_seq=target_seq,
            deadline=deadline,
        )
    status: Literal["ready", "partial"] = (
        "partial" if (pending or unavailable or failed_out) else "ready"
    )
    return BarrierResult(
        status,
        target_seq,
        tuple(ready),
        tuple(pending),
        tuple(unavailable),
        tuple(failed_out),
        holes,
    )


# --------------------------------------------------------------------------- background budget
Action = Literal["allow", "defer", "skip_optional"]
Priority = Literal["accepted", "optional"]


@dataclass(frozen=True)
class BudgetConfig:
    """Allowance for background model work. ``None`` = unmetered (O-05: off by default).

    ``calls_per_turn`` is the sleep target (OF-2: about 0.2). Allowance accrues per turn inside a
    window of ``window_turns``; unused allowance carries into the next window, capped at
    ``max_carry_windows`` windows' worth. Overdraft by one oversize accepted job carries as debt.
    """

    calls_per_turn: float | None = 0.2
    tokens_per_turn: float | None = None
    window_turns: int = 10
    carry_over: bool = True
    max_carry_windows: float = 1.0

    def __post_init__(self) -> None:
        if self.window_turns < 1:
            raise ValueError("window_turns must be >= 1")
        if self.max_carry_windows < 0:
            raise ValueError("max_carry_windows must be >= 0")
        for v in (self.calls_per_turn, self.tokens_per_turn):
            if v is not None and v < 0:
                raise ValueError("per-turn allowance must be >= 0")


@dataclass(frozen=True)
class BudgetDecision:
    action: Action
    reason: str
    calls: int
    tokens: int
    remaining_calls: float | None
    remaining_tokens: float | None
    charged: bool


@dataclass(frozen=True)
class BudgetStats:
    turns: int
    spent_calls: int
    spent_tokens: int
    deferred: int  # accepted work held back (stays queued / dirty, never dropped)
    skipped_optional: int
    deferred_calls: int
    deferred_tokens: int


@dataclass
class _ScopeState:
    turns_in_window: int = 0
    total_turns: int = 0
    spent_calls: float = 0.0
    spent_tokens: float = 0.0
    carry_calls: float = 0.0
    carry_tokens: float = 0.0
    total_calls: int = 0
    total_tokens: int = 0
    deferred: int = 0
    skipped: int = 0
    deferred_calls: int = 0
    deferred_tokens: int = 0


def _r(x: float) -> float:
    return round(x, 9)


class BackgroundBudget:
    """Per-scope background model-call / token allowance per window of turns.

    ``decide`` charges only when it returns ``allow``. Model-free work (zero calls and zero tokens,
    e.g. daily maintenance) is always allowed and never charged. Over budget, *accepted* work is
    ``defer``red (counted, stays queued; never dropped) and *optional* work is ``skip_optional``.
    An accepted job larger than a whole window's allowance runs once at an untouched window and the
    overdraft is repaid by later windows (no starvation).
    """

    def __init__(self, config: BudgetConfig | None = None) -> None:
        self.config = config or BudgetConfig()
        self._state: dict[ScopeKey, _ScopeState] = {}

    def _st(self, scope: OwnerScope) -> _ScopeState:
        return self._state.setdefault(scope_key(scope), _ScopeState())

    def _window_allowance(self) -> tuple[float | None, float | None]:
        c, w = self.config, self.config.window_turns
        return (
            None if c.calls_per_turn is None else _r(c.calls_per_turn * w),
            None if c.tokens_per_turn is None else _r(c.tokens_per_turn * w),
        )

    def _accrued(self, st: _ScopeState) -> tuple[float, float]:
        c = self.config
        return (
            _r((c.calls_per_turn or 0.0) * st.turns_in_window + st.carry_calls),
            _r((c.tokens_per_turn or 0.0) * st.turns_in_window + st.carry_tokens),
        )

    def record_turns(self, scope: OwnerScope, n: int = 1) -> None:
        """Advance the turn clock; completed windows roll their unused allowance forward."""
        if n < 0:
            raise ValueError("negative turns")
        st, cfg = self._st(scope), self.config
        wc, wt = self._window_allowance()
        for _ in range(n):
            st.turns_in_window += 1
            st.total_turns += 1
            if st.turns_in_window >= cfg.window_turns:
                ac, at = self._accrued(st)
                unused_c = _r(ac - st.spent_calls)
                unused_t = _r(at - st.spent_tokens)
                cap_c = _r((wc or 0.0) * cfg.max_carry_windows)
                cap_t = _r((wt or 0.0) * cfg.max_carry_windows)
                if cfg.carry_over:
                    st.carry_calls = min(unused_c, cap_c)
                    st.carry_tokens = min(unused_t, cap_t)
                else:
                    st.carry_calls = min(unused_c, 0.0)  # debt only
                    st.carry_tokens = min(unused_t, 0.0)
                st.turns_in_window = 0
                st.spent_calls = 0.0
                st.spent_tokens = 0.0

    def remaining(self, scope: OwnerScope) -> tuple[float | None, float | None]:
        st = self._st(scope)
        ac, at = self._accrued(st)
        c = self.config
        return (
            None if c.calls_per_turn is None else _r(ac - st.spent_calls),
            None if c.tokens_per_turn is None else _r(at - st.spent_tokens),
        )

    def decide(
        self,
        scope: OwnerScope,
        calls: int = 1,
        tokens: int = 0,
        *,
        priority: Priority = "accepted",
    ) -> BudgetDecision:
        if calls < 0 or tokens < 0:
            raise ValueError("negative cost")
        st, cfg = self._st(scope), self.config
        rc, rt = self.remaining(scope)
        if calls == 0 and tokens == 0:
            return BudgetDecision("allow", "model_free", 0, 0, rc, rt, False)
        fits = (rc is None or calls <= rc) and (rt is None or tokens <= rt)
        oversize = False
        if not fits and priority == "accepted":
            wc, wt = self._window_allowance()
            fresh_window = st.spent_calls == 0 and st.spent_tokens == 0
            too_big = (wc is not None and calls > wc * (1 + cfg.max_carry_windows)) or (
                wt is not None and tokens > wt * (1 + cfg.max_carry_windows)
            )
            oversize = fresh_window and too_big
        if fits or oversize:
            st.spent_calls += calls
            st.spent_tokens += tokens
            st.total_calls += calls
            st.total_tokens += tokens
            rc, rt = self.remaining(scope)
            reason = "oversize_overdraft" if oversize else "within_budget"
            return BudgetDecision("allow", reason, calls, tokens, rc, rt, True)
        if priority == "optional":
            st.skipped += 1
            return BudgetDecision("skip_optional", "budget_exhausted", calls, tokens, rc, rt, False)
        st.deferred += 1
        st.deferred_calls += calls
        st.deferred_tokens += tokens
        return BudgetDecision("defer", "budget_exhausted", calls, tokens, rc, rt, False)

    def stats(self, scope: OwnerScope) -> BudgetStats:
        s = self._st(scope)
        return BudgetStats(
            s.total_turns,
            s.total_calls,
            s.total_tokens,
            s.deferred,
            s.skipped,
            s.deferred_calls,
            s.deferred_tokens,
        )
