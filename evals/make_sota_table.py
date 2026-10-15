"""Build the LoCoMo results table in the published-systems format.

    python evals/make_sota_table.py [run_id ...] > table.md

Reads ``runs/<run_id>--memspine/results.jsonl`` (default: every ``qa-full-*`` run). Columns follow how
Mem0 / MemOS / Hindsight / Zep / Mnemon report LoCoMo: LLM-judge accuracy per question type
(single-hop, multi-hop, temporal, open-domain), overall accuracy, mean context tokens per question and
latency. LoCoMo categories: 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop (adversarial cat 5 excluded,
as in the published 1,540-question protocol).
"""

from __future__ import annotations

import glob
import json
import statistics
import sys
from pathlib import Path

RUNS = Path(__file__).parent / "runs"
CATS = [("cat4", "Single-hop"), ("cat1", "Multi-hop"), ("cat2", "Temporal"), ("cat3", "Open-domain")]

# Published reference rows (LoCoMo LLM-judge %, mean context tokens per question). Numbers only as stated in the
# sources named in the last column; none were re-run here, and judges/readers differ from this local stack.
PUBLISHED = [
    ("Mem0 (vendor page, Apr 2026)", "92.5", "~6,956", "vendor-run; scraped_mem0_benchmark_2026.md"),
    ("Mnemon (gpt-4.1-mini)", "91.7", "3.8k", "arXiv 2609.36059, per SOTA_SYSTEMS_UPDATE_2026-10-02.md"),
    ("MemOS (OmniMemEval)", "88.83", "5.4k", "vendor-run harness, via Mnemon Table 1"),
    ("Cognee (OmniMemEval)", "83.48", "n/r", "OmniMemEval, via Mnemon Table 1"),
    ("EverMemOS (OmniMemEval)", "82.75", "n/r", "OmniMemEval, via Mnemon Table 1"),
    ("Hindsight (OmniMemEval)", "81.99", "24.7k", "OmniMemEval, via Mnemon Table 1"),
    ("Mem0 OSS (OmniMemEval)", "77.68", "17.4k", "OmniMemEval, via Mnemon Table 1"),
    ("Letta (OmniMemEval)", "77.12", "n/r", "OmniMemEval, via Mnemon Table 1"),
    ("Zep (OmniMemEval)", "63.83", "1.9k", "OmniMemEval, via Mnemon Table 1"),
    ("A-Mem (gpt-4.1-mini, Mem++)", "61.4", "n/r", "third-party, SOTA_SYSTEMS_UPDATE_2026-10-02.md"),
]


def pct(vals: list[float]) -> str:
    return f"{100 * sum(vals) / len(vals):.1f}" if vals else "n/a"


def load(run_id: str) -> list[dict]:
    path = RUNS / f"{run_id}--memspine" / "results.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("kind") == "result":
            rows.append(d)
    return rows


def pctile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def main() -> None:
    ids = sys.argv[1:] or sorted(Path(p).name.removesuffix("--memspine") for p in glob.glob(str(RUNS / "qa-full-*--memspine")))
    print("| System (local stack: Qwen3.5-9B Q4_K_M reader + judge) | Single-hop | Multi-hop | Temporal | Open-domain | **Overall** | Ctx tok/q | Answer p50 / p95 (ms) | Answered / total |")
    print("|---|---|---|---|---|---|---|---|---|")
    for rid in ids:
        rows = load(rid)
        done = [r for r in rows if r["status"] == "completed"]
        # An unanswered or truncated question is a miss, as in the published protocols.
        per = {c: [r["score"] if r["status"] == "completed" else 0.0 for r in rows if r["type_label"] == c] for c, _ in CATS}
        overall = [v for vals in per.values() for v in vals]
        lat = [r.get("latency_answer_ms") or 0 for r in done]
        ctx = [r.get("context_tokens") or 0 for r in done]
        print(
            f"| {rid.removeprefix('qa-full-')} | "
            + " | ".join(pct(per[c]) for c, _ in CATS)
            + f" | **{pct(overall)}** | {statistics.mean(ctx):.0f} | {pctile(lat, .5):.0f} / {pctile(lat, .95):.0f} | {len(done)} / {len(rows)} |"
            if rows else f"| {rid} | no results |"
        )
    print("\n**Published systems, same metric (LLM-judge accuracy %, overall LoCoMo; not re-run, judge and reader differ):**\n")
    print("| System | Overall J | Ctx tok/q | Source |")
    print("|---|---|---|---|")
    for name, j, tok, src in PUBLISHED:
        print(f"| {name} | {j} | {tok} | {src} |")


main()
