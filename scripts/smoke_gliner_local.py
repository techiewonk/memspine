"""Bounded offline GLiNER runtime smoke against an existing complete local snapshot."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import time
from pathlib import Path


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid_span(text: str, span: object) -> bool:
    """Reject Python slice conveniences that are not valid source offsets."""
    return (
        isinstance(span, dict)
        and type(span.get("start")) is int
        and type(span.get("end")) is int
        and 0 <= span["start"] < span["end"] <= len(text)
        and text[span["start"] : span["end"]] == span.get("text")
    )


def run(model_directory: Path) -> dict:
    model_directory = model_directory.resolve(strict=True)
    required = ("config.json", "model.safetensors")
    for name in required:
        if not (model_directory / name).is_file():
            raise ValueError(f"local snapshot missing {name}")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from gliner2 import AutoExtractor

    torch.set_num_threads(2)
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": {
            name: importlib.metadata.version(name) for name in ("gliner2", "torch", "transformers")
        },
        "snapshot_revision": model_directory.name,
        "artifact_sha256": {name: file_sha256(model_directory / name) for name in required},
        "offline": True,
        "threads": 2,
        "device": "cpu",
        "model_call_cap": 4,
        "status": "not_run",
        "limits": [
            "Synthetic SDK entity smoke; not held-out quality or full Engine ingestion acceptance."
        ],
    }
    start = time.perf_counter()
    try:
        model = AutoExtractor.from_pretrained(str(model_directory), local_files_only=True)
        model.eval()
        report["load_seconds"] = time.perf_counter() - start
        text = "Mira works at Acme in Utrecht."
        calls = []
        for _ in range(4):
            start = time.perf_counter()
            with torch.inference_mode():
                result = model.extract_entities(
                    text,
                    ["person", "organization", "location"],
                    include_confidence=True,
                    include_spans=True,
                )
            calls.append({"seconds": time.perf_counter() - start, "output": result})
        report["calls"] = calls
        entities = calls[-1]["output"].get("entities", {})
        spans = [span for values in entities.values() for span in values]
        valid = bool(spans) and all(valid_span(text, span) for span in spans)
        report["grounded_nonempty_spans"] = valid
        report["status"] = "passed" if valid else "failed"
    except Exception as exc:
        report["status"] = "failed"
        report["error_class"] = type(exc).__name__
        # This smoke has only local synthetic input. Limit diagnostic length.
        report["diagnostic"] = str(exc)[:800]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-directory", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.model_directory)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"GLiNER local smoke: {report['status']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
