"""I13: judge agreement with the hand-checked calibration set. Offline, no model call.

    python evals/judge_agreement.py --run <run-name> [--runs-dir evals/runs] [--set evals/analysis/judge_calibration_dev.jsonl]
    python evals/judge_agreement.py --labels judge_labels.jsonl       # {"id": "conv-26/0-88", "label": "CORRECT"} per line
    add --json for machine-readable output, --fail-below KAPPA to gate a script.

``--run`` reads the judge outputs of a finished run (``report/per_question.jsonl``, else
``results.jsonl``) and compares them with the human verdict, but only on rows where the run's
answer text equals the calibration answer (a different reader gives a different answer, which
the human label does not cover); the count of unmatched rows is reported. ``--labels`` is for a
judge run over the calibration rows themselves (question, gold, answer fed to the judge), which
covers all of them.

Reported: binary agreement (judge CORRECT vs human CORRECT), Cohen's kappa, the confusion, false
negatives (judge WRONG, human CORRECT) and false positives (judge CORRECT, human WRONG), the rate
at which the judge credits PARTIAL answers (over-crediting of partial lists), the refusal credit
rate, all per ``kind``, with and without the ``borderline`` rows. PARTIAL rows are excluded from
kappa (the judge is binary) and reported as the credit rate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_SET = Path(__file__).parent / "analysis" / "judge_calibration_dev.jsonl"


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def load_set(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_labels(run_dir: Path) -> dict[str, tuple[str, str]]:
    """``id -> (judge label, answer text)`` from a finished run's judge outputs."""
    out: dict[str, tuple[str, str]] = {}
    report = run_dir / "report" / "per_question.jsonl"
    if report.exists():
        for line in report.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            out[f"{r['item']}/{r['qid']}"] = ("CORRECT" if r.get("correct") else "WRONG", r.get("answer") or "")
        return out
    for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("kind") != "result":
            continue
        score = r.get("score")
        out[f"{r['item_id']}/{r['query_id']}"] = ("CORRECT" if (score or 0) >= 1 else "WRONG", r.get("answer") or "")
    return out


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Kappa for two binary raters over ``(human, judge)`` pairs; None when undefined."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(h == j for h, j in pairs) / n
    ph = sum(h for h, _ in pairs) / n
    pj = sum(j for _, j in pairs) / n
    pe = ph * pj + (1 - ph) * (1 - pj)
    return None if pe == 1 else (po - pe) / (1 - pe)


def score(rows: list[dict[str, Any]], judged: dict[str, str]) -> dict[str, Any]:
    """Agreement of ``judged`` (id -> label) with the human verdicts in ``rows``."""
    def block(sub: list[dict[str, Any]]) -> dict[str, Any]:
        binary = [(r["human_verdict"] == "CORRECT", judged[r["id"]] == "CORRECT") for r in sub if r["human_verdict"] != "PARTIAL"]
        partial = [r for r in sub if r["human_verdict"] == "PARTIAL"]
        refusals = [r for r in sub if r["kind"] in ("refusal", "hedged_refusal")]
        fn = sum(h and not j for h, j in binary)
        fp = sum(j and not h for h, j in binary)
        return {
            "n": len(sub),
            "n_binary": len(binary),
            "agreement": (sum(h == j for h, j in binary) / len(binary)) if binary else None,
            "kappa": cohen_kappa(binary),
            "false_negatives": fn,
            "false_positives": fp,
            "partial_credit_rate": (sum(judged[r["id"]] == "CORRECT" for r in partial) / len(partial)) if partial else None,
            "n_partial": len(partial),
            "refusal_credit_rate": (sum(judged[r["id"]] == "CORRECT" for r in refusals) / len(refusals)) if refusals else None,
            "n_refusal": len(refusals),
        }

    rows = [r for r in rows if r["id"] in judged]
    firm = [r for r in rows if not r.get("borderline")]
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)
    return {
        "all": block(rows),
        "without_borderline": block(firm),
        "by_kind": {k: _kind_block(v, judged) for k, v in sorted(by_kind.items())},
    }


def _kind_block(sub: list[dict[str, Any]], judged: dict[str, str]) -> dict[str, Any]:
    c = Counter((r["human_verdict"], judged[r["id"]]) for r in sub)
    return {"n": len(sub), "human_vs_judge": {f"{h}/{j}": n for (h, j), n in sorted(c.items())}}


