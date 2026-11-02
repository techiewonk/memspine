#!/usr/bin/env python
"""RM-25 / RM-26 / RS-06: onnx embedder and cross-encoder CPU envelope on the reference host.

Parts (``--part``), each written as its own PA-051 record (scripts/perf_protocol.py):

* ``cache`` (RM-25): populate a CLEAN Hugging Face cache directory with
  ``memspine models fetch --template base --model Xenova/ms-marco-MiniLM-L-6-v2`` (the
  pinned public files only; SHA-256 verified by the command), re-verify it with
  ``--check``, then time the first load from that cache in fresh processes (import,
  session construction including the digest check, first call) and the steady state
  (default threads, default batch 32) in the same processes;
* ``sweep`` (RM-26): bge-small embedder and MiniLM cross-encoder at intra-op threads
  0 (ONNX Runtime default), 1, 2, 4, the physical cores and all logical threads, and
  batch sizes 1, 4, 16, 32, 64: per-call latency and items/s. int8 vs fp32 parity runs
  only for a catalogue model that has both a pinned fp32 and a pinned int8 entry
  (ADR-093 amendment); none is invented;
* ``packer`` (RS-06): the read path's token counting on realistic packed contexts: the
  packer estimate, an exact tokenizer counter (the cached bge ``tokenizer.json``) and
  ``truncate_to_fit`` under each.

    $env:PYTHONPATH='src'
    python scripts/benchmark_onnx_envelope.py --part cache --cache-dir <empty dir>
    python scripts/benchmark_onnx_envelope.py --part sweep
    python scripts/benchmark_onnx_envelope.py --part packer
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perf_protocol import (
    ROOT,
    Recorder,
    corpus_messages,
    corpus_queries,
    measure,
    summarize,
)

EMBEDDER = "BAAI/bge-small-en-v1.5"
RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"
DATE = "2026-11-02"
OUT_DIR = ROOT / "docs" / "provider_verification"
BATCHES = (1, 4, 16, 32, 64)


def _reps(batch: int) -> int:
    return {1: 100, 4: 60, 16: 40}.get(batch, 30)


# ----------------------------------------------------------------------------- child


def child_load(model: str, steady_reps: int) -> dict[str, Any]:
    """One fresh process: import, load from the (offline) cache, first call, steady state."""
    t0 = time.perf_counter()
    import onnxruntime  # noqa: F401

    from memspine.clients.weights.onnx import OnnxModel
    from memspine.clients.weights.onnx_catalogue import resolve_model_spec

    t_import = time.perf_counter()
    spec = resolve_model_spec(model)
    loaded = OnnxModel(spec, threads=0, local_files_only=True)
    t_load = time.perf_counter()
    texts = corpus_messages(32, seed=3)
    rerank = spec.pooling == "score"
    query = corpus_queries(1, seed=5)[0]

    def call() -> None:
        if rerank:
            list(loaded.rerank(query, texts, batch_size=32))
        else:
            list(loaded.embed(texts, batch_size=32))

    call()
    t_first = time.perf_counter()
    steady = measure(call, reps=steady_reps, warmups=2)
    return {
        "import_ms": (t_import - t0) * 1000,
        "load_ms": (t_load - t_import) * 1000,
        "first_call_ms": (t_first - t_load) * 1000,
        "first_load_total_ms": (t_first - t0) * 1000,
        "steady_batch32_ms": steady,
    }


# ----------------------------------------------------------------------------- cache


def part_cache(args: argparse.Namespace) -> None:
    cache = Path(args.cache_dir).resolve()
    if cache.exists() and any(cache.iterdir()):
        raise SystemExit(f"--cache-dir {cache} must be empty (a clean cache)")
    cache.mkdir(parents=True, exist_ok=True)
    rec = Recorder(
        "rm25-onnx-clean-cache",
        OUT_DIR / f"perf-rm25-onnx-clean-cache-{DATE}.json",
        allow_dirty=args.allow_dirty,
        ignore=[OUT_DIR],
        settings={
            "cache_dir": "<fresh empty temporary directory>",
            "models": [EMBEDDER, RERANKER],
            "processes": args.processes,
            "steady_reps_per_process": args.steady_reps,
            "threads": "0 (ONNX Runtime default)",
            "batch_size": 32,
        },
    )
    env = dict(os.environ)
    env.pop("HF_HUB_OFFLINE", None)
    env.pop("TRANSFORMERS_OFFLINE", None)
    env["PYTHONPATH"] = str(ROOT / "src")
    base = [sys.executable, "-m", "memspine.cli", "models", "fetch", "--template", "base"]
    fetch = [*base, "--model", RERANKER, "--cache-dir", str(cache)]
    start = time.perf_counter()
    done = subprocess.run(fetch, capture_output=True, text=True, env=env, check=False)
    fetch_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    check = subprocess.run(
        [*fetch, "--check"], capture_output=True, text=True, env=env, check=False
    )
    check_ms = (time.perf_counter() - start) * 1000
    rec.record["results"]["fetch"] = {
        "command": "memspine models fetch --template base --model " + RERANKER,
        "exit_code": done.returncode,
        "wall_ms": round(fetch_ms, 1),
        "lines": done.stdout.splitlines(),
        "stderr_tail": done.stderr.splitlines()[-5:],
        "note": "wall time includes the network download; not a host property",
    }
    rec.record["results"]["verify"] = {
        "command": "memspine models fetch ... --check",
        "exit_code": check.returncode,
        "wall_ms": round(check_ms, 1),
        "lines": check.stdout.splitlines(),
        "all_sha256_ok": check.returncode == 0
        and all("sha256 ok" in line for line in check.stdout.splitlines()),
    }
    if done.returncode or check.returncode:
        rec.finish("failed")
        raise SystemExit("fetch/verify failed; see record")
    child_env = dict(env, HF_HUB_CACHE=str(cache), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    for model in (EMBEDDER, RERANKER):
        runs: list[dict[str, Any]] = []
        for _ in range(args.processes):
            out = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child-load",
                    model,
                    "--steady-reps",
                    str(args.steady_reps),
                ],
                capture_output=True,
                text=True,
                env=child_env,
                check=True,
            )
            runs.append(json.loads(out.stdout.strip().splitlines()[-1]))
        steady = [ms for run in runs for ms in run["steady_batch32_ms"]]
        rec.record["results"][model] = {
            "first_process_after_download": {
                k: round(v, 2) for k, v in runs[0].items() if k != "steady_batch32_ms"
            },
            "import_ms": summarize([r["import_ms"] for r in runs]),
            "load_ms": summarize([r["load_ms"] for r in runs]),
            "first_call_ms": summarize([r["first_call_ms"] for r in runs]),
            "first_load_total_ms": summarize([r["first_load_total_ms"] for r in runs]),
            "steady_batch32_ms": summarize(steady, warmups=2),
            "steady_items_per_s_p50": round(32 / (summarize(steady)["p50_ms"] / 1000), 1),
        }
    rec.note(
        "Process 1 runs right after the download, so the OS file cache already holds the "
        "files; a cold-disk first load is not reproducible on Windows without a reboot."
    )
    rec.finish()


# ----------------------------------------------------------------------------- sweep


def _thread_values() -> list[int]:
    import psutil

    physical = psutil.cpu_count(logical=False) or 1
    logical = os.cpu_count() or physical
    return sorted({0, 1, 2, 4, physical, logical})


def part_sweep(args: argparse.Namespace) -> None:
    import numpy as np

    from memspine.clients.weights.onnx import OnnxModel
    from memspine.clients.weights.onnx_catalogue import CATALOGUE, CATALOGUE_FILES

    threads = _thread_values()
    rec = Recorder(
        "rm26-onnx-cpu-envelope",
        OUT_DIR / f"perf-rm26-onnx-cpu-envelope-{DATE}.json",
        allow_dirty=args.allow_dirty,
        ignore=[OUT_DIR],
        settings={
            "models": [EMBEDDER, RERANKER],
            "threads": threads,
            "threads_note": "0 = ONNX Runtime default (physical cores); intra=inter=threads",
            "batch_sizes": list(BATCHES),
            "reps": {str(b): _reps(b) for b in BATCHES},
            "warmups": 5,
            "texts": "perf_protocol.corpus_messages (invented 15-30 word chat turns)",
            "unit": "one call = one ONNX session run over `batch` texts or query-document pairs",
        },
    )
    texts = corpus_messages(64, seed=3)
    query = corpus_queries(1, seed=5)[0]
    lengths = [len(t.split()) for t in texts]
    rec.record["settings"]["words_per_text"] = {"min": min(lengths), "max": max(lengths)}
    for model in (EMBEDDER, RERANKER):
        spec = CATALOGUE[model]
        rerank = spec.pooling == "score"
        rows = []
        reference: Any = None
        for t in threads:
            start = time.perf_counter()
            loaded = OnnxModel(spec, threads=t, local_files_only=True)
            load_ms = (time.perf_counter() - start) * 1000
            for batch in BATCHES:
                chunk = texts[:batch]

                def call(
                    chunk: list[str] = chunk, loaded: OnnxModel = loaded, rerank: bool = rerank
                ) -> Any:
                    if rerank:
                        return list(loaded.rerank(query, chunk, batch_size=len(chunk)))
                    return list(loaded.embed(chunk, batch_size=len(chunk)))

                samples = measure(call, reps=_reps(batch), warmups=5)
                stats = summarize(samples, warmups=5)
                rows.append(
                    {
                        "threads": t,
                        "batch": batch,
                        "load_ms": round(load_ms, 1),
                        **stats,
                        "items_per_s_p50": round(batch / (stats["p50_ms"] / 1000), 1),
                        "ms_per_item_p50": round(stats["p50_ms"] / batch, 3),
                    }
                )
                print(model, t, batch, stats["p50_ms"], stats["p95_ms"], flush=True)
            out = np.asarray(
                list(loaded.rerank(query, texts, batch_size=64))
                if rerank
                else list(loaded.embed(texts, batch_size=64)),
                dtype=np.float64,
            )
            if reference is None:
                reference = out
            rows[-1]["max_abs_diff_vs_first_thread_setting"] = float(
                np.max(np.abs(out - reference))
            )
            del loaded
        rec.record["results"][model] = rows
    int8 = sorted(
        model
        for model, pins in CATALOGUE_FILES.items()
        if any("int8" in p.filename or "quantized" in p.filename for p in pins)
    )
    rec.record["results"]["int8_parity"] = (
        {"status": "no int8 entry", "detail": "the pinned catalogue has no int8 file entries"}
        if not int8
        else {"status": "int8 entries present", "models": int8}
    )
    if not int8:
        rec.note(
            "int8 vs fp32 parity not run: no catalogue model has a separate pinned int8 entry "
            "(ADR-093 amendment); no unpinned file was added."
        )
    rec.finish()


# ----------------------------------------------------------------------------- packer


def part_packer(args: argparse.Namespace) -> None:
    from tokenizers import Tokenizer

    from memspine.clients.weights.onnx_catalogue import CATALOGUE
    from memspine.clients.weights.onnx_files import cached_file
    from memspine.compose.budget import CallableCounter
    from memspine.compose.pack import truncate_to_fit
    from memspine.compose.pipeline.render import PACKER_ESTIMATE, resolve_counter

    path = cached_file(CATALOGUE[EMBEDDER], "tokenizer.json")
    if path is None:
        raise SystemExit("bge tokenizer.json not cached; run memspine models fetch first")
    tok = Tokenizer.from_file(str(path))
    exact = CallableCounter(
        lambda t: tok.encode(t, add_special_tokens=False).ids, "hf-tokenizers:bge-small"
    )
    counters: dict[str, Any] = {"estimate_len//4+1": PACKER_ESTIMATE, "exact_bge_wordpiece": exact}
    tiktoken_counter = resolve_counter("tiktoken:cl100k_base")
    if tiktoken_counter is not PACKER_ESTIMATE:
        counters["exact_tiktoken_cl100k"] = tiktoken_counter
    rec = Recorder(
        "rs06-packer-token-counting",
        OUT_DIR / f"perf-rs06-packer-token-counting-{DATE}.json",
        allow_dirty=args.allow_dirty,
        ignore=[OUT_DIR],
        settings={
            "counters": list(counters),
            "contexts": "joined corpus_messages turns, rendered one per line with a header",
            "reps": 200,
            "warmups": 10,
        },
    )
    messages = corpus_messages(400, seed=13)
    results: dict[str, Any] = {}
    for target in (512, 2048, 8192):  # 2048 = ASSEMBLE_BUDGET_TOKENS (the default read budget)
        lines: list[str] = []
        i = 0
        while PACKER_ESTIMATE.count("\n".join(lines)) < target:
            lines.append(f"[{i:03d} | 2026-10-{1 + i % 28:02d} | user] {messages[i % 400]}")
            i += 1
        context = "\n".join(lines)
        row: dict[str, Any] = {"chars": len(context), "lines": len(lines)}
        for name, counter in counters.items():
            row[name] = {
                "tokens": counter.count(context),
                "count_whole": summarize(
                    measure(
                        lambda c=counter, context=context, lines=lines: c.count(context),
                        reps=200,
                        warmups=10,
                    ),
                    warmups=10,
                ),
                "count_per_line_sum": summarize(
                    measure(
                        lambda c=counter, context=context, lines=lines: sum(
                            c.count(line) for line in lines
                        ),
                        reps=200,
                        warmups=10,
                    ),
                    warmups=10,
                ),
                "truncate_to_fit_half": summarize(
                    measure(
                        lambda c=counter, context=context, lines=lines: truncate_to_fit(
                            context, c.count(context) // 2, c.count
                        ),
                        reps=50,
                        warmups=3,
                    ),
                    warmups=3,
                ),
            }
        results[f"target_{target}_estimated_tokens"] = row
    rec.record["results"] = results
    if "exact_tiktoken_cl100k" not in counters:
        rec.note("tiktoken cl100k_base did not resolve offline; only the bge counter is exact")
    rec.finish()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=("cache", "sweep", "packer"))
    parser.add_argument("--cache-dir")
    parser.add_argument("--processes", type=int, default=30)
    parser.add_argument("--steady-reps", type=int, default=10)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--child-load")
    args = parser.parse_args()
    if args.child_load:
        print(json.dumps(child_load(args.child_load, args.steady_reps)))
        return
    if args.part == "cache":
        if not args.cache_dir:
            parser.error("--part cache needs --cache-dir")
        part_cache(args)
    elif args.part == "sweep":
        part_sweep(args)
    elif args.part == "packer":
        part_packer(args)
    else:
        parser.error("--part or --child-load is required")


if __name__ == "__main__":
    main()
