"""Project the token and dollar cost of a baseline LoCoMo + LongMemEval run.

Why this exists. The evaluation track is blocked on a budget decision that was posed
without a number, which makes it unanswerable. This turns it into a yes/no: it prints
what a baseline actually costs under the lean profile, and what each optional layer
adds.

What makes the estimate tractable. The lean profile binds no LLM role, so the write
path makes **zero model calls**: ingestion cost is embeddings only, which run locally
on CPU. Model spend is therefore confined to the read side -- one reader call per
question, plus one judge call per question -- and that is a small, well-defined
quantity.

Every figure below is an input assumption, printed with the result so it can be
argued with. This is an estimate, not a measurement; nothing here has been run.

    python -m evals.estimate_cost
    python -m evals.estimate_cost --reader gpt-4.1-mini --judge gpt-4o
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class Dataset:
    name: str
    questions: int
    ingest_tokens: int          # total corpus tokens written, across all samples
    context_per_question: int   # retrieved context handed to the reader
    answer_tokens: int


#: Published corpus shapes. LoCoMo: 10 conversations, ~300 turns, 1,540 questions.
#: LongMemEval-S: 500 questions over ~115K-token histories.
DATASETS = (
    Dataset("LoCoMo (full 1,540)", 1_540, 10 * 300 * 180, 4_000, 120),
    Dataset("LongMemEval-S (500)", 500, 500 * 115_000 // 50, 4_000, 120),
)

#: USD per 1M tokens (input, output). Update before quoting; these are list prices.
PRICES = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

#: Optional layers, as a multiplier on write-path model spend. The lean profile is 0:
#: no LLM role is bound, so these are the cost of turning a layer on.
LAYERS = {
    "entity extraction (local NER)": ("no model calls; CPU only", 0.0),
    "entity extraction (LLM)": ("one call per written record", 1.0),
    "graph extraction (extract_graph)": ("one call per record, plus edges", 1.4),
    "conflict ladder (LLM arm)": ("one call per conflicting write only", 0.2),
    "consolidation (deterministic)": ("extractive summariser; no model calls", 0.0),
}


def money(tokens_in: int, tokens_out: int, model: str) -> float:
    rate_in, rate_out = PRICES[model]
    return tokens_in / 1_000_000 * rate_in + tokens_out / 1_000_000 * rate_out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader", default="gpt-4.1-mini", choices=sorted(PRICES))
    parser.add_argument("--judge", default="gpt-4o-mini", choices=sorted(PRICES))
    parser.add_argument("--runs", type=int, default=1,
                        help="repeat count, for variance estimates")
    args = parser.parse_args()

    print("Baseline cost projection -- LEAN PROFILE (no LLM role bound)\n")
    print(f"  reader: {args.reader}   judge: {args.judge}   runs: {args.runs}")
    print("  write path: 0 model calls. Embeddings are local CPU (fastembed ONNX).\n")

    grand = 0.0
    total_questions = 0
    for dataset in DATASETS:
        reader_in = dataset.questions * dataset.context_per_question
        reader_out = dataset.questions * dataset.answer_tokens
        # The judge sees question, gold and prediction: far smaller than the context.
        judge_in = dataset.questions * (dataset.answer_tokens * 2 + 200)
        judge_out = dataset.questions * 10

        reader_cost = money(reader_in, reader_out, args.reader)
        judge_cost = money(judge_in, judge_out, args.judge)
        subtotal = (reader_cost + judge_cost) * args.runs
        grand += subtotal
        total_questions += dataset.questions * args.runs

        print(f"  {dataset.name}")
        print(f"    questions        {dataset.questions:,}")
        print(f"    ingest tokens    {dataset.ingest_tokens:,}  (embedded locally, $0)")
        print(f"    reader tokens    {reader_in:,} in / {reader_out:,} out   ${reader_cost:,.2f}")
        print(f"    judge tokens     {judge_in:,} in / {judge_out:,} out   ${judge_cost:,.2f}")
        print(f"    subtotal         ${subtotal:,.2f}\n")

    print(f"  BASELINE TOTAL       ${grand:,.2f}   ({total_questions:,} judged questions)\n")

    print("  Ablation matrix. Each read-side toggle re-runs the read path only,")
    print("  because the build cache keys on the write configuration:")
    for count in (5, 9):
        print(f"    {count} read-side rows: ${grand * count:,.2f}")

    print("\n  Write-side layers, as a multiple of one baseline ingest:")
    for name, (note, factor) in LAYERS.items():
        if factor == 0.0:
            print(f"    {name:34s} $0        ({note})")
        else:
            # One call per written record, ~1.5K in / 150 out at reader prices.
            records = sum(d.ingest_tokens for d in DATASETS) // 400
            cost = money(int(records * 1_500 * factor), int(records * 150 * factor), args.reader)
            print(f"    {name:34s} ${cost:,.2f}   ({note})")

    print("\n  Assumptions, all arguable and all inputs to the numbers above:")
    print("    - 4,000-token retrieved context per question")
    print("    - 120-token answers; judge sees answer, gold and question only")
    print("    - ~400 tokens per written memory record")
    print("    - list prices, no batch discount, no caching credit")
    print("    - a failed run costs the same as a good one; budget for two")
    print("\n  NOT a measurement. Nothing here has been executed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
