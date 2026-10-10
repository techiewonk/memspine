"""A03 / E06 offline estimate: run the deterministic query contract and the code answer
checks over a finished run's saved reader rows and count what they would flag.

No model is called and nothing is changed. For each saved answer the contract is built from the
question and :func:`memspine.core.answer_check.check_answer` runs on the answer text. The run's
own verdict (the judge's score on that row) says whether the answer was right, so the output
splits the flags into

* true flags: the answer was wrong and a check fired (the repair call would get a chance);
* false flags: the answer was right and a check fired (the repair call must leave it alone,
  the risk to measure in a paid screen).

The verdict is read only to SCORE the checks; it is never an input to them (rule I37). The
repair call itself cannot be evaluated offline.

    python evals/contract_offline.py <run>--trace/reads.jsonl.gz [--json out.json]
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys
from pathlib import Path
from typing import Any


def _rows(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf8") as fh:  # type: ignore[operator]
        return [json.loads(line) for line in fh if line.strip()]


def _correct(row: dict[str, Any]) -> bool | None:
    verdict = row.get("verdict")
    if isinstance(verdict, dict) and verdict.get("score") is not None:
        return float(verdict["score"]) >= 0.5
    return None


def estimate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from memspine.core.answer_check import check_answer, is_abstention
    from memspine.core.query_contract import build_contract

    total = collections.Counter()
    by_type: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_kind: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    examples: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for row in rows:
        ok = _correct(row)
        if ok is None:
            continue
        contract = build_contract(row.get("question", ""))
        answer = row.get("answer") or ""
        abstained = is_abstention(answer)
        both = [] if abstained else check_answer(contract, answer, abstained=False, soft=True)
        strict = [d for d in both if not d.kind.startswith("soft_")]
        soft = [d for d in both if d.kind.startswith("soft_")]
        total["rows"] += 1
        total["correct" if ok else "wrong"] += 1
        total["abstained"] += abstained
        total["abstained_wrong"] += abstained and not ok
        total["typed"] += contract.known
        by_type[contract.type_label]["rows"] += 1
        by_type[contract.type_label]["wrong"] += not ok
        for tier, defects in (("strict", strict), ("soft", soft), ("any", both)):
            if defects:
                total[f"{tier}_{'true' if not ok else 'false'}"] += 1
        for d in both:
            key = "true_flags" if not ok else "false_flags"
            by_kind[d.kind][key] += 1
            if not ok and len(examples[d.kind]) < 6:
                examples[d.kind].append(
                    {"q": row["question"], "a": answer[:120], "gold": str(row.get("gold"))[:80]}
                )
        if both:
            by_type[contract.type_label]["true_flags" if not ok else "false_flags"] += 1
    wrong, correct = total["wrong"], total["correct"]

    def tier(name: str) -> dict[str, Any]:
        t, f = total[f"{name}_true"], total[f"{name}_false"]
        return {
            "true_flags": t,
            "false_flags": f,
            "true_flag_share_of_wrong": round(t / wrong, 4) if wrong else None,
            "false_flag_share_of_correct": round(f / correct, 4) if correct else None,
            "precision": round(t / (t + f), 4) if t + f else None,
        }

    return {
        "rows": total["rows"],
        "correct": correct,
        "wrong": wrong,
        "typed_questions": total["typed"],
        "abstained_answers": total["abstained"],
        "abstained_and_wrong": total["abstained_wrong"],
        "strict": tier("strict"),
        "soft": tier("soft"),
        "strict_or_soft": tier("any"),
        "by_defect_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "by_answer_type": {k: dict(v) for k, v in sorted(by_type.items())},
        "wrong_examples": dict(examples),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("reads", type=Path)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)
    out = estimate(_rows(args.reads))
    print(json.dumps({k: v for k, v in out.items() if k != "wrong_examples"}, indent=1))
    if args.json:
        args.json.write_text(json.dumps(out, indent=1), encoding="utf8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
