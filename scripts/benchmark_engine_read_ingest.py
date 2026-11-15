#!/usr/bin/env python
"""RM-26 / PA-053: Engine.read latency with and without the reranker, and concurrent
ingest plus search, on the reference host (PA-051 records, scripts/perf_protocol.py).

Both parts use the ``base`` template (rules-only derivation, onnx bge-small embedder,
SQLite + LanceDB + Tantivy, outbox on) with the storage path and rerank chain set explicitly. Seeded
corpora are invented chat turns (``perf_protocol.corpus_messages``), 20 turns per session,
ingested through ``Engine.ingest_messages`` and flushed (``flush_windows``) before timing.

* ``read``: for each corpus size, ``Engine.read`` p50/p95/p99 over invented questions,
  first with ``model.rerank.chain: []`` (rerank disabled) and then, reopening the same
  store, with the onnx MiniLM cross-encoder (``chain: [{provider: onnx}]``, default
  settings). The difference is the reranker's added cost.
* ``concurrent``: each rate, mode, arm and repetition seeds its own path-bound store.
  The command supervises one killable worker process for the run, stopping the entire
  run and marking its record incomplete if an engine operation survives cancellation.
  An open-loop writer offers 1, 10 or 50 msgs/s while ``--readers`` parallel closed-loop
  readers call ``Engine.read``. The default arm inherits the resolved base template,
  including the ONNX embedder and current MiniLM reranker. Optional ``off``/``on`` arms
  explicitly set the reranker. The lag sample runs from ack to facts ``ready``; failed,
  unavailable and tail-censored states are counted separately. Raw per-repetition results
  and between-repetition dispersion are retained. Budget candidates require every
  repetition to meet the offered rate with complete seed, warmups, reads and lag samples,
  no cap, cancellation or errors. A candidate also requires the current-default arm,
  at least three repetitions, a clean revision and ``--controlled-host`` operator
  attestation. The proposal is the largest run p95 with 30% headroom, subject to owner
  approval. No performance SLO is assumed by this eligibility rule.

    $env:PYTHONPATH='src'; $env:HF_HUB_OFFLINE='1'
    python scripts/benchmark_engine_read_ingest.py --part read --work-dir <scratch>
    python scripts/benchmark_engine_read_ingest.py --part concurrent --work-dir <scratch> --concurrent-arm current-default --concurrency-repetitions 3 --concurrent-read-warmups 3 --rates 1 10 50 --durations 60 30 10 --readers 2 --controlled-host

Run the timing command later on a quiet reference host and frozen revision. The
operator must verify those conditions before setting ``--controlled-host``; load and
power snapshots document observations, not proof of quietness. No model download is
performed by this harness. The active base ONNX weights must be present.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perf_protocol import (
    ROOT,
    SENDER,
    Recorder,
    corpus_messages,
    corpus_queries,
    host_snapshot,
    load_sample,
    percentile,
    summarize,
    write_record,
)

from memspine import Engine
from memspine.config.loader import load_config
from memspine.config.schema.root import freeze_effective_config
from memspine.observability.logging import configure_logging
from memspine.runtime.model_weights import onnx_requirements
from memspine.services.secrets.env import EnvSecrets

OUT_DIR = ROOT / "docs" / "provider_verification"
NS = "corpus"
SESSION_TURNS = 20
SEED_MARKER_VERSION = 2
NUMERIC_TOKEN_BUDGET_KEYS = frozenset(
    {
        "max_tokens",
        "token_budget",
        "window_max_tokens",
        "segment_grow_tokens",
        "batch_max_tokens",
        "index_view_max_extra_tokens",
        "max_tokens_per_attempt",
        "community_summary_budget_tokens",
        "budget_tokens_per_cycle",
        "reply_reserve_tokens",
        "replay_window_tokens_before",
        "replay_window_tokens_after",
        "lead_budget_tokens",
        "external_budget_tokens",
        "date_tokens",
    }
)
RERANK_ON = {"rerank": {"chain": [{"provider": "onnx", "settings": {}}]}}
RERANK_OFF: dict[str, Any] = {"rerank": {"chain": []}}
#: ``write.*`` overrides applied to every engine this run opens (``--window-idle-s``).
WRITE_OVERRIDES: dict[str, Any] = {}
#: Concurrent part: give each session's step messages one ``conversation_id``.
CONVERSATION_PER_SESSION = False


class UnresolvedEngineTask(RuntimeError):
    """A cancelled engine operation outlived its bounded cleanup window."""


def _config(data: Path, *, rerank: bool | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(data / "memspine.db"), "busy_timeout_s": 5.0},
            }
        },
        "deployment": {"data_dir": str(data)},
    }
    if rerank is not None:
        cfg["model"] = RERANK_ON if rerank else RERANK_OFF
    if WRITE_OVERRIDES:
        cfg["write"] = dict(WRITE_OVERRIDES)
    return cfg


def _resolved_seed_config_sha256(data: Path, rerank: bool | None) -> str:
    """Mirror the base Engine's local env config layering before reusing a store."""
    config = load_config(
        template="base",
        user_config=_config(data, rerank=rerank),
        env=os.environ,
        secret_resolver=EnvSecrets(dotenv_path=None).get,
    ).config
    effective = freeze_effective_config(config)
    return hashlib.sha256(
        json.dumps(effective.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def _engine_source_identity() -> dict[str, str]:
    """Commit plus actual source bytes, so dirty and untracked engine edits invalidate reuse."""
    commit = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in (ROOT / "src" / "memspine").rglob("*")
        if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".json", ".toml"}
    )
    files.append(ROOT / "pyproject.toml")
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return {"commit": commit, "content_sha256": digest.hexdigest()}


