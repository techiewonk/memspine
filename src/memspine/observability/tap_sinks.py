"""Wiretap sinks (WT-01): jsonl, sqlite, memory, otel, log (``tap.register_sink``).

ADR-095 (logs and traces): the engine-owned sinks (memory, JSONL, SQLite) hold their traces in
process memory or in files under the configured tap directory, so each has an ``erase(ids)``
hook that :func:`memspine.observability.tap.erase_records` calls on a hard erase: every root
trace naming an erased record id is dropped (rewritten out of the JSONL files, deleted from the
SQLite tables and the database compacted). The ``log`` and ``otel`` sinks hand each trace to the
operator's logging pipeline or OpenTelemetry collector the moment it finishes; the engine keeps
no copy, so purging what those systems stored is the operator's duty (as for any log shipper).
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memspine.observability import tap
from memspine.observability.logging import get_logger

__all__ = ["JsonlSink", "LogSink", "MemorySink", "OtelSink", "SqliteSink", "register_builtin"]

_sqlite_opener: Any = None


def _names(value: Any, ids: frozenset[str]) -> bool:
    """True when the serialized ``value`` names one of ``ids`` (record ids are UUIDs)."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return any(i in text for i in ids)


def configure_sqlite_opener(opener: Any) -> None:
    """Install the client-owned opener at the composition boundary."""
    global _sqlite_opener
    _sqlite_opener = opener


