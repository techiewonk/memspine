"""Offline statistics of the adaptive-window write (W1) on a conversation dataset. No LLM.

    python evals/window_stats_offline.py --data data/locomo10.json [--device cpu]
        [--model Qwen/Qwen3-Embedding-0.6B] [--cache-dir DIR] [--out evals/analysis/X.json]

Segments each conversation (one store per conversation, as the harness runs it) with the
production segmenter (`memspine.core.window`): the first of a topic shift (the store's own running
mean - k*sd of the consecutive-turn embedding similarity), 6 turns, ~1,500 tokens or the session
end. Embeddings come from the arm's embedder, run on the CPU by default so a live GPU queue is
untouched. Reports windows per session, turns per window, why windows closed, calls per turn, and
an ESTIMATE of the ingest time on the local Ollama stack (decode ~116 tok/s, prefill ~4,869 tok/s,
the rates measured for this stack in QWEN_STACK_RESULTS; the completion size per turn is the
measured size of the existing fact prompt, scaled for the extra JSON fields). The data is read
for the turn text and session ids only: no question, answer or evidence is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

#: Rates measured on this stack (docs/ENGINE_COMPARISON_AND_WRITE_PATH_DESIGN.html B5.1).
DECODE_TOK_S = 116.0
PREFILL_TOK_S = 4869.0
#: Fixed cost per call (request, queueing, JSON parse); an assumption, not a measurement.
CALL_OVERHEAD_S = 0.3
#: Completion tokens per turn for the facts section. The existing `extract@session4` prompt
#: measured 900-1,800 completion tokens per 21.6-turn LoCoMo session (42-83 per turn, YAML);
#: the window items add `span`, `speaker`, `subject` and `turns` and are JSON, so x1.5.
COMPLETION_PER_TURN = {"low": 63, "mid": 90, "high": 125}
#: JSON wrapper and the empty sections of one reply.
COMPLETION_PER_CALL = 20


def load_turns(path: Path) -> list[tuple[str, list[tuple[str, str]]]]:
    """``[(item id, [(session id, "Speaker: text"), ...])]`` through the harness adapter."""
    from memspine_evals.datasets.locomo import LoCoMoDataset

    out = []
    for item in LoCoMoDataset(path, revision_id="auto").items():
        out.append(
            (str(item.item_id), [(t.session_id, f"{t.speaker}: {t.text}") for t in item.history])
        )
    return out


def _cache_read(cache: Path | None, key: str) -> list[Any] | None:
    if cache is not None and (cache / f"{key}.json").exists():
        return json.loads((cache / f"{key}.json").read_text(encoding="utf-8"))
    return None


def _cache_write(cache: Path | None, key: str, vectors: list[Any]) -> None:
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{key}.json").write_text(json.dumps(vectors), encoding="utf-8")


async def embed_all(texts: list[str], model: str, device: str, cache: Path | None) -> list[Any]:
    from hashlib import sha256

    key = sha256(("\n".join(texts) + "|" + model).encode()).hexdigest()[:16]
    hit = _cache_read(cache, key)
    if hit is not None:
        return hit
    from memspine.services.embedding.st_local import SentenceTransformersEmbedding

    emb = SentenceTransformersEmbedding(
        model,
        1024,
        device=device,
        dtype="float32" if device == "cpu" else "bfloat16",
        batch_size=32,
    )
    vectors: list[list[float]] = []
    for start in range(0, len(texts), 256):
        vectors.extend(await emb.embed(texts[start : start + 256]))
        print(f"  embedded {len(vectors)}/{len(texts)}", file=sys.stderr, flush=True)
    _cache_write(cache, key, vectors)
    return vectors


def prompt_tokens(sections: tuple[str, ...]) -> int:
    """Tokens of the rendered `window_extract` system prompt for ``sections`` (4 chars each)."""
    from memspine.core.window import estimate_tokens
    from memspine.prompts.registry import PromptRegistry

    prompt = PromptRegistry().for_role("window_extract")
    msg = prompt.render(
        {"content": "", "participants": "", "turn_count": 0, "sections": list(sections)}
    )
    return sum(estimate_tokens(m["content"]) for m in msg)


def summarise(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "mean": round(statistics.fmean(values), 3),
        "median": float(statistics.median(values)),
        "p10": float(ordered[int(0.1 * (len(ordered) - 1))]),
        "p90": float(ordered[int(0.9 * (len(ordered) - 1))]),
        "min": float(ordered[0]),
        "max": float(ordered[-1]),
    }


def run(
    items: list[tuple[str, list[tuple[str, str]]]], vectors: list[Any], settings: Any, sections: Any
) -> dict[str, Any]:
    from collections import Counter

    from memspine.core.window import WindowTurn, estimate_tokens

    pos = 0
    windows_total = 0
    turns_total = 0
    reasons: Counter[str] = Counter()
    per_session: dict[tuple[str, str], int] = {}
    sizes: list[float] = []
    window_tokens: list[float] = []
    levels = []
    base = prompt_tokens(sections)
    seconds = {k: 0.0 for k in COMPLETION_PER_TURN}
    for item_id, history in items:
        turns = [
            WindowTurn(
                record_id=f"{item_id}:{i}",
                text=text,
                session=session,
                vector=vectors[pos + i],
                tokens=estimate_tokens(text),
            )
            for i, (session, text) in enumerate(history)
        ]
        pos += len(history)
        from memspine.core.window import WindowSegmenter

        seg = WindowSegmenter(settings)
        wins = []
        for turn in turns:
            wins.extend(seg.add(turn))
        tail = seg.close("flush")
        if tail is not None:
            wins.append(tail)
        levels.append(
            {"item": item_id, "pairs": seg.pairs, "level": seg.topic_level}
            if seg.topic_level is not None
            else {"item": item_id, "pairs": seg.pairs, "level": None}
        )
        for win in wins:
            windows_total += 1
            turns_total += len(win.turns)
            reasons[win.reason] += 1
            per_session[(item_id, win.session or "")] = (
                per_session.get((item_id, win.session or ""), 0) + 1
            )
            sizes.append(len(win.turns))
            window_tokens.append(win.tokens)
            prompt = base + win.tokens + 40  # + the user-message frame
            for name, per_turn in COMPLETION_PER_TURN.items():
                completion = per_turn * len(win.turns) + COMPLETION_PER_CALL
                seconds[name] += (
                    prompt / PREFILL_TOK_S + completion / DECODE_TOK_S + CALL_OVERHEAD_S
                )
    sessions = len(per_session)
    return {
        "conversations": len(items),
        "sessions": sessions,
        "turns": turns_total,
        "windows": windows_total,
        "calls_per_turn": round(windows_total / turns_total, 4),
        "windows_per_session": summarise([float(v) for v in per_session.values()]),
        "turns_per_window": summarise(sizes),
        "tokens_per_window": summarise(window_tokens),
        "windows_by_reason": dict(reasons),
        "share_by_reason": {k: round(v / windows_total, 4) for k, v in reasons.items()},
        "prompt_tokens_system": base,
        "est_ingest_seconds": {k: round(v, 1) for k, v in seconds.items()},
        "est_ingest_minutes": {k: round(v / 60, 1) for k, v in seconds.items()},
        "topic_level_per_conversation": levels,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--model", default="Qwen/Qwen3-Embedding-0.6B")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--cache-dir", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()
    if a.device == "cpu":
        import torch

        torch.set_num_threads(a.threads)  # leave the cores to the live queue
    from memspine.core.window import WindowSettings

    items = load_turns(a.data)
    texts = [text for _, history in items for _, text in history]
    print(f"{len(items)} conversations, {len(texts)} turns", file=sys.stderr)
    vectors = asyncio.run(embed_all(texts, a.model, a.device, a.cache_dir))
    result: dict[str, Any] = {"data": a.data.name, "embedder": a.model, "device": a.device}
    for label, settings in (
        ("default (topic_k 2.0, 6 turns, 1500 tokens)", WindowSettings()),
        ("topic boundary off (6 turns, 1500 tokens)", WindowSettings(topic_k=0.0)),
    ):
        stats = run(items, vectors, settings, ("facts",))
        result[label] = stats
        print(f"\n== {label}")
        for key in (
            "conversations",
            "sessions",
            "turns",
            "windows",
            "calls_per_turn",
            "windows_per_session",
            "turns_per_window",
            "tokens_per_window",
            "windows_by_reason",
            "share_by_reason",
            "est_ingest_minutes",
        ):
            print(f"  {key}: {stats[key]}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
