"""LLM serving benchmark on QA-shaped requests (OpenAI-compatible endpoint, e.g. Ollama /v1).

    python evals/bench_llm.py [--model qwen3.5:9b] [--n 12] [--concurrency 1 2 4] [--ctx-tokens 2000] [--label before]

Each request: a synthetic memory context of about --ctx-tokens tokens, a question, a short answer
(max_tokens 64, thinking off, temperature 0). Distinct contexts per request so the prompt cache does not
flatter the numbers. Reports requests/s, mean and p95 latency, and mean prompt/completion tokens per level of
concurrency, and appends a JSON line to evals/runs/_logs/bench_llm.jsonl.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import random
import statistics
import time
import urllib.request
from pathlib import Path

WORDS = ("Caroline Melanie painted adoption agency counselor camping lake sunrise support group charity race "
         "pottery kids beach concert museum library vacation garden festival bakery bike friends weekend").split()


def make_prompt(seed: int, ctx_tokens: int) -> str:
    rnd = random.Random(seed)
    lines, n = [], 0
    while n < ctx_tokens:
        day = f"2023-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
        text = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(12, 30)))
        line = f"[{day}] {rnd.choice(['Caroline', 'Melanie'])}: {text}."
        lines.append(line)
        n += len(line) // 4
    return ("Memories:\n" + "\n".join(lines) + f"\n\nQuestion: When did Melanie go to the {rnd.choice(WORDS)}?\n"
            "Answer in a short phrase.")


def call(base: str, model: str, prompt: str) -> tuple[float, int, int]:
    body = {"model": model, "temperature": 0, "max_tokens": 64, "reasoning_effort": "none",
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(f"{base}/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t = time.perf_counter()
    d = json.load(urllib.request.urlopen(req, timeout=600))
    u = d.get("usage") or {}
    return time.perf_counter() - t, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--ctx-tokens", type=int, default=2000)
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    call(args.base, args.model, "hi")  # load the model
    out = Path(__file__).parent / "runs" / "_logs" / "bench_llm.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    seed = int(time.time())
    for c in args.concurrency:
        prompts = [make_prompt(seed + i + 1000 * c, args.ctx_tokens) for i in range(args.n)]
        t = time.perf_counter()
        with cf.ThreadPoolExecutor(c) as ex:
            res = list(ex.map(lambda p: call(args.base, args.model, p), prompts))
        wall = time.perf_counter() - t
        lat = sorted(r[0] for r in res)
        row = {"label": args.label, "model": args.model, "concurrency": c, "n": args.n, "ctx_tokens": args.ctx_tokens,
               "wall_s": round(wall, 2), "req_per_s": round(args.n / wall, 3),
               "lat_mean_s": round(statistics.mean(lat), 2), "lat_p95_s": round(lat[min(len(lat) - 1, int(.95 * len(lat)))], 2),
               "prompt_tokens_mean": round(statistics.mean(r[1] for r in res)),
               "completion_tokens_mean": round(statistics.mean(r[2] for r in res), 1)}
        print(json.dumps(row))
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
