"""Generate the local Qwen-stack arm configs (embedder x reranker grid) under evals/arms/.

Base = ``local-combo-A`` (the plan-v3.2 local retrieval baseline); only ``embedding`` and the
``read.rerank*`` keys change, so every cell differs from its neighbours in one component.

    python gen_qwen_stack_arms.py
"""

from __future__ import annotations

import json
from pathlib import Path

ARMS = Path(__file__).parent / "arms"
BASE = json.loads((ARMS / "local-combo-A.json").read_text(encoding="utf-8"))

QWEN_QUERY = (
    "Instruct: Given a question about a user's past conversations, "
    "retrieve memories that answer it\nQuery: "
)
EMBEDDERS = {
    "ejina": {
        "provider": "st",
        "model": "jinaai/jina-embeddings-v5-text-small-retrieval",
        "dim": 1024,
        "device": "cuda",
        "dtype": "bfloat16",
        "query_prompt_name": "query",
        "document_prompt_name": "document",
        "trust_remote_code": True,
    },
    "ebge": {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
    "ebgeb": {"provider": "fastembed", "model": "BAAI/bge-base-en-v1.5"},
    "eq06": {
        "provider": "st",
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "dim": 1024,
        "device": "cuda",
        "dtype": "bfloat16",
        "query_instruction": QWEN_QUERY,
    },
}
RERANKERS = {
    "rjina": {"rerank": "jina", "rerank_model": "jinaai/jina-reranker-v3.5", "rerank_device": "cuda"},
    "roff": {"rerank": "off"},
    "rbge": {"rerank": "fastembed", "rerank_model": "BAAI/bge-reranker-base"},
    "rq06": {
        "rerank": "qwen3",
        "rerank_model": "Qwen/Qwen3-Reranker-0.6B",
        "rerank_device": "cuda",
    },
    "rq4b4": {
        "rerank": "qwen3",
        "rerank_model": "Qwen/Qwen3-Reranker-4B",
        "rerank_device": "cuda",
        "rerank_quant": "4bit",
    },
}


def main() -> None:
    for e, emb in EMBEDDERS.items():
        for r, rr in RERANKERS.items():
            cfg = json.loads(json.dumps(BASE))
            cfg["embedding"] = emb
            cfg["read"].update(rr)
            (ARMS / f"qs-{e}-{r}.json").write_text(json.dumps(cfg), encoding="utf-8")
            print(f"qs-{e}-{r}")


if __name__ == "__main__":
    main()