def with_conventions(rows: list[dict[str, Any]], judged: dict[str, str]) -> dict[str, Any]:
    """I58: agreement of ``judge OR convention`` with the human verdicts, the credits the layer
    gave (each with the human verdict, so a false positive is visible), and the layer alone."""
    sys.path.insert(0, str(Path(__file__).parent))
    from memspine_evals.judge_conventions import check_conventions

    rows = [r for r in rows if r["id"] in judged]
    credited: dict[str, str] = {}
    layered = dict(judged)
    alone = {r["id"]: "WRONG" for r in rows}
    credits = []
    for r in rows:
        hit = check_conventions(r["question"], r["answer"], r["gold"])
        if hit is not None:
            alone[r["id"]] = "CORRECT"
            credits.append({"id": r["id"], "rule": hit.rule, "human": r["human_verdict"],
                            "judge": judged[r["id"]], "new": judged[r["id"]] != "CORRECT"})
            if judged[r["id"]] != "CORRECT":
                layered[r["id"]] = "CORRECT"
                credited[r["id"]] = hit.rule
    new = [c for c in credits if c["new"]]
    return {
        "judge_alone": score(rows, judged),
        "judge_or_conventions": score(rows, layered),
        "conventions_alone": score(rows, alone),
        "credits": credits,
        "new_credits": len(new),
        "new_false_positives": sum(c["human"] != "CORRECT" for c in new),
        "new_true_positives": sum(c["human"] == "CORRECT" for c in new),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default=str(DEFAULT_SET))
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run")
    src.add_argument("--labels")
    src.add_argument("--from-set", action="store_true",
                     help="use the judge label stored in the set (source_judge_label)")
    ap.add_argument("--runs-dir", default=str(Path(__file__).parent / "runs"))
    ap.add_argument("--conventions", action="store_true",
                    help="I58: also report the judge OR the deterministic convention layer "
                    "(judge_conventions.py), next to the judge alone, and list every credit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-below", type=float, default=None, help="exit 1 when kappa (all rows) is below this")
    args = ap.parse_args(argv)
    rows = load_set(Path(args.set))
    unmatched = 0
    if args.run:
        got = run_labels(Path(args.runs_dir) / f"{args.run}--memspine")
        judged: dict[str, str] = {}
        for r in rows:
            hit = got.get(r["id"])
            if hit and _norm(hit[1]) == _norm(r["answer"]):
                judged[r["id"]] = hit[0]
            else:
                unmatched += 1
    elif args.from_set:
        judged = {r["id"]: r["source_judge_label"] for r in rows}
    else:
        judged = {}
        for line in Path(args.labels).read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                judged[d["id"]] = "CORRECT" if str(d["label"]).upper() == "CORRECT" else "WRONG"
        unmatched = sum(r["id"] not in judged for r in rows)
    res = score(rows, judged)
    res["set_size"] = len(rows)
    res["unmatched"] = unmatched
    if args.conventions:
        res["conventions"] = with_conventions(rows, judged)
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        print(f"calibration items {len(rows)}, compared {len(rows) - unmatched}, unmatched {unmatched}")
        for name in ("all", "without_borderline"):
            b = res[name]
            kappa = "n/a" if b["kappa"] is None else f"{b['kappa']:.2f}"
            agree = "n/a" if b["agreement"] is None else f"{b['agreement']:.1%}"
            print(f"[{name}] n={b['n']} binary n={b['n_binary']} agreement={agree} kappa={kappa} "
                  f"FN={b['false_negatives']} FP={b['false_positives']} "
                  f"partial credited={b['partial_credit_rate']} (n={b['n_partial']}) "
                  f"refusal credited={b['refusal_credit_rate']} (n={b['n_refusal']})")
        for k, v in res["by_kind"].items():
            print(f"  {k:16s} n={v['n']:2d} {v['human_vs_judge']}")
    if "conventions" in res:
        c = res["conventions"]
        for name in ("judge_alone", "judge_or_conventions", "conventions_alone"):
            for sub in ("all", "without_borderline"):
                b = c[name][sub]
                kappa = "n/a" if b["kappa"] is None else f"{b['kappa']:.3f}"
                agree = "n/a" if b["agreement"] is None else f"{b['agreement']:.1%}"
                print(f"[{name} / {sub}] binary n={b['n_binary']} agreement={agree} kappa={kappa} "
                      f"FN={b['false_negatives']} FP={b['false_positives']} "
                      f"partial credited={b['partial_credit_rate']} (n={b['n_partial']})")
        print(f"conventions: {c['new_credits']} new credits over the judge "
              f"({c['new_true_positives']} human CORRECT, {c['new_false_positives']} not); "
              f"all credits: {c['credits']}")
    kappa_all = res["all"]["kappa"]
    if args.fail_below is not None and (kappa_all is None or kappa_all < args.fail_below):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
