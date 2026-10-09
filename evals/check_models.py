"""Health check for every model in the local Qwen/Jina stack: one tiny known example each.

    python evals/check_models.py

Embedders and rerankers must put the relevant passage first on at least 4 of 5 questions; the LLM must answer a one-word prompt with thinking off.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

# evals/datasets (legacy) would shadow HuggingFace `datasets` for sentence-transformers.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).parent.resolve()]

import torch  # noqa: E402

CASES = [
    ("Which planet is known as the Red Planet?",
     "Mars, known for its reddish appearance, is the Red Planet.",
     ["Venus is often called Earth's twin because of its size.", "Saturn is famous for its rings."]),
    ("Where does Alice work?",
     "Alice works at a bakery in Leeds.",
     ["Bob likes hiking in the Alps.", "The train to Leeds leaves at noon."]),
    ("When did Caroline attend the support group?",
     "Caroline went to the LGBTQ support group on 7 May 2023.",
     ["Melanie painted a sunrise last year.", "The group meets in a community centre downtown."]),
    ("What instrument does Tom play?",
     "Tom plays the cello in a local orchestra.",
     ["Tom's sister owns a bicycle shop.", "The orchestra rehearses on Thursdays."]),
    ("What is Priya allergic to?",
     "Priya is allergic to peanuts and carries an epinephrine pen.",
     ["Priya enjoys spicy food from Kerala.", "Peanut butter sales rose last quarter."]),
]
RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)


async def check_embedder(name: str, emb) -> None:
    t = time.time()
    try:
        hits = 0
        for q, good, bad in CASES:
            qv = (await emb.embed_queries([q]))[0]
            dv = await emb.embed([good, *bad])
            sims = [sum(a * b for a, b in zip(qv, v)) for v in dv]
            hits += sims.index(max(sims)) == 0
        record(name, hits >= 4 and len(qv) == emb.dim, f"top-1 {hits}/{len(CASES)} dim={len(qv)} {time.time()-t:.0f}s")
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"{type(exc).__name__}: {str(exc)[:120]}")


async def check_reranker(name: str, rr) -> None:
    t = time.time()
    try:
        hits = 0
        for q, good, bad in CASES:
            sc = await rr.rerank(q, [good, *bad])
            hits += sc.index(max(sc)) == 0
        record(name, hits >= 4, f"top-1 {hits}/{len(CASES)} {time.time()-t:.0f}s")
    except Exception as exc:  # noqa: BLE001
        record(name, False, f"{type(exc).__name__}: {str(exc)[:120]}")


def check_llm() -> None:
    t = time.time()
    try:
        body = {"model": "qwen3.5:9b", "reasoning_effort": "none", "max_tokens": 50,
                "messages": [{"role": "user", "content": "Reply with the single word: ready"}]}
        req = urllib.request.Request("http://localhost:11434/v1/chat/completions",
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=120))
        m = d["choices"][0]["message"]
        record("qwen3.5:9b (Ollama, Q4_K_M)", "ready" in (m.get("content") or "").lower()
               and not m.get("reasoning"), f"{m.get('content')!r} tokens={d['usage']['completion_tokens']} {time.time()-t:.1f}s")
    except Exception as exc:  # noqa: BLE001
        record("qwen3.5:9b (Ollama)", False, f"{type(exc).__name__}: {str(exc)[:120]}")


async def main() -> None:
    from memspine.services.embedding.fastembed_local import FastembedEmbedding
    from memspine.services.embedding.st_local import SentenceTransformersEmbedding
    from memspine.services.rerank.fastembed_rerank import FastembedReranker
    from memspine.services.rerank.jina_rerank import JinaReranker
    from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

    print("cuda:", torch.cuda.is_available(), torch.cuda.get_device_name(0))
    check_llm()
    await check_embedder("bge-small-en-v1.5 (fastembed)", FastembedEmbedding("BAAI/bge-small-en-v1.5"))
    await check_embedder("bge-base-en-v1.5 (fastembed)", FastembedEmbedding("BAAI/bge-base-en-v1.5"))
    await check_embedder("Qwen3-Embedding-0.6B (st, bf16)", SentenceTransformersEmbedding(
        "Qwen/Qwen3-Embedding-0.6B", 1024, device="cuda", dtype="bfloat16",
        query_instruction="Instruct: Given a question, retrieve passages that answer it\nQuery: "))
    await check_embedder("jina-embeddings-v5-text-small-retrieval (st, bf16)", SentenceTransformersEmbedding(
        "jinaai/jina-embeddings-v5-text-small-retrieval", 1024, device="cuda", dtype="bfloat16",
        query_prompt_name="query", document_prompt_name="document", trust_remote_code=True))
    await check_reranker("bge-reranker-base (fastembed)", FastembedReranker("BAAI/bge-reranker-base"))
    await check_reranker("Qwen3-Reranker-0.6B (cuda)", Qwen3Reranker("Qwen/Qwen3-Reranker-0.6B", device="cuda"))
    await check_reranker("Qwen3-Reranker-4B (cuda, 4-bit)", Qwen3Reranker("Qwen/Qwen3-Reranker-4B", device="cuda", quant="4bit"))
    await check_reranker("jina-reranker-v3.5 (cuda)", JinaReranker(device="cuda"))
    print(f"\n{sum(r[1] == 'PASS' for r in RESULTS)}/{len(RESULTS)} passed")


asyncio.run(main())
