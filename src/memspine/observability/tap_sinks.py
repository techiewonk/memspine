"""Wiretap sinks (WT-01): jsonl, sqlite, memory, otel, log (``tap.register_sink``)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memspine.observability import tap

__all__ = ["JsonlSink", "LogSink", "MemorySink", "OtelSink", "SqliteSink", "register_builtin"]


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
        self._db = sqlite3.connect(self.path, check_same_thread=False)
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

    def close(self) -> None:
        with self._lock:
            self._db.close()


class LogSink:
    """One structured log line per request on the ``memspine.tap`` logger."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("memspine.tap")

    def write(self, trace: dict[str, Any]) -> None:
        self.logger.info("tap.request %s", json.dumps(trace, separators=(",", ":"), default=str))

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
