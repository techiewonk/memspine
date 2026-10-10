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
from .experiments import JUDGE_CHOICES, PROTOCOL_PRESETS, C01Config, comparison_table, run_c0_1
from .results import append_score_matrix_rows


def parse_categories(value: str | None) -> tuple[int, ...] | None:
    """``"1,2,3,4"`` -> (1, 2, 3, 4); ``"all"`` -> None (every category)."""
    if value is None or value.strip().lower() == "all":
        return None
    try:
        cats = tuple(sorted({int(part) for part in value.split(",") if part.strip()}))
    except ValueError as exc:
        raise SystemExit(f"--categories takes ints like 1,2,3,4 or 'all', got {value!r}") from exc
    if not cats or any(c not in (1, 2, 3, 4, 5) for c in cats):
        raise SystemExit(f"LoCoMo categories are 1-5, got {value!r}")
    return cats


def resolve_categories(args: argparse.Namespace) -> tuple[int, ...] | None:
    """R3-1: a LoCoMo run states its categories, directly or through a preset."""
    preset = PROTOCOL_PRESETS.get(getattr(args, "protocol", None) or "", {}).get("categories")
    given = getattr(args, "categories", None)
    if given is None:
        if preset is not None:
            return tuple(preset)
        if getattr(args, "command", None) == "c0-1" and getattr(args, "dataset", None) == "locomo":
            raise SystemExit(
                "--categories is required for LoCoMo (e.g. 1,2,3,4, or 'all' to include the "
                "cat-5 abstention questions, graded against a refusal)"
            )
        return None
    return parse_categories(given)


def parse_prices(specs: list[str] | None) -> tuple[tuple[str, float, float], ...]:
    """Repeated ``--price model=IN,OUT`` flags -> ``(model, in, out)`` triples.

    ``embed:`` / ``rerank:`` prices (C-6) are skipped here; see ``parse_service_prices``.
    """
    from .bedrock import parse_price, parse_service_price

    try:
        return tuple(parse_price(spec) for spec in specs or () if parse_service_price(spec) is None)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def parse_service_prices(specs: list[str] | None) -> tuple[tuple[str, str, float], ...]:
    """C-6: ``--price embed:MODEL=USD_PER_MTOK`` / ``--price rerank:MODEL=USD_PER_1K``
    flags -> ``(kind, model, price)`` triples."""
    from .bedrock import parse_service_price

    out: list[tuple[str, str, float]] = []
    try:
        for spec in specs or ():
            parsed = parse_service_price(spec)
            if parsed is not None:
                out.append(parsed)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return tuple(out)


DEFAULT_OUT = Path(__file__).resolve().parents[1] / "runs"


def _dataset(args: argparse.Namespace) -> DatasetAdapter:
    if args.dataset == "synthetic":
        from .datasets import SyntheticDataset

        return SyntheticDataset(n_items=args.items or 3, turns_per_item=24)
    if not args.path:
        raise SystemExit(f"--path is required for --dataset {args.dataset}")
    if args.dataset == "locomo":
        from .datasets import LoCoMoDataset

        return LoCoMoDataset(
            args.path, revision_id=args.revision, categories=resolve_categories(args)
        )
    if args.dataset == "convomem":
        from .datasets import ConvoMemDataset

        return ConvoMemDataset(
            args.path,
            revision_id=args.revision,
            per_stratum=args.per_stratum,
            filler=args.filler,
        )
    if args.dataset == "memoryagentbench":
        from .datasets import MemoryAgentBenchDataset

        return MemoryAgentBenchDataset(args.path, revision_id=args.revision)
    if args.dataset == "op_bench":
        from .datasets import OPBenchDataset

        tasks = tuple(t for t in (args.opbench_tasks or "").split(",") if t.strip())
        return OPBenchDataset(
            args.path,
            revision_id=args.revision,
            **({"tasks": tasks} if tasks else {}),
            both_personas=args.both_personas,
            per_task=args.opbench_per_task,
        )
    if args.dataset == "locomo_plus":
        from .datasets import LoCoMoPlusDataset

        if not args.locomo_path:
            raise SystemExit("--locomo-path (the repo's data/locomo10.json) is required")
        return LoCoMoPlusDataset(args.path, args.locomo_path, revision_id=args.revision)
    if args.dataset == "longmemeval":
        from .datasets import LongMemEvalDataset

        return LongMemEvalDataset(args.path, revision_id=args.revision, variant=args.variant)
    raise SystemExit(f"unknown dataset {args.dataset!r}")


