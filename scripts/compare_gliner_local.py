"""Fixed synthetic entity acceptance corpus; offline per-model subprocess run, capped at 11 calls."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import time
from pathlib import Path

CASES = (
    (
        "Mira works at Acme in Utrecht.",
        (("person", "Mira"), ("organization", "Acme"), ("location", "Utrecht")),
    ),
    (
        "Tomas moved from Braga to Rotterdam in 2022.",
        (("person", "Tomas"), ("location", "Braga"), ("location", "Rotterdam")),
    ),
    (
        "Leena joined Northstar Labs last spring.",
        (("person", "Leena"), ("organization", "Northstar Labs")),
    ),
    ("Our meeting was postponed until next Tuesday.", ()),
    (
        "Nora and Elias visited Oslo together.",
        (("person", "Nora"), ("person", "Elias"), ("location", "Oslo")),
    ),
)
LABELS = ("person", "organization", "location")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def score(predicted: set[tuple[str, int, int]], expected: set[tuple[str, int, int]]) -> dict:
    true_positive = len(predicted & expected)
    precision = true_positive / len(predicted) if predicted else float(not expected)
    recall = true_positive / len(expected) if expected else float(not predicted)
    return {
        "tp": true_positive,
        "predicted": len(predicted),
        "expected": len(expected),
        "precision": precision,
        "recall": recall,
    }


def run(directory: Path) -> dict:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import psutil
    import torch
    from gliner2 import AutoExtractor

    directory = directory.resolve(strict=True)
    torch.set_num_threads(2)
    process = psutil.Process()
    baseline_rss = process.memory_info().rss
    load_start = time.perf_counter()
    model = AutoExtractor.from_pretrained(str(directory), local_files_only=True)
    model.eval()
    load_seconds = time.perf_counter() - load_start
    cpu = psutil.cpu_percent(interval=0.1)
    rows = []
    times = []
    all_predicted = set()
    all_expected = set()
    first_seconds = None
    for index, (text, gold) in enumerate(CASES):
        expected = {
            (kind, text.index(surface), text.index(surface) + len(surface))
            for kind, surface in gold
        }
        for repeat in range(2):
            start = time.perf_counter()
            with torch.inference_mode():
                result = model.extract_entities(
                    text, list(LABELS), include_spans=True, include_confidence=True
                )
            elapsed = time.perf_counter() - start
            if first_seconds is None:
                first_seconds = elapsed
            if repeat == 1:
                times.append(elapsed)
            if repeat:
                predicted = set()
                rejected = 0
                for kind, spans in result.get("entities", {}).items():
                    for span in spans:
                        if (
                            isinstance(span, dict)
                            and type(span.get("start")) is int
                            and type(span.get("end")) is int
                            and 0 <= span["start"] < span["end"] <= len(text)
                            and text[span["start"] : span["end"]] == span.get("text")
                        ):
                            predicted.add((kind, span["start"], span["end"]))
                        else:
                            rejected += 1
                all_predicted.update((index, *value) for value in predicted)
                all_expected.update((index, *value) for value in expected)
                rows.append(
                    {
                        "case": index,
                        "source": text,
                        "predicted": sorted(predicted),
                        "expected": sorted(expected),
                        "ungrounded_outputs": rejected,
                    }
                )
    totals = score(all_predicted, all_expected)
    precision, recall = totals["precision"], totals["recall"]
    totals["f1"] = 2 * precision * recall / (precision + recall) if precision + recall else 0
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "checkpoint_revision": directory.name,
        "versions": {
            name: importlib.metadata.version(name) for name in ("gliner2", "torch", "transformers")
        },
        "corpus_sha256": hashlib.sha256(json.dumps(CASES).encode()).hexdigest(),
        "settings": {
            "device": "cpu",
            "threads": 2,
            "labels": LABELS,
            "threshold": "SDK default, not tuned",
            "offline": True,
        },
        "model_calls": len(CASES) * 2,
        "call_cap": 11,
        "host_cpu_percent_before_cases": cpu,
        "load_seconds": load_seconds,
        "first_inference_seconds": first_seconds,
        "warm_seconds": {
            "median": statistics.median(times),
            "p95": percentile(times, 0.95),
            "samples": times,
        },
        "rss_before_load_bytes": baseline_rss,
        "rss_after_cases_bytes": process.memory_info().rss,
        "peak_working_set_bytes": getattr(process.memory_info(), "peak_wset", None),
        "exact_typed_span_scores": totals,
        "cases": rows,
        "limits": [
            "Five fixed invented English cases authored before inference; never tuned against these results.",
            "Not a statistically powered quality benchmark, unknown pretraining overlap, not an LLM answer benchmark.",
            "Shared development host; no production latency guarantee; threshold unchanged.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-directory", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.model_directory)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"scores": report["exact_typed_span_scores"], "model_calls": report["model_calls"]}
        )
    )
    return 1 if any(row["ungrounded_outputs"] for row in report["cases"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
