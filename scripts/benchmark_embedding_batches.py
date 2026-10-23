"""Measure onnx CPU batch sizes against an already cached model.

This benchmark uses the pinned ONNX catalogue and standard Hugging Face cache.
It never downloads model files; missing cached artifacts fail visibly.

Example (Python 3.13 validation environment):

    $env:HF_HUB_OFFLINE='1'; $env:TRANSFORMERS_OFFLINE='1'
    python scripts/benchmark_embedding_batches.py --json-out docs/provider_verification/embedding-batch-kernel.json --log-out docs/provider_verification/embedding-batch-kernel.log
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np

from memspine.clients.weights.onnx import OnnxModel
from memspine.clients.weights.onnx_catalogue import resolve_model_spec

MODEL_NAME = "BAAI/bge-small-en-v1.5"
TEXTS_32 = (
    "Aster assigned the laboratory key to Rowan on Monday.",
    "The north archive opens at eight and closes at five.",
    "Mira moved the quarterly review to the Cedar meeting room.",
    "A blue bicycle was locked beside the east entrance.",
    "The release checklist requires a signed migration report.",
    "Jun keeps spare batteries in the desk drawer.",
    "The field team recorded rain near the southern bridge.",
    "Our library card expires at the end of September.",
    "Nadia prefers concise notes after customer interviews.",
    "The workshop starts after the delivery truck arrives.",
    "A ceramic cup cracked during the evening cleanup.",
    "The finance group approved the revised travel estimate.",
    "Kai sent the garden map to the volunteer coordinator.",
    "A quiet room is available on the third floor.",
    "The printer needs a new roll of archival paper.",
    "The crew will inspect the western trail tomorrow morning.",
    "Irene saved the signed form in the project folder.",
    "The community kitchen serves soup every Thursday.",
    "A small parcel arrived without a return address.",
    "The museum guide described the restored wooden clock.",
    "We measured the room before ordering the long table.",
    "The orange notebook contains the interview questions.",
    "The support team is testing a new status dashboard.",
    "A red kite landed in the grass near the fountain.",
    "The baker reserved two loaves for the morning pickup.",
    "The archive copy includes a note about the missing page.",
    "Ravi plans to visit the coastal station in November.",
    "The workshop materials fit inside one reusable box.",
    "A maintenance notice was posted beside the elevator.",
    "The choir practiced the final verse after lunch.",
    "The map legend marks public wells with a green circle.",
    "Elena confirmed the appointment by telephone this afternoon.",
)
TEXTS_64 = TEXTS_32 + tuple(
    f"Fixture note {index:02d}: {text}" for index, text in enumerate(TEXTS_32, start=33)
)


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _probe_standard_cache(cache_dir: Path) -> dict[str, Any]:
    """Record whether onnx's normal offline resolver can load this cache."""
    try:
        OnnxModel(
            resolve_model_spec(MODEL_NAME),
            threads=1,
            providers=["CPUExecutionProvider"],
            local_files_only=True,
        )
    except Exception as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__, "error": str(exc)}
    return {"status": "available"}


def _embed_all(model: OnnxModel, texts: tuple[str, ...], batch_size: int) -> np.ndarray:
    vectors = list(model.embed(list(texts), batch_size=batch_size))
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2 or array.shape != (len(texts), 384):
        raise RuntimeError(f"unexpected vector matrix shape {array.shape}")
    if not np.isfinite(array).all():
        raise RuntimeError("embedding output contains non-finite values")
    return array


