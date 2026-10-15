"""python -m module.locomo_eval --help"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

from .dataset import CATEGORIES, dataset_hash, load_dataset
from .metrics import load_official_evaluator, score_rows

DATA_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def csv_choices(value, choices):
    result = value.split(",")
    if len(result) != len(set(result)) or any(item not in choices for item in result):
        raise argparse.ArgumentTypeError(f"Choose unique comma-separated values from {choices}")
    return result


def parser():
    cli = argparse.ArgumentParser(
        description="Evaluate our combined Graphiti + SimpleMem memory engine"
    )
    commands = cli.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="Download official LoCoMo10 JSON")
    download.add_argument(
        "--output", type=Path, default=Path("module/locomo_eval/data/locomo10.json")
    )
    inspect = commands.add_parser("inspect", help="Validate dataset; no datastore/model calls")
    inspect.add_argument("--dataset", type=Path, required=True)
    bundle = commands.add_parser(
        "bundle",
        help="Package evaluator and HTML for the same repository revision on another system",
    )
    bundle.add_argument("--output", type=Path, required=True, help="New ZIP file; must not exist")
    bundle.add_argument(
        "--include-dataset", type=Path, help="Optional validated LoCoMo dataset to include"
    )
    run = commands.add_parser(
        "run", help="Ingest conversation and evaluate our combined engine (live)"
    )
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument(
        "--output", type=Path, required=True, help="New output directory, must not exist"
    )
    run.add_argument("--samples", type=positive, default=1)
    run.add_argument(
        "--questions", type=positive, default=20, help="per sample; use --full for all"
    )
    run.add_argument("--full", action="store_true")
    run.add_argument("--sample-ids", help="Optional comma-separated actual dataset sample IDs")
    run.add_argument(
        "--profiles", type=lambda v: csv_choices(v, ["current", "enhanced"]), default=["current"]
    )
    run.add_argument(
        "--memory-types",
        type=lambda v: csv_choices(v, ["both", "longterm", "shortterm"]),
        default=["both"],
    )
    run.add_argument("--ingestion", choices=["production", "corpus"], default="production")
    run.add_argument("--session-scope", choices=["all", "latest"], default="all")
    run.add_argument("--last-n", type=positive, help="Optional last-N replay; disabled by default")
    run.add_argument("--answer-model")
    run.add_argument("--judge-model", help="Optional independent strict Bedrock judge")
    run.add_argument(
        "--official-evaluator", type=Path, help="Upstream task_eval/evaluation.py path"
    )
    score = commands.add_parser("score", help="Rescore saved traces offline")
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--official-evaluator", type=Path)
    return cli


def git_revision():
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def create_bundle(output, dataset=None, *, root=None):
    """Export only named source files, offline tests, HTML and optional dataset."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    names = [
        "module/locomo_eval/__init__.py",
        "module/locomo_eval/__main__.py",
        "module/locomo_eval/backend.py",
        "module/locomo_eval/dataset.py",
        "module/locomo_eval/metrics.py",
        "module/locomo_eval/runner.py",
        "tests/locomo_eval/test_combined_eval.py",
        "tests/locomo_eval/test_bundle.py",
        "docs/COMBINED_MEMORY_ENGINE_AND_LOCOMO.html",
    ]
    payload = {name: (root / name).read_bytes() for name in names}
    if dataset is not None:
        load_dataset(dataset)
        payload["module/locomo_eval/data/locomo10.json"] = Path(dataset).read_bytes()
    revision = git_revision()
    payload["LOCOMO_BUNDLE_README.txt"] = (
        "Combined memory evaluator transfer bundle\n\n"
        f"Source repository revision: {revision}\n"
        "Extract into a complete checkout of the same repository revision on the target system.\n"
        "This archive is an overlay, not a standalone memory engine or installed environment.\n"
        "Uncommitted evaluator changes are included; the manifest hashes identify their exact contents.\n"
        "Install the repository dependencies and configure the target system's own services and credentials.\n"
        "Read docs/COMBINED_MEMORY_ENGINE_AND_LOCOMO.html before running live evaluation.\n"
        "Offline validation: python -m pytest tests/locomo_eval -o addopts= -q\n"
        "CLI help: python -m module.locomo_eval --help\n"
        "No credentials, environment files, caches or benchmark run outputs are included.\n"
    ).encode()
    manifest = dict(
        git_revision=revision,
        created_at=datetime.now(UTC).isoformat(),
        standalone=False,
        files={name: hashlib.sha256(content).hexdigest() for name, content in payload.items()},
    )
    payload["LOCOMO_BUNDLE_MANIFEST.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as target:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in payload.items():
                archive.writestr(name, content)
    return manifest


async def live(args):
    from .backend import CombinedMemoryBackend
    from .runner import run_samples

    samples = load_dataset(args.dataset)
    total_dataset_samples = len(samples)
    if args.sample_ids:
        requested = set(args.sample_ids.split(","))
        if requested - {s.sample_id for s in samples}:
            raise ValueError("Unknown --sample-ids")
        samples = [s for s in samples if s.sample_id in requested]
    selected = samples if args.full else samples[: args.samples]
    official = load_official_evaluator(args.official_evaluator) if args.official_evaluator else None
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = dict(
        run_id=uuid.uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        dataset_sha256=dataset_hash(args.dataset),
        git_revision=git_revision(),
        sample_ids=[s.sample_id for s in selected],
        subset=not args.full or len(selected) != total_dataset_samples,
        timezone_assumption="UTC for dates without timezone",
        status="initializing",
        options={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        harness_sha256={
            path.name: dataset_hash(path) for path in Path(__file__).parent.glob("*.py")
        },
        official_evaluator_sha256=dataset_hash(args.official_evaluator)
        if args.official_evaluator
        else None,
    )
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    backend = CombinedMemoryBackend(
        run_id=manifest["run_id"],
        ingestion=args.ingestion,
        session_scope=args.session_scope,
        last_n=args.last_n or 0,
        answer_model=args.answer_model,
        judge_model=args.judge_model,
    )
    try:
        await backend.initialize()
        rows = await run_samples(
            selected,
            backend,
            args.output / "predictions.jsonl",
            profiles=args.profiles,
            memory_types=args.memory_types,
            question_limit=None if args.full else args.questions,
            judge=bool(args.judge_model),
        )
        summary = score_rows(rows, official)
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        manifest["status"] = "complete"
        manifest["has_prediction_errors"] = any(row["error"] for row in rows)
        manifest["prediction_count"] = len(rows)
        print(json.dumps(summary, indent=2))
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        raise
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        await backend.close()


def main():
    args = parser().parse_args()
    if args.command == "download":
        with urlopen(DATA_URL, timeout=60) as response:
            content = response.read()
        json.loads(content)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as target:
            target.write(content)
        print(f"Downloaded {args.output}; sha256={dataset_hash(args.output)}")
    elif args.command == "inspect":
        samples = load_dataset(args.dataset)
        print(
            json.dumps(
                dict(
                    dataset_sha256=dataset_hash(args.dataset),
                    samples=len(samples),
                    turns=sum(len(s.turns) for s in samples),
                    questions=sum(len(s.questions) for s in samples),
                    by_category={
                        CATEGORIES[c]: sum(q.category == c for s in samples for q in s.questions)
                        for c in CATEGORIES
                    },
                ),
                indent=2,
            )
        )
    elif args.command == "score":
        rows = [
            json.loads(line)
            for line in args.predictions.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        official = (
            load_official_evaluator(args.official_evaluator) if args.official_evaluator else None
        )
        summary = score_rows(rows, official)
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(summary, target, indent=2)
        print(json.dumps(summary, indent=2))
    elif args.command == "bundle":
        manifest = create_bundle(args.output, args.include_dataset)
        print(
            f"Created {args.output}; {len(manifest['files'])} payload files; revision={manifest['git_revision']}"
        )
    else:
        asyncio.run(live(args))


if __name__ == "__main__":
    main()
