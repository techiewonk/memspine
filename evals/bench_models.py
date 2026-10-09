"""Throughput of every model in the local stack, per token.

    python evals/bench_models.py [out.json]

Embedders: 256 passages (~70 tokens each) in batches of 32 (and 1 for the unbatched cost).
Rerankers: one query against a pool of 30 passages, repeated; tokens = query+passage per pair.
LLM (Ollama): prefill tokens/s from a long prompt and decode tokens/s, thinking off.
Numbers depend on what else is using the GPU; the run records nvidia-smi load at start.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).parent.resolve()]

import torch  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

BASE = ("On day {i}, Caroline told Melanie about the adoption agency she visited, how the meeting with "
        "the counsellor went, and her plans for the weekend with the kids.")
PASSAGES = [BASE.format(i=i) for i in range(256)]
QUERY = "What did Caroline tell Melanie about the adoption agency?"
OUT: list[dict] = []


def ntokens(model_id: str, texts: list[str]) -> int:
    try:
        tok = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    except Exception:  # noqa: BLE001
        tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B")
    return sum(len(tok(t)["input_ids"]) for t in texts)


def add(name: str, tokens: int, secs: float, **extra) -> None:
    row = {"model": name, "tokens": tokens, "secs": round(secs, 3),
           "tokens_per_s": round(tokens / secs), "ms_per_token": round(secs / tokens * 1000, 3), **extra}
    OUT.append(row)
    print(f"{name:52s} {row['tokens_per_s']:>8} tok/s  {row['ms_per_token']:>8} ms/tok  {extra}", flush=True)


async def bench_embedder(name: str, model_id: str, emb) -> None:
    await emb.embed(PASSAGES[:8])  # warm-up / load
    toks = ntokens(model_id, PASSAGES)
    t = time.time()
    for i in range(0, 256, 32):
        await emb.embed(PASSAGES[i:i + 32])
    add(f"{name} [embed, batch 32]", toks, time.time() - t, texts_per_s=round(256 / (time.time() - t)))
    n1 = 32
    t = time.time()
    for p in PASSAGES[:n1]:
        await emb.embed([p])
    add(f"{name} [embed, batch 1]", ntokens(model_id, PASSAGES[:n1]), time.time() - t)


async def bench_reranker(name: str, model_id: str, rr) -> None:
    pool = PASSAGES[:30]
    await rr.rerank(QUERY, pool[:4])  # warm-up / load
    toks = ntokens(model_id, [QUERY + " " + p for p in pool])
    reps = 3
    t = time.time()
    for _ in range(reps):
        await rr.rerank(QUERY, pool)
    add(f"{name} [rerank, pool 30]", toks * reps, time.time() - t, pairs_per_s=round(30 * reps / (time.time() - t), 1))


def bench_llm() -> None:
    def chat(prompt: str, n: int) -> dict:
        body = {"model": "qwen3.5:9b", "stream": False, "think": False,
                "messages": [{"role": "user", "content": prompt}], "options": {"num_predict": n, "temperature": 0}}
        req = urllib.request.Request("http://localhost:11434/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=600))

    chat("hi", 4)  # load
    long_prompt = " ".join(PASSAGES[:60]) + "\n\nSummarise the above in one sentence."
    d = chat(long_prompt, 1)
    add("qwen3.5:9b Q4_K_M [Ollama prefill]", d["prompt_eval_count"], d["prompt_eval_duration"] / 1e9)
    d = chat("Write a detailed story about a lighthouse keeper.", 200)
    add("qwen3.5:9b Q4_K_M [Ollama decode]", d["eval_count"], d["eval_duration"] / 1e9)


async def main() -> None:
    from memspine.services.embedding.fastembed_local import FastembedEmbedding
    from memspine.services.embedding.st_local import SentenceTransformersEmbedding
    from memspine.services.rerank.fastembed_rerank import FastembedReranker
    from memspine.services.rerank.jina_rerank import JinaReranker
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    mode = sys.argv[2] if len(sys.argv) > 2 else "all"  # all | llm | nollm
    load = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader"],
                          capture_output=True, text=True).stdout.strip()
    print("GPU load at start:", load, "|", torch.cuda.get_device_name(0), flush=True)
    if mode == "llm":
        bench_llm()
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text(json.dumps({"gpu_load_at_start": load, "rows": OUT}, indent=1), encoding="utf-8")
        return
    st = dict(device="cuda", dtype="bfloat16")
    await bench_embedder("bge-small-en-v1.5 (fastembed, CPU ONNX)", "BAAI/bge-small-en-v1.5", FastembedEmbedding("BAAI/bge-small-en-v1.5"))
    await bench_embedder("bge-base-en-v1.5 (fastembed, CPU ONNX)", "BAAI/bge-base-en-v1.5", FastembedEmbedding("BAAI/bge-base-en-v1.5"))
    await bench_embedder("Qwen3-Embedding-0.6B (GPU bf16)", "Qwen/Qwen3-Embedding-0.6B",
                         SentenceTransformersEmbedding("Qwen/Qwen3-Embedding-0.6B", 1024, **st))
    await bench_embedder("jina-embeddings-v5-text-small (GPU bf16)", "jinaai/jina-embeddings-v5-text-small-retrieval",
                         SentenceTransformersEmbedding("jinaai/jina-embeddings-v5-text-small-retrieval", 1024,
                                                       document_prompt_name="document", trust_remote_code=True, **st))
    await bench_reranker("bge-reranker-base (fastembed, CPU ONNX)", "BAAI/bge-reranker-base", FastembedReranker("BAAI/bge-reranker-base"))
    await bench_reranker("Qwen3-Reranker-0.6B (GPU)", "Qwen/Qwen3-Reranker-0.6B", Qwen3Reranker("Qwen/Qwen3-Reranker-0.6B", device="cuda"))
    await bench_reranker("Qwen3-Reranker-4B (GPU, 4-bit)", "Qwen/Qwen3-Reranker-4B", Qwen3Reranker("Qwen/Qwen3-Reranker-4B", device="cuda", quant="4bit"))
    await bench_reranker("jina-reranker-v3.5 (GPU)", "jinaai/jina-reranker-v3.5", JinaReranker(device="cuda"))
    if mode != "nollm":
        bench_llm()
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps({"gpu_load_at_start": load, "rows": OUT}, indent=1), encoding="utf-8")


asyncio.run(main())
