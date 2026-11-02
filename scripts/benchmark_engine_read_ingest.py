#!/usr/bin/env python
"""RM-26 / PA-053: Engine.read latency with and without the reranker, and concurrent
ingest plus search, on the reference host (PA-051 records, scripts/perf_protocol.py).

Both parts use the ``base`` template as shipped (rules-only derivation, onnx bge-small
embedder, SQLite + LanceDB + Tantivy, outbox on) with only the storage path set. Seeded
corpora are invented chat turns (``perf_protocol.corpus_messages``), 20 turns per session,
ingested through ``Engine.ingest_messages`` and flushed (``flush_windows``) before timing.

* ``read``: for each corpus size, ``Engine.read`` p50/p95/p99 over invented questions,
  first with ``model.rerank.chain: []`` (the base default) and then, reopening the same
  store, with the onnx MiniLM cross-encoder (``chain: [{provider: onnx}]``, default
  settings). The difference is the reranker's added cost.
* ``concurrent``: a copy of the seeded 1k store per rate; an open-loop writer sends one
  message per ``ingest_messages`` call at 1, 10 and 50 msgs/s while ``--readers`` closed-loop
  readers call ``Engine.read``. Records read latency, write-ack latency (call return) and
  derivation lag: time from the ack to ``wait_for_derivation(..., ("facts",))`` reporting
  the facts capability no longer pending, polled every ``--poll-ms`` for a sample of about
  two messages per second. After the writes stop
  the poll continues for ``--tail-s`` (longer than ``write.window_idle_s``) so messages in
  a still-open window are counted rather than dropped.

    $env:PYTHONPATH='src'; $env:HF_HUB_OFFLINE='1'
    python scripts/benchmark_engine_read_ingest.py --part read --work-dir <scratch>
    python scripts/benchmark_engine_read_ingest.py --part concurrent --work-dir <scratch>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perf_protocol import (
    ROOT,
    SENDER,
    Recorder,
    corpus_messages,
    corpus_queries,
    percentile,
    summarize,
)

from memspine import Engine
from memspine.observability.logging import configure_logging

DATE = "2026-11-02"
OUT_DIR = ROOT / "docs" / "provider_verification"
NS = "corpus"
SESSION_TURNS = 20
RERANK_ON = {"rerank": {"chain": [{"provider": "onnx", "settings": {}}]}}


def _config(data: Path, *, rerank: bool) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(data / "memspine.db"), "busy_timeout_s": 5.0},
            }
        },
        "deployment": {"data_dir": str(data)},
    }
    if rerank:
        cfg["model"] = RERANK_ON
    return cfg


async def _open(data: Path, *, rerank: bool) -> tuple[Engine, float]:
    start = time.perf_counter()
    eng = Engine(template="base", user_config=_config(data, rerank=rerank), dotenv_path=None)
    await eng.start()
    return eng, (time.perf_counter() - start) * 1000


async def seed(
    work: Path,
    size: int,
    *,
    name: str | None = None,
    reuse: bool = True,
    max_seed_s: float = float("inf"),
) -> dict[str, Any]:
    """Seed (or reuse) ``work/<name or seed-<size>>``; returns the seeding facts."""
    data = work / (name or f"seed-{size}")
    marker = data / "seed.json"
    if reuse and marker.exists():
        info = json.loads(marker.read_text("utf-8"))
        info["reused"] = True
        return info
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    eng, start_ms = await _open(data, rerank=False)
    texts = corpus_messages(size, seed=size)
    t0 = time.perf_counter()
    per_call: list[float] = []
    seeded = 0
    for i in range(0, size, SESSION_TURNS):
        if time.perf_counter() - t0 > max_seed_s:
            break  # bounded run: the store holds what was seeded so far
        batch = [
            {"role": "user", "sender": SENDER, "content": text}
            for text in texts[i : i + SESSION_TURNS]
        ]
        c0 = time.perf_counter()
        await eng.ingest_messages(batch, namespace=NS, session_id=f"seed-{i // SESSION_TURNS}")
        per_call.append((time.perf_counter() - c0) * 1000)
        seeded += len(batch)
    ingest_s = time.perf_counter() - t0
    # Ingest cost by corpus position: per-call time of each tenth of the seeding calls.
    tenth = max(1, len(per_call) // 10)
    progression = [
        {
            "messages_before": k * SESSION_TURNS,
            "call_p50_ms": round(percentile(per_call[k : k + tenth], 50), 1),
        }
        for k in range(0, len(per_call), tenth)
    ]
    f0 = time.perf_counter()
    flushed = await eng.flush_windows(NS, final=True)
    flush_s = time.perf_counter() - f0
    await eng.stop()
    info = {
        "requested_size": size,
        "size": seeded,
        "truncated_by_time_budget": seeded < size,
        "max_seed_s": max_seed_s,
        "sessions": -(-seeded // SESSION_TURNS),
        "engine_start_ms": round(start_ms, 1),
        "ingest_wall_s": round(ingest_s, 2),
        "ingest_msgs_per_s": round(seeded / ingest_s, 1),
        "ingest_call_20_turns_ms": summarize(per_call),
        "ingest_call_progression": progression,
        "flush_windows_s": round(flush_s, 2),
        "flush_counters": {k: v for k, v in flushed.items() if isinstance(v, (int, float))},
        "reused": False,
    }
    marker.write_text(json.dumps(info), "utf-8")
    return info


def _context_tokens(result: Any) -> int:
    return int(getattr(result.context, "tokens_used", 0))


async def read_series(data: Path, *, rerank: bool, reps: int, warmups: int) -> dict[str, Any]:
    from memspine.read.pipeline import rerank as rerank_module

    eng, start_ms = await _open(data, rerank=rerank)
    queries = corpus_queries(reps + warmups, seed=17)
    stage_ms: list[float] = []
    stage_docs: list[int] = []
    original = rerank_module.gated_rerank

    async def timed_gated_rerank(reranker: Any, query: str, documents: Any) -> Any:
        t0 = time.perf_counter()
        out = await original(reranker, query, documents)
        stage_ms.append((time.perf_counter() - t0) * 1000)
        stage_docs.append(len(documents))
        return out

    rerank_module.gated_rerank = timed_gated_rerank  # type: ignore[assignment]
    try:
        for q in queries[:warmups]:
            await eng.read(q, namespace=NS)
        stage_ms.clear()
        stage_docs.clear()
        calls0 = int(getattr(eng, "_rerank_calls", 0))
        fails0 = int(getattr(eng, "_rerank_failures", 0))
        samples: list[float] = []
        tokens: list[int] = []
        modes: dict[str, int] = {}
        for q in queries[warmups:]:
            t0 = time.perf_counter()
            res = await eng.read(q, namespace=NS)
            samples.append((time.perf_counter() - t0) * 1000)
            tokens.append(_context_tokens(res))
            modes[res.mode] = modes.get(res.mode, 0) + 1
            records = len(res.context.records)
        return {
            "rerank_chain": "[onnx MiniLM-L6]" if rerank else "[]",
            "engine_start_ms": round(start_ms, 1),
            "read_ms": summarize(samples, warmups=warmups),
            "context_tokens_p50": int(percentile(tokens, 50)),
            "context_records_last": records,
            "modes": modes,
            "reranker_calls_measured": int(getattr(eng, "_rerank_calls", 0)) - calls0,
            "reranker_failures_measured": int(getattr(eng, "_rerank_failures", 0)) - fails0,
            "rerank_model_call_ms": summarize(stage_ms) if stage_ms else None,
            "rerank_documents_per_call": (
                {"min": min(stage_docs), "max": max(stage_docs)} if stage_docs else None
            ),
        }
    finally:
        rerank_module.gated_rerank = original  # type: ignore[assignment]
        await eng.stop()


async def part_read(args: argparse.Namespace) -> None:
    rec = Recorder(
        "rm26-read-latency-reranker",
        OUT_DIR / f"perf-rm26-read-latency-reranker-{DATE}.json",
        allow_dirty=args.allow_dirty,
        ignore=[OUT_DIR],
        settings={
            "template": "base (rules-only, onnx bge-small, SQLite + LanceDB + Tantivy)",
            "sizes": args.sizes,
            "max_seed_s": args.max_seed_s,
            "reps": args.reps,
            "warmups": args.warmups,
            "queries": "perf_protocol.corpus_queries (invented)",
            "read": "Engine.read(query, namespace) with template defaults (replay mode, top_k 16)",
            "rerank_on": RERANK_ON,
            "logging": "WARNING (console INFO logging would time the terminal)",
        },
    )
    work = Path(args.work_dir)
    for size in args.sizes:
        info = await seed(work, size, max_seed_s=args.max_seed_s)
        data = work / f"seed-{size}"
        off = await read_series(data, rerank=False, reps=args.reps, warmups=args.warmups)
        on = await read_series(data, rerank=True, reps=args.reps, warmups=args.warmups)
        added = {
            q: round(on["read_ms"][f"{q}_ms"] - off["read_ms"][f"{q}_ms"], 2)
            for q in ("p50", "p95", "p99")
        }
        rec.record["results"][f"corpus_{size}"] = {
            "seed": info,
            "rerank_off": off,
            "rerank_on": on,
            "reranker_added_ms": added,
        }
        print(size, off["read_ms"], on["read_ms"], added, flush=True)
    rec.finish()


# ----------------------------------------------------------------------------- concurrent


async def run_rate(
    eng: Engine,
    label: str,
    rate: float,
    duration: float,
    batch: int,
    readers: int,
    poll_ms: int,
    tail_s: float,
) -> dict[str, Any]:
    """One open-loop load step on an already started engine.

    ``batch`` messages go in one ``ingest_messages`` call, ``rate / batch`` calls per second.
    """
    # Track the lag of about two messages per second so the status polling stays a small
    # load next to the measured writes and reads.
    sample = max(1, round(rate / 2))
    n = int(rate * duration)
    texts = corpus_messages(n + 10, seed=int(rate * 1000) + batch)
    queries = corpus_queries(400, seed=23)
    acks: list[float] = []
    reads: list[float] = []
    acked_at: dict[str, float] = {}
    lags: dict[str, float] = {}
    final_state: dict[str, str] = {}
    errors: list[str] = []
    writing = True
    calls = 0
    acked_msgs = 0

    async def write_call(first: int, scheduled: float) -> None:
        nonlocal acked_msgs
        ids = [f"{label}-{i}-{uuid.uuid4().hex[:8]}" for i in range(first, min(first + batch, n))]
        msgs = [
            {"message_id": mid, "role": "user", "sender": SENDER, "content": texts[first + k]}
            for k, mid in enumerate(ids)
        ]
        try:
            await eng.ingest_messages(
                msgs, namespace=NS, session_id=f"{label}-{first // SESSION_TURNS}"
            )
        except Exception as exc:  # recorded, not fatal
            errors.append(f"write: {type(exc).__name__}: {exc}"[:300])
            return
        now = time.perf_counter()
        acks.append((now - scheduled) * 1000)
        acked_msgs += len(ids)
        for k, mid in enumerate(ids):
            if (first + k) % sample == 0:
                acked_at[mid] = now

    async def writer() -> None:
        nonlocal calls
        tasks = []
        t0 = time.perf_counter()
        for first in range(0, n, batch):
            due = t0 + first / rate
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(write_call(first, due)))
            calls += 1
        await asyncio.gather(*tasks)

    async def reader(k: int) -> None:
        i = k
        while writing:
            t0 = time.perf_counter()
            try:
                await eng.read(queries[i % len(queries)], namespace=NS)
            except Exception as exc:
                errors.append(f"read: {type(exc).__name__}: {exc}"[:300])
            reads.append((time.perf_counter() - t0) * 1000)
            i += readers
            await asyncio.sleep(0.05)

    async def lag_poll() -> None:
        while True:
            pending = [m for m in acked_at if m not in lags]
            if pending:
                now = time.perf_counter()
                try:
                    waited = await eng.wait_for_derivation(
                        pending, namespace=NS, capabilities=("facts",), timeout_s=0.0
                    )
                except Exception as exc:
                    errors.append(f"poll: {type(exc).__name__}: {exc}"[:300])
                    waited = {}
                for mid, res in waited.items():
                    state = str(res.readiness.facts)
                    final_state[mid] = state
                    if not res.timed_out:
                        lags[mid] = (now - acked_at[mid]) * 1000
            if not writing and len(lags) == len(acked_at):
                return
            await asyncio.sleep(poll_ms / 1000)

    poll_task = asyncio.create_task(lag_poll()) if poll_ms > 0 else None
    reader_tasks = [asyncio.create_task(reader(k)) for k in range(readers)]
    w0 = time.perf_counter()
    await writer()
    write_wall = time.perf_counter() - w0
    writing = False
    await asyncio.gather(*reader_tasks)
    tail_complete = False
    if poll_task is not None:
        try:
            await asyncio.wait_for(poll_task, timeout=tail_s)
            tail_complete = True
        except TimeoutError:
            pass
    lag_values = list(lags.values())
    states: dict[str, int] = {}
    for mid in acked_at:
        state = final_state.get(mid, "unpolled")
        states[state] = states.get(state, 0) + 1
    return {
        "target_msgs_per_s": rate,
        "duration_s": duration,
        "messages_per_call": batch,
        "calls": calls,
        "messages": n,
        "acked_messages": acked_msgs,
        "achieved_msgs_per_s": round(acked_msgs / write_wall, 2),
        "write_wall_s": round(write_wall, 2),
        "write_ack_ms": summarize(acks),
        "write_ack_note": "per call, from its scheduled send time to its return (open loop)",
        "read_ms": summarize(reads),
        "readers": readers,
        "derivation_lag_ms": summarize(lag_values),
        "lag_sample_every": sample,
        "lag_tracked": len(acked_at),
        "derived": len(lag_values),
        "not_derived_within_tail": len(acked_at) - len(lag_values),
        "tail_complete": tail_complete,
        "final_facts_states": states,
        "errors": errors[:20],
        "error_count": len(errors),
    }


async def part_concurrent(args: argparse.Namespace) -> None:
    steps = [
        (f"{mode}_{rate:g}", rate, duration, 1 if mode == "single" else max(1, round(rate / 2)))
        for mode in args.modes
        for rate, duration in zip(args.rates, args.durations, strict=True)
        if not (mode == "batched" and rate <= 2)  # batched is single at <= 2 msgs/s
    ]
    rec = Recorder(
        "pa053-concurrent-ingest-search",
        OUT_DIR / f"perf-pa053-concurrent-ingest-search-{DATE}.json",
        allow_dirty=args.allow_dirty,
        ignore=[OUT_DIR],
        settings={
            "template": "base (rules-only, onnx bge-small, outbox on, window_async on)",
            "seed_corpus": args.seed_size,
            "steps": [
                {"label": s[0], "rate": s[1], "duration_s": s[2], "messages_per_call": s[3]}
                for s in steps
            ],
            "readers": args.readers,
            "reader_think_ms": 50,
            "poll_ms": args.poll_ms,
            "tail_s": args.tail_s,
            "write_call": (
                "single: ingest_messages([one message]) per send; batched: rate/2 messages per "
                "call, two calls per second; 20 messages per session"
            ),
            "store": (
                "one seeded store; steps run in order on it, so the namespace grows by the "
                "messages of the earlier steps"
            ),
            "lag_definition": "ack -> wait_for_derivation(facts) not pending (poll resolution)",
            "logging": "WARNING",
        },
    )
    work = Path(args.work_dir)
    # A store is bound to its path (the projector binding fingerprint), so it is seeded in
    # place rather than copied.
    rec.record["results"]["seed"] = await seed(
        work, args.seed_size, name="live", reuse=False, max_seed_s=args.max_seed_s
    )
    eng, start_ms = await _open(work / "live", rerank=False)
    rec.record["results"]["engine_start_ms"] = round(start_ms, 1)
    written = args.seed_size
    try:
        for label, rate, duration, batch in steps:
            row = await run_rate(
                eng, label, rate, duration, batch, args.readers, args.poll_ms, args.tail_s
            )
            row["namespace_messages_before"] = written
            written += row["acked_messages"]
            rec.record["results"][label] = row
            print(
                label,
                row["achieved_msgs_per_s"],
                row["write_ack_ms"].get("p95_ms"),
                row["read_ms"].get("p95_ms"),
                row["derivation_lag_ms"].get("p95_ms"),
                row["not_derived_within_tail"],
                flush=True,
            )
    finally:
        await eng.stop()
    for label in ("batched_10", "single_10"):
        ten = rec.record["results"].get(label)
        if not ten:
            continue
        rec.record["results"][f"proposed_budgets_{label}"] = {
            "status": "proposal only; owner approves (ADR-096)",
            "rule": "measured p95 x 1.3, rounded up",
            **{
                name: _budget(ten[key].get("p95_ms"))
                for name, key in (
                    ("read_p95_ms", "read_ms"),
                    ("write_ack_p95_ms", "write_ack_ms"),
                    ("derivation_lag_p95_ms", "derivation_lag_ms"),
                )
            },
        }
    rec.finish()


def _budget(p95: float | None) -> float | None:
    if p95 is None:
        return None
    raw = p95 * 1.3
    step = 5 if raw < 100 else 10 if raw < 1000 else 100
    return float(-(-raw // step) * step)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=("read", "concurrent"), required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--reps", type=int, default=100)
    parser.add_argument("--warmups", type=int, default=10)
    parser.add_argument("--seed-size", type=int, default=1000)
    parser.add_argument("--max-seed-s", type=float, default=600.0)
    parser.add_argument("--rates", type=float, nargs="+", default=[1.0, 10.0, 50.0])
    parser.add_argument("--durations", type=float, nargs="+", default=[60.0, 30.0, 10.0])
    parser.add_argument(
        "--modes", nargs="+", choices=("single", "batched"), default=["single", "batched"]
    )
    parser.add_argument("--readers", type=int, default=2)
    parser.add_argument("--poll-ms", type=int, default=500)
    parser.add_argument("--tail-s", type=float, default=150.0)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    configure_logging("WARNING")
    Path(args.work_dir).mkdir(parents=True, exist_ok=True)
    if args.part == "read":
        asyncio.run(part_read(args))
    else:
        asyncio.run(part_concurrent(args))


if __name__ == "__main__":
    main()