class JsonlSink:
    """One JSON line per finished root request (children nested), one file per UTC day."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, day: str | None = None) -> Path:
        return self.dir / f"tap-{day or datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"

    def write(self, trace: dict[str, Any]) -> None:
        with self.path_for().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trace, separators=(",", ":"), default=str) + "\n")

    def erase(self, ids: frozenset[str]) -> int:
        """ADR-095: rewrite every day file without the lines (root traces) naming ``ids``; each
        file is replaced atomically, so no reader ever sees half a file."""
        dropped = 0
        for path in sorted(self.dir.glob("tap-*.jsonl")):
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            kept = [line for line in lines if not _names(line, ids)]
            if len(kept) == len(lines):
                continue
            dropped += len(lines) - len(kept)
            tmp = path.with_name(path.name + ".erase")
            tmp.write_text("".join(kept), encoding="utf-8", newline="")
            os.replace(tmp, path)
        return dropped

    def close(self) -> None:
        return None


class MemorySink:
    """Ring buffer of the last ``size`` root traces (tests, CLI, notebooks)."""

    def __init__(self, size: int = 1000) -> None:
        self._q: deque[dict[str, Any]] = deque(maxlen=size)

    def write(self, trace: dict[str, Any]) -> None:
        self._q.append(trace)

    def traces(self) -> list[dict[str, Any]]:
        return list(self._q)

    def erase(self, ids: frozenset[str]) -> int:
        """ADR-095: drop every held root trace naming ``ids``."""
        kept = [t for t in self._q if not _names(t, ids)]
        dropped = len(self._q) - len(kept)
        if dropped:
            self._q = deque(kept, maxlen=self._q.maxlen)
        return dropped

    def close(self) -> None:
        return None


class SqliteSink:
    """``requests`` and ``events`` tables in ``<dir>/tap.sqlite`` (children flattened)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS requests(
      id TEXT PRIMARY KEY, parent_id TEXT, kind TEXT, scope_hash TEXT, started TEXT,
      duration_ms REAL, fields TEXT, n_events INTEGER);
    CREATE TABLE IF NOT EXISTS events(
      request_id TEXT, seq INTEGER, t_ms REAL, stage TEXT, fields TEXT,
      PRIMARY KEY(request_id, seq));
    CREATE INDEX IF NOT EXISTS events_stage ON events(stage);
    """

    def __init__(self, directory: str | Path) -> None:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / "tap.sqlite"
        self._lock = threading.Lock()
        if _sqlite_opener is None:
            raise tap.TapConfigError("SQLite tap sink was not configured by the composition root")
        self._db = _sqlite_opener(self.path, check_same_thread=False)
        self._db.executescript(self.SCHEMA)

    def write(self, trace: dict[str, Any]) -> None:
        with self._lock:
            self._insert(trace)
            self._db.commit()

    def _insert(self, t: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?,?,?,?)",
            (t["id"], t["parent_id"], t["kind"], t["scope_hash"], t["started"],
             t["duration_ms"], json.dumps(t["fields"], default=str), len(t["events"])),
        )  # fmt: skip
        self._db.executemany(
            "INSERT OR REPLACE INTO events VALUES(?,?,?,?,?)",
            [(t["id"], e["seq"], e["t_ms"], e["stage"], json.dumps(e["fields"], default=str))
             for e in t["events"]],
        )  # fmt: skip
        for child in t["children"]:
            self._insert(child)

    def erase(self, ids: frozenset[str]) -> int:
        """ADR-095: delete every request tree (root and children) whose request or event fields
        name ``ids``, then compact the database so no free page keeps their text."""
        with self._lock:
            hit: set[str] = set()
            for rid in ids:
                for (req,) in self._db.execute(
                    "SELECT id FROM requests WHERE id = ? OR instr(fields, ?) > 0", (rid, rid)
                ).fetchall():
                    hit.add(str(req))
                for (req,) in self._db.execute(
                    "SELECT DISTINCT request_id FROM events WHERE instr(fields, ?) > 0", (rid,)
                ).fetchall():
                    hit.add(str(req))
            if not hit:
                return 0
            parent = {
                str(i): (str(p) if p is not None else None)
                for i, p in self._db.execute("SELECT id, parent_id FROM requests").fetchall()
            }
            roots: set[str] = set()
            for req in hit:  # a child's text belongs to its whole request: climb to the root
                seen: set[str] = set()
                cur = req
                while parent.get(cur) is not None and cur not in seen:
                    seen.add(cur)
                    cur = str(parent[cur])
                roots.add(cur)
            children: dict[str, list[str]] = {}
            for i, p in parent.items():
                if p is not None:
                    children.setdefault(p, []).append(i)
            doomed: set[str] = set()
            stack = list(roots)
            while stack:
                cur = stack.pop()
                if cur not in doomed:
                    doomed.add(cur)
                    stack.extend(children.get(cur, ()))
            rows = [(req,) for req in sorted(doomed)]
            self._db.executemany("DELETE FROM events WHERE request_id = ?", rows)
            self._db.executemany("DELETE FROM requests WHERE id = ?", rows)
            self._db.commit()
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # a no-op unless in WAL mode
            return len(roots)

    def close(self) -> None:
        with self._lock:
            self._db.close()


class LogSink:
    """One structured ``tap.request`` log line per request on the ``memspine.tap`` logger."""

    def __init__(self) -> None:
        self.logger = get_logger("memspine.tap")

    def write(self, trace: dict[str, Any]) -> None:
        self.logger.info("tap.request", trace=trace)

    def close(self) -> None:
        return None


class OtelSink:
    """Exports each request as an OpenTelemetry span with one span event per tap event."""

    def __init__(self) -> None:
        try:
            from opentelemetry import trace as otel_trace
        except ImportError as exc:
            raise tap.TapConfigError(
                "observability.tap.sink='otel' needs the opentelemetry-api package"
            ) from exc
        self._tracer = otel_trace.get_tracer("memspine.tap")

    def write(self, trace: dict[str, Any]) -> None:
        with self._tracer.start_as_current_span(f"memspine.{trace['kind']}") as span:
            span.set_attribute("memspine.request_id", trace["id"])
            span.set_attribute("memspine.scope_hash", trace["scope_hash"])
            span.set_attribute("memspine.duration_ms", trace["duration_ms"])
            for ev in trace["events"]:
                span.add_event(
                    ev["stage"], {k: json.dumps(v, default=str) for k, v in ev["fields"].items()}
                )
            for child in trace["children"]:
                self.write(child)

    def close(self) -> None:
        return None


def register_builtin() -> None:
    tap.register_sink("jsonl", lambda cfg: JsonlSink(cfg.dir))
    tap.register_sink("sqlite", lambda cfg: SqliteSink(cfg.dir))
    tap.register_sink("memory", lambda cfg: MemorySink())
    tap.register_sink("log", lambda cfg: LogSink())
    tap.register_sink("otel", lambda cfg: OtelSink())