def apply_opbench_protocol(args: argparse.Namespace) -> list[str]:
    """OP-Bench runs use the official answer prompt and judge (``analysis/OPBENCH_PROTOCOL.md``).

    Picks ``--qa-prompt opbench_assistant`` and ``--judge-prompt opbench`` when they are left
    at their defaults, refuses a conflicting choice (they would grade open-ended assistant
    replies as factual QA), and switches off the LoCoMo-only reader and judge fixes: the
    refusal retry, the judge guards, the date check and the answer verifier. Returns the log
    lines it printed.
    """
    notes: list[str] = []
    qa = args.mode == "qa" and not args.retrieval_only
    if args.dataset != "op_bench":
        if args.qa_prompt == "opbench_assistant" or args.judge_prompt == "opbench":
            raise SystemExit(
                "--qa-prompt opbench_assistant / --judge-prompt opbench need --dataset op_bench"
            )
        return notes
    if not qa:
        return notes
    if args.bedrock or args.protocol:
        raise SystemExit("OP-Bench runs use the local reader and judge: no --bedrock / --protocol")
    if args.qa_prompt == "default":
        args.qa_prompt = "opbench_assistant"
        notes.append("OP-Bench: --qa-prompt default -> opbench_assistant (official prompt)")
    elif args.qa_prompt != "opbench_assistant":
        raise SystemExit(
            f"--qa-prompt {args.qa_prompt} is a factual-QA prompt; OP-Bench probes are "
            "open-ended assistant requests: use opbench_assistant (or leave the default)"
        )
    if args.judge_prompt == "rubric":
        args.judge_prompt = "opbench"
        notes.append("OP-Bench: --judge-prompt rubric -> opbench (official OP-Bench judge)")
    elif args.judge_prompt != "opbench":
        raise SystemExit(f"--judge-prompt {args.judge_prompt} cannot grade OP-Bench; use opbench")
    for flag in ("retry_refusal", "judge_guards", "judge_date_check", "verify_answer"):
        if getattr(args, flag):
            setattr(args, flag, False)
            notes.append(
                f"OP-Bench: --{flag.replace('_', '-')} ignored (a LoCoMo factual-QA fix; it "
                "does not apply to open-ended assistant replies)"
            )
    for note in notes:
        print(note, file=sys.stderr, flush=True)
    return notes


def finalize_opbench(
    dataset: DatasetAdapter, summaries: list, out: Path, config: C01Config
) -> None:
    """After an OP-Bench run: the official aggregation (QA mode) and the retrieval proxies,
    printed, written to ``<run>/opbench_summary.json`` and merged into ``summary.json``."""
    from .opbench import aggregate, default_embedder, diagnostics, render_report, write_report
    from .results import read_run

    meta = {q.query_id: q.meta for item in dataset.items() for q in item.queries}
    embed = None
    for summary in summaries:
        run_dir = out / summary.run_id
        _, rows, _ = read_run(run_dir / "results.jsonl")
        report: dict = {}
        if config.mode == "qa" and not config.retrieval_only:
            embed = embed or default_embedder(config.embedding_model)
            report = aggregate(rows, embed)
        report["diagnostics"] = diagnostics(rows, meta)
        write_report(run_dir, report)
        path = run_dir / "summary.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["opbench"] = {k: v for k, v in report.items() if k != "per_probe"}
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\n{summary.run_id}\n{render_report(report)}")


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


