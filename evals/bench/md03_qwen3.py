"""MD-03: Qwen3-Embedding-0.6B (ONNX export, onnx-community) on CPU vs DirectML (AMD iGPU).

Worker: python md03_qwen3.py --variant fp32|fp16 --provider cpu|dml --device-id N --out PREFIX
Last-token pooling + L2 norm (Qwen3-Embedding recipe). Synthetic texts only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import psutil
from tokenizers import Tokenizer

sys.path.insert(0, str(Path(__file__).parent))
from md03_bench import make_texts

ROOT = Path("C:/Users/hembad/AppData/Local/Temp/md03/qwen3emb")
INSTR = (
    "Instruct: Given a question about a person's life, retrieve the memory that answers it\nQuery:"
)
MAXLEN = 512


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="fp32")
    ap.add_argument("--provider", default="cpu")
    ap.add_argument("--device-id", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("--bs", default="1,8,16")
    a = ap.parse_args()
    if a.provider == "dml" and a.device_id == 0:
        sys.exit("refusing device 0")
    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    provs = (
        ["CPUExecutionProvider"]
        if a.provider == "cpu"
        else [("DmlExecutionProvider", {"device_id": a.device_id}), "CPUExecutionProvider"]
    )
    so = ort.SessionOptions()
    if a.provider == "dml":  # DirectML requirements
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    t0 = time.perf_counter()
    f = "model.onnx" if a.variant == "fp32" else "model_fp16.onnx"
    sess = ort.InferenceSession(str(ROOT / "onnx" / f), sess_options=so, providers=provs)
    load_s = time.perf_counter() - t0
    tok = Tokenizer.from_file(str(ROOT / "tokenizer.json"))
    tok.enable_truncation(MAXLEN)
    pdt = np.float32 if a.variant == "fp32" else np.float16
    names = [i.name for i in sess.get_inputs()]
    layers = sum(1 for n in names if n.endswith(".key"))

    def embed(texts):
        encs = [tok.encode(t) for t in texts]
        L = max(len(e.ids) for e in encs)
        B = len(encs)
        ids = np.zeros((B, L), np.int64)
        am = np.zeros((B, L), np.int64)
        for i, e in enumerate(encs):
            ids[i, : len(e.ids)] = e.ids
            am[i, : len(e.ids)] = 1
        feed = {
            "input_ids": ids,
            "attention_mask": am,
            "position_ids": np.tile(np.arange(L, dtype=np.int64), (B, 1)),
        }
        for l in range(layers):
            feed[f"past_key_values.{l}.key"] = np.zeros((B, 8, 0, 128), pdt)
            feed[f"past_key_values.{l}.value"] = np.zeros((B, 8, 0, 128), pdt)
        h = sess.run(["last_hidden_state"], feed)[0].astype(np.float64)
        last = h[np.arange(B), am.sum(1) - 1]
        return last / np.linalg.norm(last, axis=1, keepdims=True)

    texts = [INSTR + t for t in make_texts(a.n)]
    t = time.perf_counter()
    embed(texts[:1])
    first = time.perf_counter() - t
    res = {
        "variant": a.variant,
        "provider": a.provider,
        "active": sess.get_providers(),
        "load_s": load_s,
        "first_call_s": first,
        "bs": {},
    }
    for bs in map(int, a.bs.split(",")):
        embed(texts[:bs])  # shape warm-up
        lats, parts = [], []
        w0 = time.perf_counter()
        for i in range(0, len(texts), bs):
            t = time.perf_counter()
            parts.append(embed(texts[i : i + bs]))
            lats.append(time.perf_counter() - t)
        w = time.perf_counter() - w0
        res["bs"][bs] = {
            "items_per_s": len(texts) / w,
            "p50_ms": float(np.percentile(lats, 50) * 1e3),
            "p95_ms": float(np.percentile(lats, 95) * 1e3),
        }
        np.save(f"{a.out}_bs{bs}.npy", np.concatenate(parts))
    res["rss_delta_mb"] = (proc.memory_info().rss - rss0) / 2**20
    print("JSON:" + json.dumps(res))


if __name__ == "__main__":
    main()