async def _open(data: Path, *, rerank: bool | None) -> tuple[Engine, float]:
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
    rerank: bool | None = False,
) -> dict[str, Any]:
    """Seed (or reuse) ``work/<name or seed-<size>>``; returns the seeding facts."""
    child = Path(name or f"seed-{size}")
    root = work.resolve()
    data = work / child
    resolved = data.resolve()
    if (
        child.is_absolute()
        or ".." in child.parts
        or resolved == root
        or root not in resolved.parents
        or data.is_symlink()
        or (hasattr(data, "is_junction") and data.is_junction())
    ):
        raise ValueError("seed path must be a non-linked child of the work directory")
    marker = data / "seed.json"
    seed_identity = {
        "version": SEED_MARKER_VERSION,
        "requested_size": size,
        "rerank_arm": "default" if rerank is None else "on" if rerank else "off",
        "resolved_config_sha256": _resolved_seed_config_sha256(data, rerank),
        "engine_source": _engine_source_identity(),
        "source_sha256": _source_hashes(),
    }
    if reuse and marker.exists():
        try:
            parsed = json.loads(marker.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
        info: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
        if (
            info.get("seed_identity") == seed_identity
            and info.get("size") == size
            and not info.get("seed_incomplete", True)
        ):
            info["reused"] = True
            return info
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    eng, start_ms = await _open(data, rerank=rerank)
    texts = corpus_messages(size, seed=size)
    t0 = time.perf_counter()
    per_call: list[float] = []
    seeded = 0
    submitted = 0
    sessions_with_retained = 0
    positions: list[tuple[int, int]] = []
    for i in range(0, size, SESSION_TURNS):
        if time.perf_counter() - t0 > max_seed_s:
            break  # bounded run: the store holds what was seeded so far
        batch = [
            {"role": "user", "sender": SENDER, "content": text}
            for text in texts[i : i + SESSION_TURNS]
        ]
        c0 = time.perf_counter()
        positions.append((submitted, seeded))
        records = await eng.ingest_messages(
            batch, namespace=NS, session_id=f"seed-{i // SESSION_TURNS}"
        )
        per_call.append((time.perf_counter() - c0) * 1000)
        submitted += len(batch)
        retained = min(
            len({str(getattr(record, "record_id", id(record))) for record in records or []}),
            len(batch),
        )
        seeded += retained
        sessions_with_retained += int(retained > 0)
    ingest_s = time.perf_counter() - t0
    # Ingest cost by corpus position: per-call time of each tenth of the seeding calls.
    tenth = max(1, len(per_call) // 10)
    progression = [
        {
            "submitted_messages_before": positions[k][0],
            "retained_messages_before": positions[k][1],
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
        "submitted_messages": submitted,
        "dropped_messages": submitted - seeded,
        "seed_incomplete": seeded < size,
        "time_cap_reached": submitted < size,
        "truncated_by_time_budget": submitted < size,
        "max_seed_s": max_seed_s,
        "sessions": sessions_with_retained,
        "sessions_attempted": len(per_call),
        "engine_start_ms": round(start_ms, 1),
        "ingest_wall_s": round(ingest_s, 2),
        "ingest_msgs_per_s": round(seeded / ingest_s, 1) if ingest_s else 0.0,
        "submitted_msgs_per_s": round(submitted / ingest_s, 1) if ingest_s else 0.0,
        "ingest_call_20_turns_ms": summarize(per_call),
        "ingest_call_progression": progression,
        "flush_windows_s": round(flush_s, 2),
        "flush_counters": {k: v for k, v in flushed.items() if isinstance(v, (int, float))},
        "reused": False,
        "seed_identity": seed_identity,
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
    original = rerank_module.gated_rerank  # type: ignore[attr-defined]

    async def timed_gated_rerank(reranker: Any, query: str, documents: Any) -> Any:
        t0 = time.perf_counter()
        out = await original(reranker, query, documents)
        stage_ms.append((time.perf_counter() - t0) * 1000)
        stage_docs.append(len(documents))
        return out

    rerank_module.gated_rerank = timed_gated_rerank  # type: ignore[attr-defined]
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
        rerank_module.gated_rerank = original  # type: ignore[attr-defined]
        await eng.stop()


async def part_read(args: argparse.Namespace) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    rec = Recorder(
        args.family or "rm26-read-latency-reranker",
        OUT_DIR
        / f"perf-{args.family or 'rm26-read-latency-reranker'}-{stamp}-{uuid.uuid4().hex[:8]}{args.suffix}.json",
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
        info = await seed(work, size, max_seed_s=args.max_seed_s, reuse=not args.fresh_seed)
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

#: record id -> (perf_counter at the window cut, cut reason, turns in the window)
CUTS: dict[str, tuple[float, str, int]] = {}
#: per-cut (reason, turns, tokens) of the current step
STEP_CUTS: list[tuple[str, int, int]] = []


def install_cut_probe(eng: Engine) -> bool:
    """Record when each record's window is cut, so derivation lag splits into the wait for
    the cut (idle window, turn cap, session end) and the processing after it."""
    windows = getattr(eng, "_windows", None)
    if windows is None or not hasattr(windows, "_enqueue"):
        return False
    original = windows._enqueue

    def enqueue(ns: str, store: Any, window: Any, win: Any) -> Any:
        now = time.perf_counter()
        turns = len(window.record_ids)
        for record_id in window.record_ids:
            CUTS[record_id] = (now, str(window.reason), turns)
        STEP_CUTS.append((str(window.reason), turns, int(getattr(window, "tokens", 0) or 0)))
        return original(ns, store, window, win)

    windows._enqueue = enqueue
    return True


def _window_stats(eng: Engine, *, reset: bool = False) -> dict[str, Any] | None:
    windows = getattr(eng, "_windows", None)
    if windows is None:
        return None
    stats = windows.stats(reset=reset)
    keep = (
        "turns",
        "windows",
        "windows_by_reason",
        "windows_processed",
        "turns_per_window",
        "calls",
        "rule_facts_returned",
        "rule_facts_unresolved",
        "facts_returned",
        "facts_deposited",
        "windows_empty",
    )
    return {k: stats.get(k) for k in keep}


def _record_message_ids(records: Any, ids: list[str]) -> dict[str, str]:
    """record id -> message id for one ``ingest_messages`` call."""
    out: dict[str, str] = {}
    for record in records or []:
        source = getattr(record, "source", None)
        message = getattr(source, "message", None)
        mid = getattr(message, "message_id", None)
        if mid is None and isinstance(message, dict):
            mid = message.get("message_id")
        if mid is not None and str(mid) in ids:
            out[str(record.record_id)] = str(mid)
    return out


async def _bounded(coro: Any, timeout_s: float) -> tuple[Any, bool, bool]:
    """Bound one engine await; report timeout and an uncooperative cancelled task."""
    task = asyncio.create_task(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError:
        task.cancel()
        settled, _ = await asyncio.wait({task}, timeout=0.25)
        if not settled:
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            raise UnresolvedEngineTask("engine operation ignored outer cancellation") from None
        raise
    if done:
        return task.result(), False, False
    task.cancel()
    settled, _ = await asyncio.wait({task}, timeout=0.25)
    if not settled:
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        raise UnresolvedEngineTask("engine operation ignored cancellation; abort isolated worker")
    return None, True, False


async def run_rate(
    eng: Engine,
    label: str,
    rate: float,
    duration: float,
    batch: int,
    readers: int,
    poll_ms: int,
    tail_s: float,
    step_max_s: float = float("inf"),
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
    record_of: dict[str, str] = {}  # message id -> record id
    STEP_CUTS.clear()
    _window_stats(eng, reset=True)
    writing = True
    calls = 0
    acked_msgs = 0
    saturated: list[int] = []
    unscheduled = 0
    write_tasks_unresolved = 0
    excluded_messages = 0
    read_timeouts = 0
    poll_timeouts = 0
    unresolved_awaits = 0

    async def write_call(first: int, scheduled: float) -> None:
        nonlocal acked_msgs, excluded_messages
        ids = [f"{label}-{i}-{uuid.uuid4().hex[:8]}" for i in range(first, min(first + batch, n))]
        msgs = [
            {"message_id": mid, "role": "user", "sender": SENDER, "content": texts[first + k]}
            for k, mid in enumerate(ids)
        ]
        try:
            session = f"{label}-{first // SESSION_TURNS}"
            records = await eng.ingest_messages(
                msgs,
                namespace=NS,
                session_id=session,
                # --conversation-per-session: the session's turns share one conversation, so
                # a window can hold several turns; otherwise every root message starts its
                # own conversation (and its own window).
                conversation_id=session if CONVERSATION_PER_SESSION else None,
            )
        except Exception as exc:  # recorded, not fatal
            errors.append(f"write: {type(exc).__name__}: {exc}"[:300])
            return
        now = time.perf_counter()
        acks.append((now - scheduled) * 1000)
        accepted = _record_message_ids(records, ids)
        accepted = {rid: mid for rid, mid in accepted.items() if mid in ids}
        accepted_ids = set(accepted.values())
        acked_msgs += len(accepted_ids)
        excluded_messages += len(ids) - len(accepted_ids)
        for rid, mid in accepted.items():
            record_of[mid] = rid
        for k, mid in enumerate(ids):
            if mid in accepted_ids and (first + k) % sample == 0:
                acked_at[mid] = now

    async def writer() -> None:
        nonlocal calls, unscheduled, write_tasks_unresolved
        tasks = []
        t0 = time.perf_counter()
        for first in range(0, n, batch):
            if time.perf_counter() - t0 >= step_max_s:
                unscheduled = n - first
                break
            due = t0 + first / rate
            delay = min(due - time.perf_counter(), step_max_s - (time.perf_counter() - t0))
            if delay > 0:
                await asyncio.sleep(delay)
            if time.perf_counter() - t0 >= step_max_s:
                unscheduled = n - first
                break
            tasks.append(asyncio.create_task(write_call(first, due)))
            calls += 1
        remaining = step_max_s - (time.perf_counter() - t0)
        _, late = await asyncio.wait(tasks, timeout=max(remaining, 0.0))
        if late:  # saturated: the backlog would only measure queueing; stop it
            saturated.append(len(late))
            for task in late:
                task.cancel()
            _, unsettled = await asyncio.wait(late, timeout=0.25)
            write_tasks_unresolved = len(unsettled)
            if unsettled:
                raise UnresolvedEngineTask("cancelled write call remains live")

    async def reader(k: int) -> None:
        nonlocal read_timeouts, unresolved_awaits
        i = k
        while writing:
            t0 = time.perf_counter()
            try:
                _, timed_out, unresolved = await _bounded(
                    eng.read(queries[i % len(queries)], namespace=NS), min(5.0, step_max_s)
                )
                if timed_out:
                    read_timeouts += 1
                    unresolved_awaits += int(unresolved)
                    continue
            except UnresolvedEngineTask:
                raise
            except Exception as exc:
                errors.append(f"read: {type(exc).__name__}: {exc}"[:300])
            else:
                reads.append((time.perf_counter() - t0) * 1000)
            i += readers
            await asyncio.sleep(0.05)

    async def lag_poll() -> None:
        nonlocal poll_timeouts, unresolved_awaits
        while True:
            pending = [
                m
                for m in acked_at
                if m not in lags and final_state.get(m) not in ("failed", "unavailable")
            ]
            if pending:
                try:
                    waited, timed_out, unresolved = await _bounded(
                        eng.wait_for_derivation(
                            pending, namespace=NS, capabilities=("facts",), timeout_s=0.0
                        ),
                        min(5.0, tail_s),
                    )
                    if timed_out:
                        poll_timeouts += 1
                        unresolved_awaits += int(unresolved)
                        waited = {}
                except UnresolvedEngineTask:
                    raise
                except Exception as exc:
                    errors.append(f"poll: {type(exc).__name__}: {exc}"[:300])
                    waited = {}
                for mid, res in waited.items():
                    state = str(res.readiness.facts)
                    final_state[mid] = state
                    if state == "ready" and not res.timed_out:
                        lags[mid] = (time.perf_counter() - acked_at[mid]) * 1000
            if not writing and len(lags) + sum(
                final_state.get(m) in ("failed", "unavailable") for m in acked_at
            ) == len(acked_at):
                return
            await asyncio.sleep(poll_ms / 1000)

    poll_task = asyncio.create_task(lag_poll()) if poll_ms > 0 else None
    reader_tasks = [asyncio.create_task(reader(k)) for k in range(readers)]
    w0 = time.perf_counter()
    await writer()
    write_wall = time.perf_counter() - w0
    writing = False
    readers_done, readers_late = (
        await asyncio.wait(reader_tasks, timeout=min(5.0, step_max_s))
        if reader_tasks
        else (set(), set())
    )
    for task in readers_late:
        task.cancel()
    if readers_late:
        _, unsettled_readers = await asyncio.wait(readers_late, timeout=0.25)
        unresolved_awaits += len(unsettled_readers)
        if unsettled_readers:
            if poll_task is not None:
                poll_task.cancel()
            raise UnresolvedEngineTask("cancelled reader remains live")
    read_tasks_cancelled = len(readers_late)
    for task in readers_done:
        if not task.cancelled():
            reader_error = task.exception()
            if reader_error is not None:
                if poll_task is not None:
                    poll_task.cancel()
                raise reader_error
    tail_complete = False
    if poll_task is not None and (saturated or unscheduled):
        poll_task.cancel()
        _, unsettled_poll = await asyncio.wait({poll_task}, timeout=0.25)
        unresolved_awaits += len(unsettled_poll)
        if unsettled_poll:
            raise UnresolvedEngineTask("cancelled poll remains live")
    elif poll_task is not None:
        try:
            finished, _ = await asyncio.wait({poll_task}, timeout=tail_s)
            tail_complete = bool(finished)
            if not finished:
                raise TimeoutError
        except TimeoutError:
            poll_task.cancel()
            _, unsettled_poll = await asyncio.wait({poll_task}, timeout=0.25)
            unresolved_awaits += len(unsettled_poll)
            if unsettled_poll:
                raise UnresolvedEngineTask("cancelled poll remains live") from None
    if poll_task is not None and poll_task.done() and not poll_task.cancelled():
        poll_error = poll_task.exception()
        if poll_error:
            raise poll_error
    lag_values = list(lags.values())
    # Split each derived lag at its window cut: the wait for the cut (the configured idle
    # window, a turn cap or a session end) and the processing after the cut.
    cut_wait: list[float] = []
    processing: list[float] = []
    lag_by_reason: dict[str, list[float]] = {}
    unmatched = 0
    for mid, lag in lags.items():
        cut = CUTS.get(record_of.get(mid, ""))
        if cut is None or mid not in acked_at:
            unmatched += 1
            continue
        wait = max(0.0, (cut[0] - acked_at[mid]) * 1000)
        cut_wait.append(wait)
        processing.append(max(0.0, lag - wait))
        lag_by_reason.setdefault(cut[1], []).append(lag)
    cut_reasons: dict[str, int] = {}
    for reason, _, _ in STEP_CUTS:
        cut_reasons[reason] = cut_reasons.get(reason, 0) + 1
    states: dict[str, int] = {}
    for mid in acked_at:
        state = final_state.get(mid, "unpolled")
        states[state] = states.get(state, 0) + 1
    return {
        "target_msgs_per_s": rate,
        "duration_s": duration,
        "messages_per_call": batch,
        "saturated": bool(saturated or unscheduled),
        "calls_cancelled_at_step_cap": saturated[0] if saturated else 0,
        "write_tasks_unresolved": write_tasks_unresolved,
        "messages_unscheduled_at_step_cap": unscheduled,
        "step_max_s": step_max_s,
        "calls": calls,
        "messages": n,
        "acked_messages": acked_msgs,
        "excluded_messages": excluded_messages,
        "achieved_msgs_per_s": round(acked_msgs / write_wall, 2),
        "achieved_msgs_per_s_exact": acked_msgs / write_wall if write_wall else 0.0,
        "write_wall_s": round(write_wall, 2),
        "write_ack_ms": summarize(acks),
        "write_ack_note": "per call, from its scheduled send time to its return (open loop)",
        "read_warmups": 0,
        "warmup_failures": 0,
        "read_timeouts": read_timeouts,
        "poll_timeouts": poll_timeouts,
        "read_tasks_cancelled": read_tasks_cancelled,
        "unresolved_awaits": unresolved_awaits,
        "read_ms": summarize(reads),
        "readers": readers,
        "derivation_lag_ms": summarize(lag_values),
        "derivation_lag_note": "ack to observed facts ready, timestamped after poll returns",
        "lag_wait_for_window_cut_ms": summarize(cut_wait),
        "lag_processing_after_cut_ms": summarize(processing),
        "lag_by_cut_reason_ms": {r: summarize(v) for r, v in lag_by_reason.items()},
        "lag_split_unmatched": unmatched,
        "window_cuts": {
            "count": len(STEP_CUTS),
            "by_reason": cut_reasons,
            "turns_per_window": summarize([float(t) for _, t, _ in STEP_CUTS]),
            "tokens_per_window": summarize([float(t) for _, _, t in STEP_CUTS]),
        },
        "window_stats": _window_stats(eng),
        "lag_sample_every": sample,
        "lag_tracked": len(acked_at),
        "derived": len(lag_values),
        "not_derived_within_tail": len(acked_at) - len(lag_values),
        "failed_readiness": states.get("failed", 0),
        "unavailable_readiness": states.get("unavailable", 0),
        "lag_censored": len(acked_at) - len(lag_values),
        "tail_complete": tail_complete,
        "final_facts_states": states,
        "errors": errors[:20],
        "error_count": len(errors),
    }


def _source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ("benchmark_engine_read_ingest.py", "perf_protocol.py")
    }


def _redact_config(value: Any, key: str = "") -> Any:
    """Keep resolved values while excluding credentials and credential-bearing URLs."""
    if (
        key in NUMERIC_TOKEN_BUDGET_KEYS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return value  # numeric token budgets are workload settings, not credentials
    if re.search(r"password|passwd|secret|token|api.?key|credential|auth", key, re.I):
        return "<redacted>"
    if re.search(r"(?:^dsn$|connection.?string|(?:^|_)uri$|(?:^|_)url$)", key, re.I):
        return "<redacted-connection>"
    if isinstance(value, dict):
        return {k: _redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    if isinstance(value, str) and "://" in value:
        return "<redacted-url>"
    return value


def _effective_config(eng: Engine) -> dict[str, Any]:
    """Record resolved config with secrets redacted, plus pinned model identities."""
    cfg = eng.config
    resolved = _redact_config(cfg.model_dump(mode="json"))
    return {
        "template": "base",
        "resolved": resolved,
        "resolved_redacted_sha256": hashlib.sha256(
            json.dumps(resolved, sort_keys=True).encode()
        ).hexdigest(),
        "embedding": cfg.model.embedding.model_dump(mode="json", exclude={"settings"}),
        "onnx_models": [
            {"slot": req.slot, "model": req.model, "revision": req.spec.revision}
            for req in onnx_requirements(cfg)
        ],
        "rerank": cfg.model.rerank.model_dump(
            mode="json", exclude={"chain": {"__all__": {"settings"}}}
        ),
        "rerank_settings_sha256": [
            hashlib.sha256(
                json.dumps(stage.settings, sort_keys=True, default=str).encode()
            ).hexdigest()
            for stage in cfg.model.rerank.chain
        ],
        "read": resolved["read"],
        "write": {
            "window_idle_s": cfg.write.window_idle_s,
            "window_async": cfg.write.window_async,
        },
        "storage_providers": {
            name: getattr(cfg.storage, name).provider if getattr(cfg.storage, name) else None
            for name in ("records", "vector", "lexical", "graph", "cache")
        },
    }


def _between_repetition(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Dispersion of independent per-run summaries; no pooled query percentiles."""
    out: dict[str, Any] = {"independent_repetitions": len(rows)}
    for name, path in (
        ("achieved_msgs_per_s", ("achieved_msgs_per_s",)),
        ("write_ack_p95_ms", ("write_ack_ms", "p95_ms")),
        ("read_p95_ms", ("read_ms", "p95_ms")),
        ("derivation_lag_p95_ms", ("derivation_lag_ms", "p95_ms")),
    ):
        values = [
            float(v)
            for row in rows
            if (v := row[path[0]] if len(path) == 1 else row[path[0]].get(path[1])) is not None
        ]
        out[name] = {
            "n": len(values),
            "mean": round(statistics.fmean(values), 6) if values else None,
            "stdev": round(statistics.stdev(values), 6) if len(values) > 1 else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    return out


def _complete_at_target(row: dict[str, Any]) -> bool:
    """Strict evidence gate for a budget candidate, not a performance SLO."""
    return (
        not row["saturated"]
        and row["messages_unscheduled_at_step_cap"] == 0
        and row["calls_cancelled_at_step_cap"] == 0
        and row["write_tasks_unresolved"] == 0
        and row["excluded_messages"] == 0
        and row["acked_messages"] == row["messages"]
        and row["achieved_msgs_per_s_exact"] >= row["target_msgs_per_s"]
        and row["tail_complete"]
        and row["lag_censored"] == 0
        and row["failed_readiness"] == 0
        and row["unavailable_readiness"] == 0
        and row["error_count"] == 0
        and row["read_timeouts"] == 0
        and row["poll_timeouts"] == 0
        and row["read_tasks_cancelled"] == 0
        and row["unresolved_awaits"] == 0
        and row["warmup_failures"] == 0
        and all(
            row[k].get("p95_ms") is not None
            for k in ("read_ms", "write_ack_ms", "derivation_lag_ms")
        )
    )


def _eligible_repetition(entry: dict[str, Any]) -> bool:
    seed_info = entry["seed"]
    return (
        seed_info["size"] == seed_info["requested_size"]
        and not seed_info["truncated_by_time_budget"]
        and _complete_at_target(entry["metrics"])
    )


def _proposal_eligible(
    *,
    complete_at_target: bool,
    arm: str,
    repetitions: int,
    controlled_host: bool,
    diagnostic_only: bool,
) -> bool:
    return (
        complete_at_target
        and arm == "current-default"
        and repetitions >= 3
        and controlled_host
        and not diagnostic_only
    )


async def part_concurrent(args: argparse.Namespace) -> None:
    steps = [
        (f"{mode}_{rate:g}", rate, duration, 1 if mode == "single" else max(1, round(rate / 2)))
        for mode in args.modes
        for rate, duration in zip(args.rates, args.durations, strict=True)
        if not (mode == "batched" and rate <= 2)  # batched is single at <= 2 msgs/s
    ]
    if args.steps:
        steps = [step for step in steps if step[0] in set(args.steps)]
    arm = args.concurrent_arm
    rerank = {"current-default": None, "off": False, "on": True}[arm]
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    run_id = getattr(args, "run_id", None) or uuid.uuid4().hex[:12]
    family = args.family or "pa053-concurrent-ingest-search"
    out = (
        Path(args.worker_record_path)
        if getattr(args, "worker_record_path", None)
        else OUT_DIR / f"perf-{family}-{arm}-{stamp}-{run_id}{args.suffix}.json"
    )
    rec = Recorder(
        family,
        out,
        allow_dirty=args.allow_dirty,
        ignore=[OUT_DIR],
        settings={
            "template": "base (rules-only, onnx bge-small, outbox on, window_async on)",
            "seed_corpus_requested": args.seed_size,
            "concurrent_arm": arm,
            "repetitions": args.concurrency_repetitions,
            "read_warmups_requested_per_repetition": args.concurrent_read_warmups,
            "controlled_host_operator_attested": args.controlled_host,
            "run_id": run_id,
            "execution_at_utc": datetime.now(UTC).isoformat(),
            "source_sha256": _source_hashes(),
            "steps": [
                {"label": s[0], "rate": s[1], "duration_s": s[2], "messages_per_call": s[3]}
                for s in steps
            ],
            "readers": args.readers,
            "step_max_s": args.step_max_s,
            "reader_think_ms": 50,
            "poll_ms": args.poll_ms,
            "tail_s": args.tail_s,
            "write_call": (
                "single: ingest_messages([one message]) per send; batched: rate/2 messages per "
                "call, two calls per second; 20 messages per session"
            ),
            "store": "fresh path-bound seed for each arm, step and independent repetition; no step growth",
            "lag_definition": "ack -> wait_for_derivation(facts) ready; failed/unavailable/censored separately",
            "budget_eligibility": "current-default arm, clean revision, operator-attested quiet fixed-power host, at least 3 independent repetitions, complete seed and warmups, fully accepted at or above offered rate, no cap/cancel/error/censored lag, and nonempty read/write/lag p95; owner approval still required",
            "lag_split": (
                "per tracked message: wait = window cut time - ack (WindowPipeline._enqueue "
                "probe); processing = lag - wait"
            ),
            "write_overrides": dict(WRITE_OVERRIDES),
            "conversation_per_session": CONVERSATION_PER_SESSION,
            "logging": "WARNING",
        },
    )
    write_record(rec.out, {**rec.record, "status": "running"})
    work = Path(args.work_dir)
    for label, rate, duration, batch in steps:
        repetitions: list[dict[str, Any]] = []
        for rep in range(1, args.concurrency_repetitions + 1):
            data = work / f"{run_id}-{arm}-{label}-rep{rep:02d}"
            info = await seed(
                work,
                args.seed_size,
                name=data.name,
                reuse=False,
                max_seed_s=args.max_seed_s,
                rerank=rerank,
            )
            eng, start_ms = await _open(data, rerank=rerank)
            abort = False
            try:
                effective = _effective_config(eng)
                CUTS.clear()
                probe = install_cut_probe(eng)
                warmup_success = 0
                warmup_failures = 0
                for query in corpus_queries(args.concurrent_read_warmups, seed=101):
                    try:
                        _, timed_out, unresolved = await _bounded(
                            eng.read(query, namespace=NS), min(5.0, args.step_max_s)
                        )
                        if timed_out or unresolved:
                            warmup_failures += 1
                        else:
                            warmup_success += 1
                    except UnresolvedEngineTask:
                        raise
                    except Exception:
                        warmup_failures += 1
                before = {"host": host_snapshot(), "load": load_sample()}
                row = await run_rate(
                    eng,
                    label,
                    rate,
                    duration,
                    batch,
                    args.readers,
                    args.poll_ms,
                    args.tail_s,
                    args.step_max_s,
                )
                row["read_warmups"] = warmup_success
                row["warmup_failures"] = warmup_failures
            except UnresolvedEngineTask as exc:
                abort = True
                rec.record["results"][label] = {
                    "repetitions": repetitions,
                    "incomplete_repetition": rep,
                    "reason": str(exc),
                }
                write_record(rec.out, {**rec.record, "status": "aborted_incomplete"})
                raise
            finally:
                if not abort:
                    await eng.stop()
            repetitions.append(
                {
                    "repetition": rep,
                    "store_path": str(data),
                    "seed": info,
                    "namespace_messages_before": info["size"],
                    "engine_start_ms": round(start_ms, 1),
                    "cut_probe_installed": probe,
                    "effective_config": effective,
                    "load_before": before,
                    "load_after": {"host": host_snapshot(), "load": load_sample()},
                    "metrics": row,
                }
            )
            rec.record["results"][label] = {"repetitions": repetitions}
            write_record(rec.out, {**rec.record, "status": "running"})
            print(label, rep, row["achieved_msgs_per_s"], row["lag_censored"], flush=True)
        rows = [entry["metrics"] for entry in repetitions]
        result = rec.record["results"][label]
        result["between_repetitions"] = _between_repetition(rows)
        result["complete_at_target"] = all(_eligible_repetition(entry) for entry in repetitions)
        result["proposal_eligible"] = _proposal_eligible(
            complete_at_target=result["complete_at_target"],
            arm=arm,
            repetitions=args.concurrency_repetitions,
            controlled_host=args.controlled_host,
            diagnostic_only=rec.record.get("diagnostic_only", True),
        )
        if result["proposal_eligible"]:
            result["proposed_budgets"] = {
                "status": "proposal only; owner approves (ADR-096)",
                "rule": "largest independent repetition p95 x 1.3, rounded up",
                **{
                    name: _budget(max(row[key]["p95_ms"] for row in rows))
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


def _run_isolated_command(cmd: list[str], out: Path, timeout: float) -> None:
    """Kill only the owned process after a deadline and retain an incomplete record."""
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd)
    reason: str | None = None
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if out.exists():
            try:
                partial = json.loads(out.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                partial = {}
            if partial.get("status") == "aborted_incomplete":
                reason = "isolated worker reported unresolved engine task"
                break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = f"isolated worker exceeded {timeout:g}s hard deadline"
            break
        try:
            proc.wait(timeout=min(0.5, remaining))
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
    if reason is None and proc.returncode != 0:
        reason = f"isolated worker exited {proc.returncode}"
    if reason is None:
        try:
            completed = json.loads(out.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            completed = {}
        if completed.get("status") != "completed":
            reason = "isolated worker exited without a completed record"
    if reason is not None:
        try:
            parsed = json.loads(out.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
        record: dict[str, Any] = parsed if isinstance(parsed, dict) else {"results": {}}
        if parsed is None or not isinstance(parsed, dict):
            record["partial_record_unreadable"] = True
        results = record.get("results")
        if not isinstance(results, dict):
            results = {}
            record["results"] = results
        for result in results.values():
            if isinstance(result, dict):
                result.pop("proposed_budgets", None)
                result["proposal_eligible"] = False
        record.update(
            {
                "status": "aborted_incomplete",
                "aborted_at_utc": datetime.now(UTC).isoformat(),
                "abort_reason": reason,
                "worker_exit_code": proc.returncode,
            }
        )
        write_record(out, record)
        raise RuntimeError(f"concurrent benchmark aborted; incomplete record: {out}")
    print(f"completed record: {out}", flush=True)


def _supervise_concurrent(args: argparse.Namespace) -> None:
    """Own one killable worker for the whole run; never advance after unresolved work."""
    run_id = uuid.uuid4().hex[:12]
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    family = args.family or "pa053-concurrent-ingest-search"
    out = OUT_DIR / f"perf-{family}-{args.concurrent_arm}-{stamp}-{run_id}{args.suffix}.json"
    step_count = sum(mode != "batched" or rate > 2 for mode in args.modes for rate in args.rates)
    if args.steps:
        step_count = len(args.steps)
    timeout = args.max_run_s or (
        60.0
        + step_count
        * args.concurrency_repetitions
        * (args.max_seed_s + args.step_max_s + args.tail_s + 5 * args.concurrent_read_warmups + 60)
    )
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        *sys.argv[1:],
        "--worker-record-path",
        str(out),
        "--run-id",
        run_id,
    ]
    _run_isolated_command(cmd, out, timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=("read", "concurrent"), required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--reps", type=int, default=100)
    parser.add_argument("--concurrency-repetitions", type=int, default=3)
    parser.add_argument("--concurrent-read-warmups", type=int, default=3)
    parser.add_argument(
        "--max-run-s",
        type=float,
        default=None,
        help="hard deadline for the isolated concurrent worker",
    )
    parser.add_argument("--worker-record-path", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    parser.add_argument(
        "--controlled-host",
        action="store_true",
        help="operator attests quiet host, fixed power plan and frozen clean revision for budget candidacy",
    )
    parser.add_argument(
        "--concurrent-arm", choices=("current-default", "off", "on"), default="current-default"
    )
    parser.add_argument("--warmups", type=int, default=10)
    parser.add_argument("--seed-size", type=int, default=1000)
    parser.add_argument("--max-seed-s", type=float, default=600.0)
    parser.add_argument("--rates", type=float, nargs="+", default=[1.0, 10.0, 50.0])
    parser.add_argument("--durations", type=float, nargs="+", default=[60.0, 30.0, 10.0])
    parser.add_argument(
        "--modes", nargs="+", choices=("single", "batched"), default=["single", "batched"]
    )
    parser.add_argument("--readers", type=int, default=2)
    parser.add_argument("--steps", nargs="+", help="run only these step labels")
    parser.add_argument("--step-max-s", type=float, default=300.0)
    parser.add_argument("--poll-ms", type=int, default=500)
    parser.add_argument("--tail-s", type=float, default=150.0)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--suffix", default="", help="record file suffix, e.g. -after")
    parser.add_argument("--family", default=None, help="record family (and file) name")
    parser.add_argument(
        "--window-idle-s", type=float, default=None, help="override write.window_idle_s"
    )
    parser.add_argument(
        "--fresh-seed", action="store_true", help="read part: reseed even if a seed exists"
    )
    parser.add_argument(
        "--conversation-per-session",
        action="store_true",
        help="concurrent part: one conversation_id per session of step messages",
    )
    args = parser.parse_args()
    if (
        args.concurrency_repetitions < 1
        or args.reps < 1
        or args.warmups < 0
        or args.concurrent_read_warmups < 0
    ):
        parser.error("repetitions/reps must be positive and warmups nonnegative")
    if (
        args.seed_size < 1
        or args.max_seed_s <= 0
        or args.readers < 1
        or args.poll_ms < 1
        or args.tail_s <= 0
        or args.step_max_s <= 0
    ):
        parser.error("seed size, seed/step/tail caps, readers, and poll interval must be positive")
    if (
        len(args.rates) != len(args.durations)
        or not args.rates
        or any(not math.isfinite(v) or v <= 0 for v in [*args.rates, *args.durations])
    ):
        parser.error(
            "rates and durations must have equal nonzero lengths and positive finite values"
        )
    if any(
        int(rate * duration) < 1 for rate, duration in zip(args.rates, args.durations, strict=True)
    ):
        parser.error("each rate x duration must schedule at least one message")
    if args.steps and any(
        step not in {f"{mode}_{rate:g}" for mode in args.modes for rate in args.rates}
        for step in args.steps
    ):
        parser.error("--steps contains an unknown mode/rate label")
    if args.window_idle_s is not None and (
        not math.isfinite(args.window_idle_s) or args.window_idle_s <= 0
    ):
        parser.error("--window-idle-s must be positive and finite")
    if args.max_run_s is not None and (not math.isfinite(args.max_run_s) or args.max_run_s <= 0):
        parser.error("--max-run-s must be positive and finite")
    if args.conversation_per_session:
        global CONVERSATION_PER_SESSION
        CONVERSATION_PER_SESSION = True
    if args.window_idle_s is not None:
        WRITE_OVERRIDES["window_idle_s"] = args.window_idle_s
    configure_logging("WARNING")
    Path(args.work_dir).mkdir(parents=True, exist_ok=True)
    if args.part == "read":
        asyncio.run(part_read(args))
    elif args.worker_record_path:
        asyncio.run(part_concurrent(args))
    else:
        _supervise_concurrent(args)


if __name__ == "__main__":
    main()
