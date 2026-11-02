#!/usr/bin/env python
"""PA-053 follow-up: where the time of one write, one single-message ingest and one read goes.

Opens an already seeded base-template store (``benchmark_engine_read_ingest.py`` leaves one
under ``<work-dir>/live``), alone on the host, and measures:

* ``Engine.write`` (episodic) and ``Engine.ingest_messages`` of one message, ``--reps`` each
  after a warm-up, under cProfile (top-20 cumulative functions per operation) and with
  wall-clock spans around the write-path stages: embedding, the intake ledger transactions
  (intent record/apply/lookup), derivation discovery, entity-index resolution and relink,
  the event append and offsets, every projector apply (records, vector/LanceDB,
  lexical/Tantivy, graph, entity index), the LanceDB upsert and the Tantivy index/commit
  calls (in their worker threads), window observe, the outbox job store (synchronous SQL)
  and every SQL statement by kind (``BEGIN IMMEDIATE`` waits, ``COMMIT`` with its fsync,
  ``PRAGMA wal_checkpoint``), attributed to the operation and to the thread it ran on;
* the background derivation step, by running ``flush_windows`` under the same spans;
* one ``Engine.read`` alone and one issued while a 20-message ingest is in flight, with
  ``asyncio.Lock`` waits recorded, to show whether the read queues behind the write.

Spans are inclusive and can nest (a projector apply contains its SQL), so they do not sum
to the operation time. The record follows the PA-051 protocol (scripts/perf_protocol.py).

    $env:PYTHONPATH='src'; $env:HF_HUB_OFFLINE='1'
    python scripts/profile_write_path.py --work-dir <scratch>
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import cProfile
import functools
import inspect
import io
import pstats
import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark_engine_read_ingest import NS, _open
from perf_protocol import ROOT, SENDER, Recorder, corpus_messages, corpus_queries, summarize

from memspine.observability.logging import configure_logging

DATE = "2026-11-02"
OUT_DIR = ROOT / "docs" / "provider_verification"

OP: contextvars.ContextVar[str] = contextvars.ContextVar("op", default="other")
#: (op, span) -> list of (ms, thread kind)
SPANS: dict[tuple[str, str], list[float]] = defaultdict(list)
THREADS: dict[tuple[str, str], set[str]] = defaultdict(set)


def _thread() -> str:
    return "loop" if threading.current_thread() is threading.main_thread() else "worker"


def _note(span: str, ms: float) -> None:
    key = (OP.get(), span)
    SPANS[key].append(ms)
    THREADS[key].add(_thread())


def _wrap(span: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(fn, "__perf_span__", None):
        return fn
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                _note(span, (time.perf_counter() - t0) * 1000)

        awrapper.__perf_span__ = span  # type: ignore[attr-defined]
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            _note(span, (time.perf_counter() - t0) * 1000)

    wrapper.__perf_span__ = span  # type: ignore[attr-defined]
    return wrapper


def patch_function(module: str, name: str, span: str) -> None:
    """Replace ``module.name`` everywhere it is bound (``from x import name`` copies)."""
    original = getattr(sys.modules[module], name)
    wrapped = _wrap(span, original)
    for mod in list(sys.modules.values()):
        namespace = getattr(mod, "__dict__", None)
        if not isinstance(namespace, dict) or not str(getattr(mod, "__name__", "")).startswith(
            "memspine"
        ):
            continue
        for key, value in list(namespace.items()):
            if value is original:
                setattr(mod, key, wrapped)


def patch_method(owner: Any, name: str, span: str) -> None:
    if owner is None or not hasattr(owner, name):
        return
    setattr(owner, name, _wrap(span, getattr(owner, name)))


def patch_class_public(cls: type, prefix: str) -> None:
    for name, value in list(vars(cls).items()):
        if name.startswith("_") or not callable(value) or isinstance(value, (staticmethod, type)):
            continue
        setattr(cls, name, _wrap(f"{prefix}.{name}", value))


def install_sql_events() -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "before_cursor_execute")
    def _before(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        conn.info.setdefault("perf_t0", []).append(time.perf_counter())

    @event.listens_for(Engine, "after_cursor_execute")
    def _after(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        stack = conn.info.get("perf_t0") or []
        if not stack:
            return
        ms = (time.perf_counter() - stack.pop()) * 1000
        head = statement.lstrip().split(None, 2)
        kind = head[0].upper() if head else "?"
        if kind == "BEGIN" and len(head) > 1:
            kind = f"BEGIN {head[1].upper()}"
        if kind == "PRAGMA":
            kind = "PRAGMA " + statement.split()[1].split("(")[0]
        _note(f"sql.{kind}", ms)

    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection

    patch_method(Connection, "commit", "sql.Connection.commit()")
    patch_method(AsyncConnection, "commit", "sql.AsyncConnection.commit()")


def install_spans(eng: Any) -> None:
    from memspine.ingest import discovery
    from memspine.memories.associative import entity_index
    from memspine.runtime.outbox import SqlJobStore
    from memspine.services.storage.intake_sql import SqlIntakeLedger

    patch_function(entity_index.__name__, "with_entity_links", "entity.with_entity_links")
    patch_function(entity_index.__name__, "relink_events", "entity.relink_events")
    patch_function(
        discovery.__name__, "discover_committed_derivations", "discovery.discover_committed"
    )
    for name in (
        "record_intent",
        "apply_intent",
        "lookup_messages",
        "set_readiness",
        "intents_by_id",
        "discover_committed",
        "record_ids",
    ):
        patch_method(SqlIntakeLedger, name, f"intake.{name}")
    patch_class_public(SqlJobStore, "outbox")
    patch_method(eng._embedder, "embed", "embed.embed")
    patch_method(eng._embedder, "embed_queries", "embed.embed_queries")
    patch_method(eng._storage, "append_event", "events.append_event")
    patch_method(eng._storage, "set_offset", "events.set_offset")
    for projector in eng._projectors:
        patch_method(projector, "apply", f"project.{type(projector).__name__}.apply")
    patch_method(eng._vector, "upsert", "lancedb.upsert")
    patch_method(eng._vector, "search", "lancedb.search")
    if hasattr(eng._vector, "_write_pending"):  # RM-160: the batched table write
        patch_method(eng._vector, "_write_pending", "lancedb.write_pending")
    lexical = eng._lexical
    for name in ("_index_one", "_commit_if_pending", "_commit_index_many", "_search"):
        patch_method(lexical, name, f"tantivy.{name}")
    windows = eng._windows
    if windows is not None:
        patch_method(windows, "observe", "windows.observe")
        patch_method(windows, "_drain", "windows.drain")
        patch_method(windows, "_run_job", "windows.run_job")
        patch_method(windows, "_prepare_job", "windows.prepare_job (rules)")
    original_acquire = asyncio.Lock.acquire

    async def acquire(self: asyncio.Lock) -> bool:
        t0 = time.perf_counter()
        out = await original_acquire(self)
        waited = (time.perf_counter() - t0) * 1000
        if waited >= 0.5:
            _note("asyncio.Lock wait (>=0.5 ms)", waited)
        return out

    asyncio.Lock.acquire = acquire  # type: ignore[method-assign]


def span_table(op: str, reps: int) -> list[dict[str, Any]]:
    rows = []
    for (o, span), values in SPANS.items():
        if o != op:
            continue
        rows.append(
            {
                "span": span,
                "calls_per_op": round(len(values) / reps, 2),
                "ms_per_op": round(sum(values) / reps, 3),
                "per_call": summarize(values),
                "threads": sorted(THREADS[(o, span)]),
            }
        )
    rows.sort(key=lambda r: r["ms_per_op"], reverse=True)
    return rows


def top_cumulative(profile: cProfile.Profile, n: int = 20) -> list[dict[str, Any]]:
    stats = pstats.Stats(profile, stream=io.StringIO())
    rows = []
    for (path, line, func), (cc, nc, tt, ct, _) in stats.stats.items():  # type: ignore[attr-defined]
        rows.append(
            {
                "function": f"{Path(path).name}:{line}({func})",
                "path": path.replace("\\", "/").split("site-packages/")[-1].split("/src/")[-1],
                "ncalls": nc,
                "tottime_ms": round(tt * 1000, 2),
                "cumtime_ms": round(ct * 1000, 2),
            }
        )
    rows.sort(key=lambda r: r["cumtime_ms"], reverse=True)
    return rows[:n]


async def run(args: argparse.Namespace) -> None:
    data = Path(args.work_dir) / "live"
    if not (data / "seed.json").exists():
        raise SystemExit(f"{data} has no seeded store; run benchmark_engine_read_ingest first")
    out = OUT_DIR / f"perf-pa053-write-path-profile-{DATE}{args.suffix}.json"
    rec = Recorder(
        "pa053-write-path-profile",
        out,
        allow_dirty=args.allow_dirty,
        ignore=[OUT_DIR],
        settings={
            "store": "the seeded PA-053 store (<work-dir>/live), base template, rules-only",
            "reps": args.reps,
            "profiler": "cProfile (main thread only; worker-thread time shows as waits)",
            "spans": "wall-clock, inclusive, may nest; per operation averages over reps",
        },
    )
    install_sql_events()
    eng, start_ms = await _open(data, rerank=False)
    install_spans(eng)
    texts = corpus_messages(args.reps * 4 + 60, seed=99)
    queries = corpus_queries(10, seed=31)
    import json

    rec.record["results"]["seed"] = json.loads((data / "seed.json").read_text("utf-8"))
    rec.record["results"]["engine_start_ms"] = round(start_ms, 1)
    k = iter(range(10**6))

    async def do_write() -> None:
        await eng.write(texts[next(k)], namespace=NS, memory_type="episodic", session_id="prof-w")

    async def do_ingest() -> None:
        msg = {
            "message_id": f"prof-{uuid.uuid4().hex[:10]}",
            "role": "user",
            "sender": SENDER,
            "content": texts[next(k)],
        }
        await eng.ingest_messages([msg], namespace=NS, session_id="prof-i")

    async def do_read() -> None:
        await eng.read(queries[0], namespace=NS)

    try:
        for warm in (do_write, do_ingest, do_read):  # load models, warm caches
            OP.set("warmup")
            await warm()
        ops: dict[str, Any] = {}
        for name, fn in (("write", do_write), ("ingest_one", do_ingest), ("read", do_read)):
            OP.set(name)
            profile = cProfile.Profile()
            walls = []
            for _ in range(args.reps):
                t0 = time.perf_counter()
                profile.enable()
                await fn()
                profile.disable()
                walls.append((time.perf_counter() - t0) * 1000)
            ops[name] = {
                "wall_ms": summarize(walls),
                "spans": span_table(name, args.reps),
                "top20_cumulative": top_cumulative(profile),
            }
            print(name, ops[name]["wall_ms"]["p50_ms"], flush=True)
        # Background derivation: the windows the writes above opened, processed now.
        OP.set("derivation_flush")
        profile = cProfile.Profile()
        t0 = time.perf_counter()
        profile.enable()
        flushed = await eng.flush_windows(NS, final=True)
        profile.disable()
        ops["derivation_flush"] = {
            "wall_ms": round((time.perf_counter() - t0) * 1000, 2),
            "windows": flushed.get("windows_processed"),
            "spans": span_table("derivation_flush", 1),
            "top20_cumulative": top_cumulative(profile),
        }
        # A read issued while a 20-message ingest is in flight.
        batch = [
            {
                "message_id": f"prof-b-{uuid.uuid4().hex[:10]}",
                "role": "user",
                "sender": SENDER,
                "content": t,
            }
            for t in texts[-20:]
        ]
        concurrent_walls = []
        ingest_walls = []
        for _ in range(args.concurrent_reps):

            async def ingest_batch(b: list[dict[str, Any]] = batch) -> float:
                OP.set("concurrent_ingest")
                t0 = time.perf_counter()
                await eng.ingest_messages(
                    [{**m, "message_id": f"{m['message_id']}-{uuid.uuid4().hex[:6]}"} for m in b],
                    namespace=NS,
                    session_id="prof-c",
                )
                return (time.perf_counter() - t0) * 1000

            task = asyncio.create_task(ingest_batch())
            await asyncio.sleep(0.05)
            OP.set("read_during_ingest")
            t0 = time.perf_counter()
            await do_read()
            concurrent_walls.append((time.perf_counter() - t0) * 1000)
            ingest_walls.append(await task)
        ops["read_during_ingest"] = {
            "wall_ms": summarize(concurrent_walls),
            "concurrent_ingest_20_ms": summarize(ingest_walls),
            "spans": span_table("read_during_ingest", args.concurrent_reps),
            "ingest_spans": span_table("concurrent_ingest", args.concurrent_reps),
        }
        print("read_during_ingest", ops["read_during_ingest"]["wall_ms"]["p50_ms"], flush=True)
        rec.record["results"]["operations"] = ops
    finally:
        await eng.stop()
    rec.finish()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--concurrent-reps", type=int, default=5)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--suffix", default="", help="record file suffix, e.g. -after")
    args = parser.parse_args()
    configure_logging("WARNING")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
