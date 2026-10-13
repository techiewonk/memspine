"""O4-01a: when does a namespace's sleep job fire (O-02 C). Pure arithmetic, no I/O, no clock.

``(N new turns AND M minutes idle) OR context pressure >= C``, both behind a minimum gap ``K``
since the last completed run. Manual verbs bypass the policy (the caller never asks it). The
daily maintenance job (decay, retention, compress) is due once per ``maintenance_hours``.
The fixed-interval loop of v1 maps to ``N=0, M=0, K=interval`` (O-02 migration).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

__all__ = ["TriggerConfig", "TriggerDecision", "TriggerPolicy", "TriggerState"]


@dataclass(frozen=True)
class TriggerConfig:
    """N, M, C and K are configuration (``offline.sleep_*``)."""

    min_turns: int = 20  # N
    idle: timedelta = timedelta(minutes=10)  # M
    context_pressure: float = 0.70  # C
    min_gap: timedelta = timedelta(minutes=60)  # K
    maintenance_interval: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if self.min_turns < 0:
            raise ValueError("min_turns must be >= 0")
        if not 0.0 < self.context_pressure <= 1.0:
            raise ValueError("context_pressure must be in (0, 1]")
        if min(self.idle, self.min_gap, self.maintenance_interval) < timedelta(0):
            raise ValueError("durations must be non-negative")


@dataclass(frozen=True)
class TriggerState:
    """The persisted per-namespace counters the policy reads."""

    deposits_since_run: int = 0
    last_deposit_at: datetime | None = None
    last_run_at: datetime | None = None
    context_pressure: float = 0.0
    last_maintenance_at: datetime | None = None


@dataclass(frozen=True)
class TriggerDecision:
    fire: bool
    kind: Literal["sleep", "none"]
    #: deposits_idle | context_pressure | deposits_idle+context_pressure | gap | idle_wait | not_due
    reason: str
    next_check_at: datetime | None = None  # idle expiry or end of the gap: when to look again


class TriggerPolicy:
    """Binds a :class:`TriggerConfig` to the two pure decisions."""

    def __init__(self, cfg: TriggerConfig | None = None) -> None:
        self.cfg = cfg or TriggerConfig()

    def decide(self, state: TriggerState, now: datetime) -> TriggerDecision:
        cfg = self.cfg
        n_met = state.deposits_since_run > 0 and state.deposits_since_run >= cfg.min_turns
        idle_at = None if state.last_deposit_at is None else state.last_deposit_at + cfg.idle
        deposit_cond = n_met and (idle_at is None or now >= idle_at)
        pressure_cond = state.context_pressure >= cfg.context_pressure
        if not (deposit_cond or pressure_cond):
            if n_met and idle_at is not None:
                return TriggerDecision(False, "none", "idle_wait", idle_at)
            return TriggerDecision(False, "none", "not_due")
        gap_end = None if state.last_run_at is None else state.last_run_at + cfg.min_gap
        if gap_end is not None and now < gap_end:
            return TriggerDecision(False, "none", "gap", gap_end)
        if deposit_cond and pressure_cond:
            return TriggerDecision(True, "sleep", "deposits_idle+context_pressure")
        return TriggerDecision(
            True, "sleep", "deposits_idle" if deposit_cond else "context_pressure"
        )

    def maintenance_due(self, state: TriggerState, now: datetime) -> bool:
        last = state.last_maintenance_at
        return last is None or now - last >= self.cfg.maintenance_interval
