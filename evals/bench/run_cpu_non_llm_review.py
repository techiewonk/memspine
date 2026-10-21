"""Sequential isolated-process driver for the synthetic CPU kernel probe."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--deps", required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    hub = Path.home() / ".cache/huggingface/hub"
    specs = [
        ("bm25", "bm25-labels"),
        ("static", "minishlab/potion-base-8M"),
        ("static", "minishlab/potion-retrieval-32M"),
        ("encoder", "sentence-transformers/all-MiniLM-L6-v2"),
        ("encoder", "BAAI/bge-small-en-v1.5"),
        ("gliclass", "knowledgator/gliclass-instruct-edge-v1.0"),
        ("gliner", "fastino/gliner2.5-small-v1"),
        ("gliner", "fastino/gliner2.5-base-v1"),
        ("reranker", "cross-encoder/ms-marco-TinyBERT-L2-v2"),
        ("reranker", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
    ]
    manifest = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_threads_per_worker": 4,
        "models": [],
        "policy": "Sequential fresh processes; CPU only; cached local weights; synthetic task kernels; no engine/eval suite.",
    }
    for kind, name in specs:
        slug = name.replace("/", "--")
        output = a.out / (slug + ".json")
        cmd = [
            a.python,
            str(Path(__file__).with_name("cpu_non_llm_kernels.py")),
            "--kind",
            kind,
            "--model",
            name,
            "--out",
            str(output),
            "--deps",
            a.deps,
        ]
        snapshot = None
        if kind != "bm25":
            folder = hub / ("models--" + name.replace("/", "--")) / "snapshots"
            choices = [p for p in folder.glob("*") if (p / "model.safetensors").exists()]
            if not choices:
                print("MISSING", name, flush=True)
                manifest["models"].append({"model": name, "status": "missing cached weights"})
                continue
            # Prefer a full repository layout when available.
            snapshot = max(choices, key=lambda p: len(list(p.iterdir())))
            cmd += ["--path", str(snapshot)]
        env = dict(
            os.environ,
            CUDA_VISIBLE_DEVICES="",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            PYTHONUNBUFFERED="1",
            TOKENIZERS_PARALLELISM="false",
            OMP_NUM_THREADS="4",
            MKL_NUM_THREADS="4",
            OPENBLAS_NUM_THREADS="4",
        )
        start = time.perf_counter()
        print("START", name, flush=True)
        try:
            with (a.out / (slug + ".log")).open("w", encoding="utf-8") as log:
                p = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=240)
            if output.exists():
                result = json.loads(output.read_text())
                result["snapshot_revision"] = snapshot.name if snapshot else None
                result["worker_exit_code"] = p.returncode
                result["worker_wall_seconds"] = time.perf_counter() - start
                output.write_text(json.dumps(result, indent=2) + "\n")
                summary = {
                    k: result[k]
                    for k in ("status", "load_and_label_setup_ms", "sampled_peak_rss_mb", "error")
                    if k in result
                }
                for task in ("routing", "ner", "rerank", "cached_similarity"):
                    if task in result:
                        summary[task] = {k: v for k, v in result[task].items() if k != "outputs"}
            else:
                summary = {"status": "worker failed", "exit": p.returncode}
        except subprocess.TimeoutExpired:
            summary = {"status": "worker timeout", "limit_seconds": 240}
        manifest["models"].append(
            {"model": name, "revision": snapshot.name if snapshot else None, **summary}
        )
        (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print("DONE", name, json.dumps(summary), flush=True)
    print("COMPLETE", str(a.out), flush=True)


if __name__ == "__main__":
    main()
