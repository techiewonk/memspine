"""Command line for the harness. ``python -m memspine_evals <command>``.

Stdlib argparse only: the CLI must work in a bare environment, because the
whole point of the baselines is that they need nothing installed.

    python -m memspine_evals smoke
    python -m memspine_evals c0-1 --dataset locomo --path data/locomo10.json --revision auto
    python -m memspine_evals c0-1 --dataset longmemeval --path data/longmemeval_s.json \
        --revision 2025-09-cleaned --mode qa --reader-model qwen3:4b --max-model-calls 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .contracts import DatasetAdapter
from .experiments import C01Config, comparison_table, run_c0_1
from .results import append_score_matrix_rows

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "runs"


def _dataset(args: argparse.Namespace) -> DatasetAdapter:
    if args.dataset == "synthetic":
        from .datasets import SyntheticDataset

        return SyntheticDataset(n_items=args.items or 3, turns_per_item=24)
    if not args.path:
        raise SystemExit(f"--path is required for --dataset {args.dataset}")
    if args.dataset == "locomo":
        from .datasets import LoCoMoDataset

        return LoCoMoDataset(args.path, revision_id=args.revision)
    if args.dataset == "longmemeval":
        from .datasets import LongMemEvalDataset

        return LongMemEvalDataset(args.path, revision_id=args.revision, variant=args.variant)
    raise SystemExit(f"unknown dataset {args.dataset!r}")


def cmd_smoke(args: argparse.Namespace) -> int:
    """Prove the loop end to end with no data, no models, no network."""
    from .datasets import SyntheticDataset

    dataset = SyntheticDataset(n_items=2, turns_per_item=16, facts_per_item=3)
    config = C01Config(mode="retrieval", budget_tokens=1000, top_k=5)
    summaries = asyncio.run(
        run_c0_1(dataset, config, Path(args.out), run_id=f"smoke-{dataset.info().revision_id}")
    )
    print(comparison_table(summaries, "retrieval"))
    print()
    for summary in summaries:
        print(summary.headline())
    return 0


def cmd_c0_1(args: argparse.Namespace) -> int:
    dataset = _dataset(args)
    config = C01Config(
        mode=args.mode,
        budget_tokens=args.budget,
        top_k=args.top_k,
        seed=args.seed,
        dense=args.dense,
        hybrid=args.hybrid,
        reader_model=args.reader_model,
        judge_model=args.judge_model or args.reader_model,
        base_url=args.base_url,
        include_memspine=args.with_memspine,
        max_items=args.items,
        max_model_calls=args.max_model_calls,
        bedrock=args.bedrock,
        memspine_config=json.loads(args.memspine_config) if args.memspine_config else None,
        memspine_read_mode=args.memspine_read_mode,
        qa_prompt=args.qa_prompt,
        only_systems=tuple(args.only_systems.split(",")) if args.only_systems else None,
        item_ids=tuple(args.item_ids.split(",")) if args.item_ids else None,
    )
    if config.bedrock:
        from .bedrock import load_aws_credentials

        # AWS keys + region ONLY; nothing else in the repo .env is read.
        load_aws_credentials(Path(__file__).resolve().parents[2] / ".env")
    if config.mode == "qa" and args.max_model_calls is None:
        raise SystemExit(
            "qa mode calls models — pass --max-model-calls with a cap you have agreed to. "
            "An uncapped run is how a benchmark bill becomes a surprise."
        )
    info = dataset.info()
    run_id = args.run_id or f"c0-1-{config.mode}-{info.dataset_id}-{info.revision_id}"
    run_id = run_id.replace(":", "-").replace("/", "-")
    summaries = asyncio.run(run_c0_1(dataset, config, Path(args.out), run_id=run_id))

    table = comparison_table(summaries, config.mode)
    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    header = (
        f"# C0-1 — verbatim baseline gate\n\n"
        f"- dataset: `{info.dataset_id}` revision `{info.revision_id}` "
        f"({info.n_items} items, {info.n_queries} queries)\n"
        f"- mode: **{config.mode}** | budget: {config.budget_tokens} tokens | top_k: {config.top_k}"
        f" | seed: {config.seed} | retriever: {'dense' if config.dense else 'bm25 lexical'}\n"
        f"- reader: `{config.reader_model if config.mode == 'qa' else 'none (retrieval only)'}`\n\n"
    )
    (out_dir / "COMPARISON.md").write_text(header + table + "\n", encoding="utf-8")

    if args.score_matrix:
        rows = []
        for summary in summaries:
            payload = json.loads(
                (Path(args.out) / summary.run_id / "summary.json").read_text(encoding="utf-8")
            )
            rows.append(payload["score_matrix_row"])
        written = append_score_matrix_rows(Path(args.score_matrix), rows)
        print(f"appended {written} rows to {args.score_matrix}")

    print(table)
    print(f"\nwritten: {out_dir / 'COMPARISON.md'}")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    """Reserve development items before tuning, and write the ids to a file."""
    from .split import make_split

    dataset = _dataset(args)
    split = make_split(dataset, n_dev_items=args.dev_items, seed=args.seed, note=args.note)
    path = split.save(args.output)
    print(f"dataset:  {split.dataset_id} @ {split.revision_id} (sha {split.content_sha256[:12]})")
    print(f"dev:      {len(split.dev_items)} items -> {', '.join(split.dev_items[:5])}")
    print(f"heldout:  {len(split.heldout_items)} items")
    print(f"balance:  dev={split.category_balance['dev']}")
    print(f"written:  {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memspine_evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    smoke = sub.add_parser("smoke", help="synthetic end-to-end check, no data or models")
    smoke.add_argument("--out", default=str(DEFAULT_OUT))
    smoke.set_defaults(func=cmd_smoke)

    c01 = sub.add_parser("c0-1", help="verbatim-baseline gate (Plan C section 1.1)")
    c01.add_argument("--dataset", choices=("locomo", "longmemeval", "synthetic"), required=True)
    c01.add_argument("--path", help="path to the dataset json")
    c01.add_argument(
        "--revision",
        default="auto",
        help="data revision id; 'auto' hashes the file. LongMemEval REQUIRES this to be meaningful",
    )
    c01.add_argument("--variant", default="s", help="longmemeval variant: s | m | oracle")
    c01.add_argument("--mode", choices=("retrieval", "qa"), default="retrieval")
    c01.add_argument(
        "--budget",
        type=int,
        default=4096,
        help="retrieved-token cap; 4096 is the evaluation plan primary setting",
    )
    c01.add_argument("--top-k", type=int, default=10)
    c01.add_argument("--seed", type=int, default=11)
    c01.add_argument("--dense", action="store_true", help="use fastembed instead of BM25")
    c01.add_argument("--hybrid", action="store_true", help="RRF fusion of BM25 and fastembed")
    c01.add_argument("--with-memspine", action="store_true")
    c01.add_argument("--items", type=int, default=None, help="cap items (pilot runs)")
    c01.add_argument("--reader-model", default="qwen3:4b")
    c01.add_argument("--judge-model", default=None)
    c01.add_argument("--base-url", default="http://localhost:11434/v1")
    c01.add_argument("--max-model-calls", type=int, default=None)
    c01.add_argument(
        "--bedrock", action="store_true", help="D23 Qwen3 protocol: Bedrock Qwen3 reader + judge"
    )
    c01.add_argument(
        "--memspine-config", default=None, help="JSON engine overrides for the memspine arm"
    )
    c01.add_argument("--only-systems", default=None, help="comma list of system ids to run")
    c01.add_argument(
        "--qa-prompt",
        choices=("default", "dated", "abstain"),
        default="default",
        help="QA prompt variant for every arm (H7/H12)",
    )
    c01.add_argument("--item-ids", default=None, help="comma list of item ids (resume a run)")
    c01.add_argument(
        "--memspine-read-mode",
        choices=("replay", "auto", "full", "compose"),
        default=None,
        help="memspine arm reads via Engine.read(mode) instead of assemble (C7')",
    )
    c01.add_argument("--run-id", default=None)
    c01.add_argument("--out", default=str(DEFAULT_OUT))
    c01.add_argument("--score-matrix", default=None, help="append rows to this CSV")
    c01.set_defaults(func=cmd_c0_1)

    split = sub.add_parser(
        "split", help="reserve development items before tuning (evaluation plan section 4)"
    )
    split.add_argument("--dataset", choices=("locomo", "longmemeval", "synthetic"), required=True)
    split.add_argument("--path")
    split.add_argument("--revision", default="auto")
    split.add_argument("--variant", default="s")
    split.add_argument(
        "--dev-items",
        type=int,
        required=True,
        help="whole items reserved for development: 2 for LoCoMo, 40 for LongMemEval",
    )
    split.add_argument("--seed", type=int, default=0)
    split.add_argument("--items", type=int, default=None)
    split.add_argument("--note", default="")
    split.add_argument("--output", required=True)
    split.set_defaults(func=cmd_split)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
