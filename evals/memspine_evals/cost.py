"""Attach the engine's write-side cost ledger to a screen run's results (W2-12; W-26, P-14, P-22).

``economy.ledger_v2`` makes ``Engine.cost_report(namespace)`` return model calls, tokens in and
out, latency and embed calls per layer, stage and namespace. ``cost_block`` turns that report plus
the P-14 target verdicts into one plain dict for a result row, a ``meta`` entry or the summary;
``attach_cost`` puts it under ``write_cost``. Both return nothing new when the engine runs with
the flag off (an empty report), so an arm without the ledger is unchanged.

Targets need denominators the engine report does not hold (turns, windows); the caller passes
them. The verdicts are plain strings and numbers (``met`` / ``missed`` / ``waived`` /
``not_measured``) so a manifest check can refuse a run whose target is missed without a waiver.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

__all__ = ["attach_cost", "cost_block", "unwaived_misses"]


def cost_block(
    engine: Any,
    namespace: str | None = None,
    *,
    turns: int | None = None,
    windows: int | None = None,
    waivers: Mapping[str, str] | None = None,
    static_prompt_tokens: int | None = None,
) -> dict[str, Any]:
    """The ledger report of ``engine`` (optionally for one namespace) with target verdicts.

    ``{}`` when the engine has no ``cost_report`` or the ledger is off. ``targets`` is present
    only when ``turns`` or ``windows`` is given (the denominators)."""
    report_fn = getattr(engine, "cost_report", None)
    if not callable(report_fn):
        return {}
    raw = report_fn(namespace)
    report = dict(raw) if isinstance(raw, Mapping) else {}
    if not report:
        return {}
    block: dict[str, Any] = {"ledger": report}
    ledger = getattr(engine, "_cost_ledger", None)
    if ledger is not None and (turns is not None or windows is not None):
        from memspine.economy.targets import TargetSet

        results = TargetSet(waivers=dict(waivers or {})).check(
            ledger, turns=turns, windows=windows, static_prompt_tokens=static_prompt_tokens
        )
        block["targets"] = [
            {
                "name": r.name,
                "status": r.status,
                "observed": r.observed,
                "target": r.target,
                **({"waiver": r.waiver} if r.waiver else {}),
            }
            for r in results
        ]
    return block


def attach_cost(
    results: MutableMapping[str, Any], engine: Any, namespace: str | None = None, **kwargs: Any
) -> MutableMapping[str, Any]:
    """Put ``cost_block(...)`` under ``results["write_cost"]`` (nothing when the ledger is off)."""
    block = cost_block(engine, namespace, **kwargs)
    if block:
        results["write_cost"] = block
    return results


def unwaived_misses(block: Mapping[str, Any]) -> list[str]:
    """Names of the targets a ``cost_block`` records as missed without a waiver (P-14 gate)."""
    return [t["name"] for t in block.get("targets", []) if t.get("status") == "missed"]
