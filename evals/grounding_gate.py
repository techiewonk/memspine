"""W2-08 gate: grounding of the X-05 labelled set through ``GroundingChecker`` (CPU only, no GPU).

    python evals/grounding_gate.py [--adjudicator lexical|decider|none]

Gold labels: supported / partly / unsupported. The stage keeps ``supported`` and drops (or marks
tentative) ``partly`` and ``unsupported``, so the headline *grounding rate* is the share of items the
stage handles correctly (a supported item is kept, a partly or unsupported one is not), plus the
three-way accuracy. ``undecided`` after adjudication counts as kept (``uncertain``) and is reported.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from labelled.loader import load_grounding

from memspine.derive.ground import Cite, CitedSpan, Claim, WindowTurn
from memspine.derive.grounding_stage import Adjudicator, GroundingChecker

LABELS = ("supported", "partly", "unsupported")


def claim_for(it, spans: bool = False) -> tuple[Claim, dict[str, WindowTurn]]:
    """A faithful extractor cites the support and binding turns; for unsupported items it cites all."""
    w = {t.turn_id: WindowTurn(key=t.turn_id, speaker=t.speaker, text=t.text) for t in it.turns}
    if it.support_turns:
        cites = [Cite(role="support", turn=k) for k in it.support_turns]
        if spans and it.span is not None:
            cites[0] = Cite(
                role="support",
                turn=it.span.turn_id,
                span=CitedSpan(start=it.span.start, end=it.span.end, quote=it.span.text),
            )
        cites += [Cite(role="binding", turn=k) for k in it.binding_turns]
    else:
        cites = [Cite(role="support", turn=k) for k in w]
    on = date.fromisoformat(it.source_time) if it.source_time else None
    return Claim(
        text=it.assertion, modality=it.claimed_modality, source_time=on, cites=tuple(cites)
    ), w


async def run(adjudicator: str, decider=None, spans: bool = False) -> dict:
    adj = None
    if adjudicator == "lexical":
        adj = Adjudicator(None)
    elif adjudicator == "decider":
        adj = Adjudicator(decider)
    checker = GroundingChecker(adjudicator=adj)
    rows = []
    for it in load_grounding().items:
        claim, w = claim_for(it, spans)
        v = await checker.check_claim(claim, w)
        rows.append((it, v))
    conf = Counter((it.label, v.label) for it, v in rows)
    n = len(rows)
    keep = lambda lab: lab in ("supported", "undecided")
    correct = sum(1 for it, v in rows if keep(v.label) == (it.label == "supported"))
    three = sum(1 for it, v in rows if v.label == it.label)
    tp = sum(1 for it, v in rows if v.label == "supported" and it.label == "supported")
    pred_sup = sum(1 for _, v in rows if v.label == "supported")
    gold_sup = sum(1 for it, _ in rows if it.label == "supported")
    drop_tp = sum(1 for it, v in rows if not keep(v.label) and it.label != "supported")
    pred_drop = sum(1 for _, v in rows if not keep(v.label))
    gold_drop = sum(1 for it, _ in rows if it.label != "supported")
    return {
        "n": n,
        "grounding_rate": correct / n,
        "three_way": three / n,
        "supported_precision": tp / pred_sup if pred_sup else None,
        "supported_recall": tp / gold_sup,
        "ungrounded_precision": drop_tp / pred_drop if pred_drop else None,
        "ungrounded_recall": drop_tp / gold_drop,
        "undecided": sum(1 for _, v in rows if v.label == "undecided"),
        "adjudicated": sum(1 for _, v in rows if v.adjudicated is not None),
        "conf": conf,
        "wrong": [(it, v) for it, v in rows if keep(v.label) != (it.label == "supported")],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adjudicator", default="lexical", choices=["none", "lexical", "decider"])
    ap.add_argument("--spans", action="store_true", help="the extractor also cites the gold span")
    a = ap.parse_args()
    decider = None
    if a.adjudicator == "decider":
        from memspine.providers.decider import build_decider

        decider = build_decider("opendecider", device="cpu")
    r = asyncio.run(run(a.adjudicator, decider, a.spans))
    for k, v in r.items():
        if k not in ("conf", "wrong"):
            print(f"{k}: {v if not isinstance(v, float) else round(v, 3)}")
    print("confusion:", dict(r["conf"]))
    for it, v in r["wrong"]:
        print(
            f"  WRONG {it.id} [{it.category}/{it.lang}] gold {it.label}/{it.failure} got {v.label} {list(v.reasons)[:2]}"
        )


if __name__ == "__main__":
    main()