def run(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("HF_HUB_OFFLINE", "").upper() not in {"1", "TRUE", "YES", "ON"}:
        raise RuntimeError("HF_HUB_OFFLINE must be set to 1 before running this benchmark")
    if os.environ.get("TRANSFORMERS_OFFLINE", "").upper() not in {"1", "TRUE", "YES", "ON"}:
        raise RuntimeError("TRANSFORMERS_OFFLINE must be set to 1 before running this benchmark")
    if args.warmups not in range(0, 3) or args.trials not in range(1, 6):
        raise ValueError("warmups must be 0..2 and trials must be 1..5")
    if args.max_seconds < 1 or args.max_seconds > 180:
        raise ValueError("max-seconds must be between 1 and 180")

    start = time.perf_counter()
    cache_dir = Path(args.cache_dir).resolve()
    os.environ["HF_HUB_CACHE"] = str(cache_dir)
    result: dict[str, Any] = {
        "benchmark": "onnx-real-cpu-batch-kernel",
        "command": " ".join(sys.argv),
        "started_at_utc": datetime.now(UTC).isoformat(),
        "status": "running",
        "model": MODEL_NAME,
        "provider": "onnx/ONNX Runtime CPUExecutionProvider",
        "cache_dir": str(cache_dir),
        "offline_environment": {
            "HF_HUB_OFFLINE": os.environ["HF_HUB_OFFLINE"],
            "TRANSFORMERS_OFFLINE": os.environ["TRANSFORMERS_OFFLINE"],
            "local_files_only": True,
        },
        "settings": {
            "threads": 1,
            "cuda": False,
            "providers": ["CPUExecutionProvider"],
            "parallel": None,
            "warmups": args.warmups,
            "trials": args.trials,
            "batch_sizes": [1, 8, 32, 64],
            "text_counts": [32, 64],
            "allclose": {"rtol": 1e-5, "atol": 1e-6},
            "maximum_runtime_seconds": args.max_seconds,
        },
        "versions": {
            "python": sys.version,
            "platform": platform.platform(),
            "tokenizers": _version("tokenizers"),
            "onnxruntime": _version("onnxruntime"),
            "numpy": np.__version__,
        },
        "scope": (
            "Single-host local benchmark over invented fixed English text; no full MemSpine engine, "
            "no network, no model-quality claim."
        ),
    }

    with tempfile.TemporaryDirectory(prefix="memspine-onnx-batch-") as temp:
        result["standard_offline_cache_probe"] = _probe_standard_cache(cache_dir)
        spec = resolve_model_spec(MODEL_NAME)
        model = OnnxModel(
            spec, threads=1, providers=["CPUExecutionProvider"], local_files_only=True
        )
        result["model_info"] = {
            "runtime_class": type(model).__name__,
            "dimension": spec.dim,
            "revision": spec.revision,
            "repo": spec.repo,
            "model_file": spec.model_file,
        }
        result["cases"] = []
        for text_count, texts in ((32, TEXTS_32), (64, TEXTS_64)):
            reference: np.ndarray | None = None
            case_rows = []
            for batch_size in (1, 8, 32, 64):
                outputs: list[np.ndarray] = []
                for _ in range(args.warmups):
                    _embed_all(model, texts, batch_size)
                timings = []
                for _ in range(args.trials):
                    trial_start = time.perf_counter()
                    vector_matrix = _embed_all(model, texts, batch_size)
                    timings.append(time.perf_counter() - trial_start)
                    outputs.append(vector_matrix)
                    if time.perf_counter() - start > args.max_seconds:
                        raise TimeoutError(f"runtime exceeded {args.max_seconds}s cap")
                current = outputs[0]
                if reference is None:
                    reference = current
                    parity = {"matches_batch_size_1": True, "max_abs_difference": 0.0}
                else:
                    max_abs = float(np.max(np.abs(reference - current)))
                    parity = {
                        "matches_batch_size_1": bool(
                            np.allclose(reference, current, rtol=1e-5, atol=1e-6)
                        ),
                        "max_abs_difference": max_abs,
                    }
                repeat_max_abs = (
                    max(float(np.max(np.abs(current - other))) for other in outputs[1:])
                    if len(outputs) > 1
                    else 0.0
                )
                row = {
                    "text_count": text_count,
                    "batch_size": batch_size,
                    "vectors": int(current.shape[0]),
                    "dimension": int(current.shape[1]),
                    "mean_seconds": statistics.mean(timings),
                    "median_seconds": statistics.median(timings),
                    "documents_per_second_median": text_count / statistics.median(timings),
                    "repeat_max_abs_difference": repeat_max_abs,
                    "parity": parity,
                }
                case_rows.append(row)
            if not all(row["parity"]["matches_batch_size_1"] for row in case_rows):
                raise RuntimeError(f"batch output parity failed for text_count={text_count}")
            result["cases"].extend(case_rows)
        result["elapsed_seconds"] = time.perf_counter() - start
        result["status"] = "passed"
        result["conclusion"] = (
            "All batch sizes returned one 384-dimensional vector per input in order and were "
            "allclose to batch_size=1 at the recorded tolerance. Timing is a single-host kernel "
            "observation and does not justify changing a provider default."
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("HF_HUB_CACHE", str(Path.home() / ".cache/huggingface/hub")),
    )
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--log-out", type=Path)
    args = parser.parse_args()
    try:
        report = run(args)
    except Exception as exc:
        report = {
            "benchmark": "onnx-real-cpu-batch-kernel",
            "command": " ".join(sys.argv),
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "model": MODEL_NAME,
            "cache_dir": str(Path(args.cache_dir).resolve()),
            "offline_environment": {
                "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
                "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE"),
                "local_files_only": True,
            },
            "versions": {"python": sys.version, "tokenizers": _version("tokenizers")},
            "scope": "No network permitted; failure is an honest cache/runtime limitation.",
        }
        status = 1
    else:
        status = 0

    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered, encoding="utf-8")
    log = f"status={report['status']}\n{rendered}"
    if args.log_out:
        args.log_out.parent.mkdir(parents=True, exist_ok=True)
        args.log_out.write_text(log, encoding="utf-8")
    print(log, end="")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
