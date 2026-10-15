"""Adoption decision: paired-gate verdicts composed with the behavioural report (G-02 sixth gate, G-04).

``paired_gate.py`` is not changed. Each suite entry that needs a paired verdict supplies one (a
``PairedResult`` or its verdict string); the behavioural report is compared with a reference:

* a case that PASSes on the reference and FAILs (or is absent) on the arm is a **behavioural regression**
  and blocks adoption (PROMOTE becomes HOLD with reason "behavioural regression");
* NOT_SUPPORTED never blocks; it is listed;
* a missing set or a missing behavioural report gives INCOMPLETE (the gate refuses).

The reference is, in order: the ``reference`` argument (a report or a ``{case id: status}`` mapping), else
``behavioural_baseline.json`` next to this file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memspine_evals.behavioural import BehaviouralReport, Status

from .suite import Suite, load_suite

BASELINE_PATH = Path(__file__).resolve().parent / "behavioural_baseline.json"
REGRESSION_REASON = "behavioural regression"
SAFETY_REASON = "safety failure"
#: G-04 C: tenant-isolation and viewer cases reject on any FAIL, whatever the reference says.
BLOCKING_SAFETY = ("SAF-TENANT-", "SAF-VIEWER")


@dataclass
class Adoption:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    not_supported: list[str] = field(default_factory=list)
    paired: dict[str, str] = field(default_factory=dict)


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, str]:
    return dict(json.loads(path.read_text(encoding="utf-8"))["statuses"])


def _verdict_of(v: Any) -> str:
    return v if isinstance(v, str) else v.verdict


def regressions(arm: BehaviouralReport, reference: Mapping[str, str]) -> list[str]:
    """Case ids that PASS on the reference and FAIL (or are absent) on the arm."""
    arm_status = arm.statuses()
    out = []
    for cid, ref in sorted(reference.items()):
        if ref != Status.PASS.value:
            continue
        now = arm_status.get(cid)
        if now is None or now == Status.FAIL.value:
            out.append(cid)
    return out


def _aggregate(verdicts: Mapping[str, str]) -> tuple[str, list[str]]:
    vs = set(verdicts.values())
    for worst in ("INCOMPLETE", "INCOMPARABLE", "REJECT", "HOLD"):
        if worst in vs:
            bad = sorted(k for k, v in verdicts.items() if v == worst)
            return worst, [f"{k}: {worst}" for k in bad]
    if "PROMOTE" in vs:
        return "PROMOTE", ["no set rejected or held; at least one promoted"]
    return "NO CHANGE", ["no question flipped on any set"]


def adopt(
    paired: Mapping[str, Any],
    behavioural: BehaviouralReport | None,
    reference: BehaviouralReport | Mapping[str, str] | None = None,
    *,
    suite: Suite | None = None,
) -> Adoption:
    suite = suite or load_suite()
    verdicts = {k: _verdict_of(v) for k, v in paired.items()}
    missing = [i for i in suite.gate_ids() if i not in verdicts]
    if missing:
        return Adoption(
            "INCOMPLETE", [f"missing paired verdict for {', '.join(missing)}"], paired=verdicts
        )
    if behavioural is None:
        return Adoption("INCOMPLETE", ["behavioural report missing (sixth gate)"], paired=verdicts)
    if reference is None:
        ref = load_baseline()
    elif isinstance(reference, BehaviouralReport):
        ref = reference.statuses()
    else:
        ref = dict(reference)
    verdict, reasons = _aggregate({k: verdicts[k] for k in suite.gate_ids()})
    safety = sorted(
        r.id
        for r in behavioural.results
        if r.status is Status.FAIL and r.id.startswith(BLOCKING_SAFETY)
    )
    regs = [c for c in regressions(behavioural, ref) if c not in safety]
    if safety:
        return Adoption(
            "REJECT",
            [SAFETY_REASON + ": " + ", ".join(safety), *reasons],
            regs,
            sorted(r.id for r in behavioural.results if r.status is Status.NOT_SUPPORTED),
            verdicts,
        )
    ns = sorted(r.id for r in behavioural.results if r.status is Status.NOT_SUPPORTED)
    if regs:
        reasons = [*reasons, f"{REGRESSION_REASON}: {', '.join(regs)}"]
        if verdict == "PROMOTE":
            verdict = "HOLD"
            reasons = [REGRESSION_REASON, *reasons]
    if ns:
        reasons = [*reasons, f"not supported (listed, not blocking): {', '.join(ns)}"]
    return Adoption(verdict, reasons, regs, ns, verdicts)
