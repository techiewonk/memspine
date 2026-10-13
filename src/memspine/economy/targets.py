"""Write and read targets as regression gates (W2-12; P-14, EX-2, EX-4; on LIB-27 ``WriteTargets``).

The plan's per-turn targets, checked against a ``WriteCostLedger``:

- at most 1 generative call per window for all online layers (retries and split calls count);
- static prompt at most 500 tokens;
- output at most 2x input (completion tokens over prompt tokens, window extraction);
- sleep at most 0.2 calls per turn, amortised;
- deep-tier read (R-22): at most 1 extra model call and 1 extra search per read.

A missed target fails ``assert_met`` unless it carries a recorded waiver (P-14: a run that exceeds
a target without a waiver fails). Counts come from the ledger; the caller supplies the
denominators (turns, windows) and the figures only the caller knows (static prompt tokens, deep
reads). Pure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from memspine.economy.ledger import WriteCostLedger
from memspine.observability.stage_ledger import TargetVerdict, WriteTargets

__all__ = [
    "SLEEP_STAGES",
    "TargetFailure",
    "TargetResult",
    "TargetSet",
]

#: Ledger stages that count as "sleep" for the per-turn target (the K_C cycle; not window extract).
SLEEP_STAGES: tuple[str, ...] = (
    "consolidate",
    "mine_facts",
    "project_facts",
    "anticipate",
    "reflect_profile",
    "extract_graph",
    "rule_edges",
    "summarize_entities",
    "reorganize",
    "decay_sweep",
    "compress",
    "sleep_compute",
    "reconcile",
    "histories",
    "pattern_observations",
    "standing_answers",
    "compile_edges",
    "predict_calibrate",
    "session_close",
)


class TargetFailure(AssertionError):
    """One or more targets were missed without a waiver."""


@dataclass(frozen=True)
class TargetResult:
    name: str
    status: Literal["met", "missed", "waived", "not_measured"]
    observed: float | None
    target: float
    waiver: str | None = None


@dataclass(frozen=True)
class TargetSet:
    """The P-14 targets. A value of None is not checked."""

    generative_calls_per_window: float | None = 1.0
    static_prompt_tokens: int | None = 500
    output_to_input_ratio: float | None = 2.0
    sleep_calls_per_turn: float | None = 0.2
    deep_extra_calls_per_read: float | None = 1.0
    deep_extra_searches_per_read: float | None = 1.0
    window_stages: tuple[str, ...] = ("window_extract",)
    sleep_stages: tuple[str, ...] = SLEEP_STAGES
    #: target name -> reason; a missed target with a waiver is reported ``waived``, not failed.
    waivers: Mapping[str, str] = field(default_factory=dict)

    def check(
        self,
        ledger: WriteCostLedger,
        *,
        turns: int | None = None,
        windows: int | None = None,
        static_prompt_tokens: int | None = None,
        deep_reads: int | None = None,
        deep_extra_calls: int | None = None,
        deep_extra_searches: int | None = None,
    ) -> list[TargetResult]:
        out: list[TargetResult] = []
        lib = WriteTargets(
            write_p95_ms=None,
            generative_calls_per_window=self.generative_calls_per_window,
            sleep_calls_per_turn=None,
            window_stages=self.window_stages,
        )
        out.extend(
            self._lift(v) for v in lib.check(ledger.stage_ledger, turns=turns, windows=windows)
        )
        if self.sleep_calls_per_turn is not None:  # model (generative) calls only, not embeds
            per_turn = ledger.stage_ledger.calls_per_turn(
                turns or 0, cost_class="generative", stages=self.sleep_stages
            )
            out.append(self._cmp("sleep_calls_per_turn", per_turn, self.sleep_calls_per_turn))
        if self.static_prompt_tokens is not None:
            out.append(
                self._cmp("static_prompt_tokens", static_prompt_tokens, self.static_prompt_tokens)
            )
        if self.output_to_input_ratio is not None:
            rows = ledger.select(stages=self.window_stages, kind="generative")
            tin = sum(r.tokens_in for r in rows)
            ratio = (sum(r.tokens_out for r in rows) / tin) if tin else None
            out.append(self._cmp("output_to_input_ratio", ratio, self.output_to_input_ratio))
        if self.deep_extra_calls_per_read is not None:
            out.append(
                self._cmp(
                    "deep_extra_calls_per_read",
                    _per(deep_extra_calls, deep_reads),
                    self.deep_extra_calls_per_read,
                )
            )
        if self.deep_extra_searches_per_read is not None:
            out.append(
                self._cmp(
                    "deep_extra_searches_per_read",
                    _per(deep_extra_searches, deep_reads),
                    self.deep_extra_searches_per_read,
                )
            )
        return out

    @staticmethod
    def assert_met(results: list[TargetResult]) -> None:
        missed = [r for r in results if r.status == "missed"]
        if missed:
            raise TargetFailure(
                "; ".join(f"{r.name}: observed {r.observed} > target {r.target}" for r in missed)
            )

    def _lift(self, v: TargetVerdict) -> TargetResult:
        return self._result(v.name, v.status, v.observed, v.target)

    def _cmp(self, name: str, observed: float | None, target: float) -> TargetResult:
        if observed is None:
            return self._result(name, "not_measured", None, float(target))
        return self._result(
            name, "met" if observed <= target else "missed", observed, float(target)
        )

    def _result(
        self, name: str, status: str, observed: float | None, target: float
    ) -> TargetResult:
        waiver = self.waivers.get(name)
        if status == "missed" and waiver:
            return TargetResult(name, "waived", observed, target, waiver)
        return TargetResult(name, status, observed, target)  # type: ignore[arg-type]


def _per(count: int | None, reads: int | None) -> float | None:
    if count is None or not reads or reads <= 0:
        return None
    return count / reads
