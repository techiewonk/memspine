"""MD-03 driver: run md03_bench.py for every model x provider, compare, write JSON results.

Usage: python md03_run.py <outdir>   (uses this interpreter; DML device = the AMD adapter)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from md03_adapters import find_amd, list_adapters  # noqa: E402

MODELS = [
    ("embed", "BAAI/bge-small-en-v1.5"),
    ("embed", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
    ("rerank", "Xenova/ms-marco-MiniLM-L-6-v2"),
]
BS = (1, 8, 32, 64)


def run(kind, model, prov, dev, out):
    tag = f"{model.replace('/', '_')}_{prov}"
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "md03_bench.py"), "--kind", kind, "--model", model,
                        "--provider", prov, "--device-id", str(dev), "--out", str(out / tag)], capture_output=True, text=True)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("JSON:")]
    if not line:
        return {"model": model, "provider": prov, "error": (r.stderr or r.stdout)[-1500:]}
    return json.loads(line[0][5:]) | {"_tag": tag}


def main():
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    dev = find_amd()
    print("adapters:", list_adapters(), "amd id:", dev, flush=True)
    assert dev not in (None, 0)
    results = []
    for kind, model in MODELS:
        cpu = run(kind, model, "cpu", dev, out); print(model, "cpu done", flush=True)
        dml = run(kind, model, "dml", dev, out); print(model, "dml done", flush=True)
        if "error" not in dml and "error" not in cpu:
            dml["correct"] = {}
            for bs in BS:
                a = np.load(out / f"{cpu['_tag']}_bs{bs}.npy"); b = np.load(out / f"{dml['_tag']}_bs{bs}.npy")
                if kind == "embed":
                    cos = (a * b).sum(1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
                    dml["correct"][bs] = {"min_cos": float(cos.min()), "ok": bool(cos.min() >= 0.999)}
                else:
                    d = float(np.abs(a - b).max())
                    dml["correct"][bs] = {"max_abs_diff": d, "same_rank": bool((np.argsort(-a) == np.argsort(-b)).all()),
                                          "ok": bool(d <= 1e-3 and (np.argsort(-a) == np.argsort(-b)).all())}
        results += [cpu, dml]
    (out / "results.json").write_text(json.dumps(results, indent=1))
    print("DONE")


if __name__ == "__main__":
    main()
