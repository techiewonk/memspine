"""E01 / E02 offline estimate: how often the fact-chain step and the slot loop would fire, and
what they would cost in LLM calls, on a finished run's saved reader rows.

No model is called and nothing is changed. For each saved row the triggers are evaluated
exactly as the engine does:

* ``read.agentic_trigger: multi_hop`` -> :func:`memspine.core.query_shape.is_multi_hop` on the
  question (the slot loop fires on the same trigger);
* ``read.fact_chain_trigger: triggered`` -> multi-hop OR the A03 contract's relation is not
  stated beside its subject in any of the first ``FACT_CHAIN_MAX_LINES`` context lines
  (:func:`memspine.core.fact_chain.relation_unresolved`).

The run's own verdict (the judge's score on the row) is read only to SCORE the triggers (how
many wrong answers they cover); it is never an input to them (rule I37).

Cost: the fact-chain step is exactly one ``extract@assertions`` call per fired question; the
slot loop is one ``sufficiency@agentic_slot`` call per step, between 1 (answer / qualified stop
at step 0) and ``agentic_max_steps`` per fired question.

    python evals/slotloop_offline.py <run>--trace/reads.jsonl.gz [--max-steps 2] [--json out.json]
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

_LINE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}[^\]]*\]\s*")


def _rows(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf8") as fh:  # type: ignore[operator]
        return [json.loads(line) for line in fh if line.strip()]


def _correct(row: dict[str, Any]) -> bool | None:
    verdict = row.get("verdict")
    if isinstance(verdict, dict) and verdict.get("score") is not None:
        return float(verdict["score"]) >= 0.5
    return None


def _lines(row: dict[str, Any], limit: int) -> list[str]:
    text = row.get("context_text") or ""
    out = [_LINE.sub("", ln).strip() for ln in text.splitlines() if ln.strip()]
    return out[:limit]


def estimate(rows: list[dict[str, Any]], max_steps: int = 2) -> dict[str, Any]:
    from memspine.config import constants
    from memspine.core.fact_chain import relation_unresolved
    from memspine.core.query_contract import build_contract
    from memspine.core.query_shape import is_multi_hop

    total = collections.Counter()
    by_type: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    line_chars: list[int] = []
    for row in rows:
        ok = _correct(row)
        if ok is None:
            continue
        question = row.get("question", "")
        lines = _lines(row, constants.FACT_CHAIN_MAX_LINES)
        contract = build_contract(question)
        multi = is_multi_hop(question)
        unresolved = relation_unresolved(contract, lines)
        flags = {
            "multi_hop": multi,
            "relation_unresolved": unresolved,
            "chain_fires": multi or unresolved,
        }
        kind = row.get("type_label") or "all"
        for scope in ("all", kind):
            c = total if scope == "all" else by_type[scope]
            c["questions"] += 1
            c["wrong"] += not ok
            for name, hit in flags.items():
                if hit:
                    c[name] += 1
                    c[f"{name}_wrong"] += not ok
        if flags["chain_fires"]:
            line_chars.append(sum(min(len(ln), constants.FACT_CHAIN_LINE_CHARS) for ln in lines))

    def summarise(c: collections.Counter) -> dict[str, Any]:
        n = c["questions"] or 1
        out: dict[str, Any] = {"questions": c["questions"], "wrong": c["wrong"]}
        for name in ("multi_hop", "relation_unresolved", "chain_fires"):
            out[name] = {
                "fires": c[name],
                "share": round(c[name] / n, 4),
                "wrong_covered": c[f"{name}_wrong"],
                "precision_wrong": round(c[f"{name}_wrong"] / c[name], 4) if c[name] else None,
                "recall_wrong": round(c[f"{name}_wrong"] / c["wrong"], 4) if c["wrong"] else None,
            }
        share_slot = c["multi_hop"] / n
        share_chain = c["chain_fires"] / n
        out["calls_per_question"] = {
            "slot_loop": {"min": round(share_slot, 3), "max": round(share_slot * max_steps, 3)},
            "fact_chain": round(share_chain, 3),
            "both": {
                "min": round(share_slot + share_chain, 3),
                "max": round(share_slot * max_steps + share_chain, 3),
            },
        }
        return out

    result = summarise(total)
    result["by_type"] = {k: summarise(v) for k, v in sorted(by_type.items())}
    result["max_steps"] = max_steps
    if line_chars:
        result["fact_chain_prompt_chars_mean"] = round(sum(line_chars) / len(line_chars))
        result["fact_chain_prompt_tokens_est_mean"] = round(sum(line_chars) / len(line_chars) / 4)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("reads", type=Path)
    ap.add_argument("--max-steps", type=int, default=2)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    result = estimate(_rows(args.reads), args.max_steps)
    text = json.dumps(result, indent=1)
    if args.json:
        args.json.write_text(text, encoding="utf8")
    json.dump(result, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