def failed_rerank_checks(out: Path, summaries: list) -> list[str]:
    """B11: run ids whose ``summary.json`` says ``rerank_check: FAILED``."""
    failed = []
    for summary in summaries:
        path = out / summary.run_id / "summary.json"
        if not path.exists():
            continue
        if json.loads(path.read_text(encoding="utf-8")).get("rerank_check") == "FAILED":
            failed.append(summary.run_id)
    return failed


def cmd_c0_1(args: argparse.Namespace) -> int:
    if args.cache_reader and not args.cache_dir:
        raise SystemExit("--cache-reader needs --cache-dir")
    if args.retrieval_only and args.mode == "qa":
        print(
            "NOTE: --retrieval-only skips the reader and the judge; --mode qa is ignored",
            file=sys.stderr,
            flush=True,
        )
    apply_opbench_protocol(args)
    opbench_root = None
    if args.dataset == "op_bench" and args.mode == "qa" and not args.retrieval_only:
        from .opbench import load_judge_prompts, resolve_root

        opbench_root = str(resolve_root(args.path))
        load_judge_prompts(opbench_root)  # fail before any ingestion if the checkout is partial
    dataset = _dataset(args)
    config = C01Config(
        # screening runs read as the QA run would, but never generate
        mode="retrieval" if args.retrieval_only else args.mode,
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
        memspine_as_of_question_date=args.memspine_as_of_question_date,
        memspine_mark_hits=args.memspine_mark_hits,
        memspine_context_order=args.memspine_context_order,
        memspine_build_sleep=args.memspine_build_sleep,
        memspine_batch_turns=args.memspine_batch_turns,
        qa_prompt=args.qa_prompt,
        judge_prompt=args.judge_prompt,
        only_systems=tuple(args.only_systems.split(",")) if args.only_systems else None,
        item_ids=tuple(args.item_ids.split(",")) if args.item_ids else None,
        categories=resolve_categories(args) if args.dataset == "locomo" else None,
        naive_dense_same_embedder=args.naive_dense_same_embedder,
        matched_budget_tokens=args.matched_budget_tokens,
        memspine_llm=args.memspine_llm,
        prices_per_mtok=parse_prices(args.price),
        service_prices=parse_service_prices(args.price),
        max_usd=args.max_usd,
        verify_answer=args.verify_answer,
        retry_refusal=args.retry_refusal,
        judge_guards=args.judge_guards,
        judge_date_check=args.judge_date_check,
        retrieval_only=args.retrieval_only,
        cache_dir=args.cache_dir,
        cache_reader=args.cache_reader,
        presence_penalty=args.presence_penalty,
        frequency_penalty=args.frequency_penalty,
        top_p=args.top_p,
        sampler_seed=args.sampler_seed,
        server_ctx=args.server_ctx,
        strict_ctx=args.strict_ctx,
        token_count=args.token_count,
        tokenizer_id=args.tokenizer_id,
        capture_runtime=not args.no_runtime_capture,
        opbench_root=opbench_root,
        # a local Ollama is asked for its version / loaded models only when this run uses it
        probe_server=(
            not args.no_runtime_capture
            and not args.bedrock
            and not args.retrieval_only
            and args.mode == "qa"
        ),
    )
    if args.protocol:
        from .experiments import apply_protocol_preset

        config = apply_protocol_preset(config, args.protocol)
    from .experiments import check_dollar_cap, unpriced_services

    try:
        check_dollar_cap(config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    for kind, model in unpriced_services(config):
        # C-6: without --max-usd an unpriced paid service is allowed, but not silently
        print(
            f"WARNING: the memspine arm's {kind} model {model!r} is a paid cloud service "
            f"with no --price {kind}:{model}=...; its cost is NOT in the metered spend",
            file=sys.stderr,
            flush=True,
        )
    if config.bedrock or (config.include_memspine and config.memspine_llm != "none"):
        from .bedrock import load_aws_credentials

        # AWS keys + region ONLY; nothing else in the repo .env is read.
        load_aws_credentials(Path(__file__).resolve().parents[2] / ".env")
        if not args.skip_preflight:
            from .credentials import CredentialsInvalid, estimate_run_seconds, preflight_aws

            # C-3: before any spend, the credentials must work and should outlive the run.
            try:
                preflight_aws(expected_seconds=estimate_run_seconds(args.max_model_calls))
            except CredentialsInvalid as exc:
                raise SystemExit(str(exc)) from exc
    if config.include_memspine and config.memspine_llm != "none" and args.max_model_calls is None:
        raise SystemExit(
            "--memspine-llm binds the engine's LLM roles to a paid model — pass "
            "--max-model-calls with a cap you have agreed to"
        )
    if config.mode == "qa" and not config.retrieval_only and args.max_model_calls is None:
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
        f"- reader: `{config.reader_model if config.mode == 'qa' else 'none (retrieval only)'}`\n"
        + ("- screening: retrieval-only (no reader, no judge)\n" if config.retrieval_only else "")
        + "\n"
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
    if args.dataset == "op_bench":
        finalize_opbench(dataset, summaries, Path(args.out), config)
    failed = failed_rerank_checks(Path(args.out), summaries)
    if failed:
        # B11 [RET-3]: a configured reranker that never ran (or failed) voids the run
        print(
            f"ERROR: rerank_check FAILED for {', '.join(failed)}: the reranker is configured "
            "but made no call or failed; the numbers are not the configured pipeline's",
            file=sys.stderr,
            flush=True,
        )
        return 3
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
    c01.add_argument(
        "--dataset",
        choices=(
            "convomem",
            "locomo",
            "locomo_plus",
            "longmemeval",
            "memoryagentbench",
            "op_bench",
            "synthetic",
        ),
        required=True,
    )
    c01.add_argument(
        "--both-personas",
        action="store_true",
        help="op_bench: score both speakers of each conversation (the official default is the "
        "first speaker only, 869 probes)",
    )
    c01.add_argument(
        "--opbench-tasks",
        default=None,
        help="op_bench: comma list from irrelevance_easy,irrelevance_hard,sycophancy,diversity "
        "(default all four)",
    )
    c01.add_argument(
        "--opbench-per-task",
        type=int,
        default=None,
        metavar="N",
        help="op_bench rehearsals: keep only the first N probes of each task per persona",
    )
    c01.add_argument("--per-stratum", type=int, default=20, help="ConvoMem items per stratum")
    c01.add_argument("--filler", type=int, default=0, help="ConvoMem filler conversations")
    c01.add_argument("--locomo-path", default=None, help="locomo10.json paired with LoCoMo-Plus")
    c01.add_argument(
        "--path",
        help="path to the dataset json (op_bench: the OP-Bench checkout, e.g. "
        "data/opbench_src, or its data/ folder)",
    )
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
    c01.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    c01.add_argument("--max-model-calls", type=int, default=None)
    c01.add_argument(
        "--bedrock", action="store_true", help="D23 Qwen3 protocol: Bedrock Qwen3 reader + judge"
    )
    c01.add_argument(
        "--memspine-config", default=None, help="JSON engine overrides for the memspine arm"
    )
    c01.add_argument("--only-systems", default=None, help="comma list of system ids to run")
    c01.add_argument(
        "--protocol",
        choices=("omnimemeval",),
        default=None,
        help="declared protocol preset (reader/judge/endpoint), H25",
    )
    c01.add_argument(
        "--judge-prompt",
        choices=JUDGE_CHOICES,
        default="rubric",
        help=(
            "QA judge on every endpoint: rubric (QA; abstention-aware), constraint "
            "(LoCoMo-Plus), alias, locomo-plus-v2 (official LoCoMo-Plus prompts), longmemeval "
            "(anscheck templates by type), omnimemeval (official OmniMemEval LoCoMo judge), "
            "mem0-official (Mem0 paper J-score judge, CORRECT/WRONG, generous on same-topic "
            "answers; vendored text, cannot grade cat-5 abstention), "
            "rubric-guarded (rubric plus relative-date and hedged-answer rules), "
            "opbench (official OP-Bench judge prompts; --dataset op_bench only)"
        ),
    )
    c01.add_argument(
        "--qa-prompt",
        choices=(
            "default",
            "dated",
            "dated2",
            "dated_infer",
            "dated3",
            "grounded",
            "grounded_detail",
            "grounded_ordered",
            "grounded_v2",
            "grounded_v3",
            "dated_world",
            "abstain",
            "converse",
            "mab_fc",
            "question_dated",
            "routed",
            "dated_planned",
            "dated_noabstain",
            "evermemos_cot",
            "opbench_assistant",
        ),
        default="default",
        help="QA prompt variant for every arm (H7/H12); question_dated shows the question date; "
        "routed (C1) picks dated / a temporal / an inference variant per question; "
        "grounded explains the line dates and [= date] annotations, no blanket refusal",
    )
    c01.add_argument(
        "--retry-refusal",
        action="store_true",
        help="re-ask once, with a firmer instruction, when the reader refuses (or answers "
        "empty) on a non-empty context; both answers land in the row meta, the extra call "
        "counts against --max-model-calls; off by default",
    )
    c01.add_argument(
        "--judge-guards",
        action="store_true",
        help="empty answers score wrong without a judge call; with --judge-prompt rubric the "
        "judge is the rubric-guarded variant (equivalent relative-date phrasings and hedged "
        "answers that contain the gold fact are CORRECT); off by default",
    )
    c01.add_argument(
        "--judge-date-check",
        action="store_true",
        help="gap A2: before the LLM judge, credit an answer whose first date is the single day "
        "the "
        "gold names (e.g. 'the Friday before 15 July 2023'); refusals and denials never credited",
    )
    c01.add_argument(
        "--verify-answer",
        action="store_true",
        help="#39: verify each QA answer against its context (memspine verify_answer prompt, "
        "+1 call per question on the judge backend); off by default",
    )
    c01.add_argument(
        "--categories",
        default=None,
        help="LoCoMo categories, e.g. 1,2,3,4 or 'all' (required for LoCoMo unless a preset "
        "sets them)",
    )
    c01.add_argument(
        "--naive-dense-same-embedder",
        action="store_true",
        help="add a naive-RAG arm on memspine's own (fastembed) embedder (R4-6)",
    )
    c01.add_argument(
        "--matched-budget-tokens",
        type=int,
        default=None,
        help="add a naive-RAG arm capped at this context size, e.g. memspine's mean (R4-6)",
    )
    c01.add_argument("--item-ids", default=None, help="comma list of item ids (resume a run)")
    c01.add_argument(
        "--memspine-build-sleep",
        action="store_true",
        help="run Engine.sleep() after ingestion so write-time stages take part (H2/H8/H14)",
    )
    c01.add_argument(
        "--memspine-batch-turns",
        type=int,
        default=1,
        metavar="N",
        help="memspine arm: write up to N turns of one session per write_messages call "
        "(one batched embedding); flushed before every query and at session boundaries (G9)",
    )
    c01.add_argument(
        "--memspine-as-of-question-date",
        action="store_true",
        help="memspine arm reads as of each question's date (LongMemEval question_date; "
        "N34), so turns recorded after the question are never retrieved",
    )
    c01.add_argument(
        "--memspine-mark-hits",
        choices=("off", "star", "rank"),
        default="off",
        help="memspine arm: mark the context lines that are final search hits for the reader "
        "(star: '* ' prefix; rank: '[hit k] ' prefix); neighbour lines stay unmarked (C1)",
    )
    c01.add_argument(
        "--memspine-context-order",
        choices=("chrono", "hits_first", "hit_blocks"),
        default="chrono",
        help="memspine arm: order of the rendered context lines (R2-4). chrono: as read; "
        "hits_first: final search hits in rank order, then the rest chronologically; "
        "hit_blocks: each hit with its neighbour lines, blocks in rank order",
    )
    c01.add_argument(
        "--memspine-read-mode",
        choices=("replay", "auto", "full", "compose"),
        default=None,
        help="memspine arm reads via Engine.read(mode) instead of assemble (C7')",
    )
    c01.add_argument(
        "--memspine-llm",
        choices=("none", "bedrock-qwen3"),
        default="none",
        help="bind every engine LLM role of the memspine arm (extract, summarize, reflect, ...) "
        "to this model; bedrock-qwen3 = the reader's Qwen3-32B in the environment's AWS region. "
        "Explicit llm.roles in --memspine-config win",
    )
    c01.add_argument(
        "--price",
        action="append",
        default=None,
        metavar="MODEL=IN,OUT",
        help="USD per 1M input,output tokens for a model id (repeatable); overrides the "
        "built-in table. Also embed:MODEL=USD_PER_1M_TOKENS for a paid embedder and "
        "rerank:MODEL=USD_PER_1K_SEARCHES for a paid reranker (C-6). Take them from the "
        "AWS Bedrock pricing page",
    )
    c01.add_argument(
        "--skip-preflight",
        action="store_true",
        help="do not check the AWS credentials with STS before a paid run (C-3)",
    )
    c01.add_argument(
        "--max-usd",
        type=float,
        default=None,
        help="dollar cap per arm (reader + judge + engine); a call whose worst case would "
        "cross it is refused and the arm stops with UNATTEMPTED rows",
    )
    c01.add_argument(
        "--retrieval-only",
        action="store_true",
        help="screening: ingest and read every question exactly as the QA run would (same "
        "engine config, budget, top_k, read mode) but skip the reader and the judge; rows "
        "record retrieved_ids, context_tokens, the gold evidence and ev_all/ev_any/ev_frac, "
        "and summary.json a per-category coverage block",
    )
    c01.add_argument(
        "--cache-dir",
        default=None,
        metavar="PATH",
        help="screening: a disk cache of paid embedding and engine-role completion calls "
        "(temperature 0 or unset only); a hit costs $0 and is counted in summary.json "
        "(cache_hits, cache_misses, usd_saved). Reader and judge calls are not cached",
    )
    c01.add_argument(
        "--cache-reader",
        action="store_true",
        help="with --cache-dir: cache reader and judge completions too (temperature 0 only)",
    )
    c01.add_argument(
        "--presence-penalty",
        type=float,
        default=0.0,
        help="A8: sampler presence_penalty sent with every reader and judge request (default 0; "
        "before this flag the server's own default, 1.5 for Qwen3.5 on Ollama, applied silently)",
    )
    c01.add_argument(
        "--frequency-penalty",
        type=float,
        default=0.0,
        help="A8: sampler frequency_penalty sent with every reader and judge request (default 0)",
    )
    c01.add_argument(
        "--top-p",
        type=float,
        default=1.0,
        help="A8: sampler top_p sent with every reader and judge request (default 1)",
    )
    c01.add_argument(
        "--sampler-seed",
        type=int,
        default=None,
        help="A8: sampler seed sent with every reader and judge request (default: --seed)",
    )
    c01.add_argument(
        "--server-ctx",
        type=int,
        default=8192,
        help="D2: the model server's context window in tokens; a reader/judge call with "
        "prompt + completion tokens >= this - 8 marks the row meta "
        "server_truncation_suspected and is counted in summary.json (default 8192)",
    )
    c01.add_argument(
        "--strict-ctx",
        action="store_true",
        help="D2: raise (stop the run) instead of flagging when a call reaches --server-ctx",
    )
    c01.add_argument(
        "--token-count",
        choices=("heuristic", "reader"),
        default="heuristic",
        help="D1: heuristic (chars/4, default; old runs stay comparable) | reader (count with "
        "the reader's tokenizer from the local HF cache, drop the lowest-ranked lines of an "
        "over-budget context instead of cutting its tail, record engine_tokens next to "
        "context_tokens)",
    )
    c01.add_argument(
        "--tokenizer-id",
        default=None,
        help="D1: Hugging Face tokenizer for --token-count reader (default: the first Qwen3 "
        "tokenizer found in the local cache; offline)",
    )
    c01.add_argument(
        "--no-runtime-capture",
        action="store_true",
        help="D4: do not record the manifest runtime block (versions, argv, env, Ollama state)",
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
