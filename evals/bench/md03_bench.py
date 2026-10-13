"""MD-03 worker: one (model, provider) run -> JSON on stdout + npy of outputs.

Synthetic invented texts only (no benchmark text). Run via md03_run.py.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

import numpy as np
import psutil

BS = (1, 8, 32, 64)
N = 96
REPEATS = 2

_SENT = {
    "en": ["The ferry to Halvorsen Island leaves at dawn on Tuesdays", "Mira prefers oat milk and refuses to drink anything with sugar",
           "Our quarterly offsite moved to the lakeside cabin because the venue flooded", "He repotted the fig tree after the cat knocked it over twice"],
    "de": ["Die Fähre nach Halvorsen fährt dienstags im Morgengrauen ab", "Lena trinkt nur Hafermilch und meidet jeden Zucker",
           "Das Treffen wurde in die Hütte am See verlegt, weil der Saal überschwemmt war"],
    "es": ["El ferri a la isla de Halvorsen sale al amanecer los martes", "A Mira le gusta la leche de avena y evita el azúcar",
           "La reunión se trasladó a la cabaña junto al lago por la inundación"],
    "hi": ["हैल्वोर्सन द्वीप की नाव मंगलवार को सुबह निकलती है", "मीरा को जई का दूध पसंद है और वह चीनी से बचती है", "बाढ़ के कारण बैठक झील किनारे की झोपड़ी में हुई"],
    "zh": ["去哈尔沃森岛的渡船每周二黎明出发", "米拉喜欢燕麦奶，从不喝加糖的饮料", "因为场地被淹，季度会议改到了湖边小屋"],
    "ja": ["ハルボルセン島行きのフェリーは火曜の夜明けに出ます", "ミラはオーツミルクが好きで砂糖を避けている", "会場が浸水したため、会議は湖畔の小屋に変更された"],
}


def make_texts(n: int = N, seed: int = 7) -> list[str]:
    rnd = random.Random(seed)
    pool = [s for v in _SENT.values() for s in v]
    out = []
    for i in range(n):
        if i % 2 == 0:  # short
            out.append(rnd.choice(pool) + f" (note {i})")
        else:  # long: ~12-25 sentences, truncates at the model max length
            out.append(". ".join(rnd.choice(pool) for _ in range(rnd.randint(12, 25))) + f" [{i}]")
    return out


def pct(a, q):
    return float(np.percentile(a, q))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["embed", "rerank"], required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", choices=["cpu", "dml"], required=True)
    ap.add_argument("--device-id", type=int, default=1)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.provider == "dml" and a.device_id == 0:
        sys.exit("refusing DML device 0 (NVIDIA, busy)")
    if a.provider == "dml":  # DirectML-recommended session options (fastembed does not set them)
        import onnxruntime as ort
        _orig = ort.InferenceSession.__init__

        def _init(self, path, sess_options=None, providers=None, **kw):
            sess_options = sess_options or ort.SessionOptions()
            sess_options.enable_mem_pattern = False
            sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            _orig(self, path, sess_options=sess_options, providers=providers, **kw)

        ort.InferenceSession.__init__ = _init
    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    provs = ["CPUExecutionProvider"] if a.provider == "cpu" else [("DmlExecutionProvider", {"device_id": a.device_id}), "CPUExecutionProvider"]
    t0 = time.perf_counter()
    if a.kind == "embed":
        from fastembed import TextEmbedding
        m = TextEmbedding(model_name=a.model, providers=provs)
        sess = m.model.model
    else:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        m = TextCrossEncoder(model_name=a.model, providers=provs)
        sess = m.model.model
    load_s = time.perf_counter() - t0
    texts = make_texts()
    query = "When does the ferry to the island leave and who drinks oat milk?"

    def run(chunk):
        if a.kind == "embed":
            return np.array(list(m.embed(chunk, batch_size=len(chunk))), dtype=np.float64)
        return np.array(list(m.rerank(query, chunk, batch_size=len(chunk))), dtype=np.float64)

    t = time.perf_counter(); run(texts[:1]); first_call_s = time.perf_counter() - t  # includes DML kernel compile
    t = time.perf_counter(); run(texts[:8]); second_call_s = time.perf_counter() - t
    res = {"model": a.model, "kind": a.kind, "provider": a.provider, "active_providers": sess.get_providers(),
           "load_s": load_s, "first_call_s": first_call_s, "second_call_s_bs8": second_call_s, "bs": {}}
    cpu_pct = []
    for bs in BS:
        lat, wall = [], []
        outs = None
        for rep in range(REPEATS + 1):  # rep 0 = shape warm-up for this bs, discarded
            lats, parts = [], []
            psutil.cpu_percent(None)
            ct0 = sum(proc.cpu_times()[:2])
            w0 = time.perf_counter()
            for i in range(0, len(texts), bs):
                t = time.perf_counter(); parts.append(run(texts[i:i + bs])); lats.append(time.perf_counter() - t)
            w = time.perf_counter() - w0
            if rep == 0:
                outs = np.concatenate(parts)
                warm = w
                continue
            lat += lats; wall.append(w)
            res.setdefault("_c", {})[bs] = res.get("_c", {}).get(bs, []) + [(sum(proc.cpu_times()[:2]) - ct0) / w]
            cpu_pct.append(psutil.cpu_percent(None))
        res["bs"][bs] = {"items_per_s": len(texts) / float(np.median(wall)), "p50_ms": pct(lat, 50) * 1e3, "p95_ms": pct(lat, 95) * 1e3,
                         "shape_warmup_s": warm, "proc_cpu_cores": float(np.mean(res["_c"][bs])), "sys_cpu_pct": float(np.mean(cpu_pct[-REPEATS:]))}
        np.save(f"{a.out}_bs{bs}.npy", outs)
    res.pop("_c")
    res["rss_delta_mb"] = (proc.memory_info().rss - rss0) / 2**20
    res["rss_peak_mb"] = proc.memory_info().peak_wset / 2**20 if hasattr(proc.memory_info(), "peak_wset") else None
    print("JSON:" + json.dumps(res))


if __name__ == "__main__":
    main()
