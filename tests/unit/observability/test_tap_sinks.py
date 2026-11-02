"""``memspine.observability.tap_sinks``: the built-in wiretap sinks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from structlog.testing import capture_logs

from memspine.observability.tap_sinks import JsonlSink, LogSink, MemorySink


def _trace(rid: str = "r1") -> dict[str, Any]:
    return {
        "id": rid,
        "parent_id": None,
        "kind": "read",
        "scope_hash": "h",
        "started": "2026-01-01T00:00:00+00:00",
        "duration_ms": 1.5,
        "fields": {},
        "events": [{"seq": 1, "t_ms": 0.1, "stage": "read.a.b", "fields": {"n": 1}}],
        "children": [],
    }


def test_log_sink_emits_one_structlog_line_per_request() -> None:
    sink = LogSink()
    trace = _trace()
    with capture_logs() as logs:
        sink.write(trace)
    assert logs == [{"event": "tap.request", "log_level": "info", "trace": trace}]
    sink.close()


def test_memory_sink_keeps_the_last_traces() -> None:
    sink = MemorySink(size=2)
    for rid in ("a", "b", "c"):
        sink.write(_trace(rid))
    assert [t["id"] for t in sink.traces()] == ["b", "c"]


def test_jsonl_sink_appends_one_line_per_trace(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "tap")
    sink.write(_trace("a"))
    sink.write(_trace("b"))
    lines = sink.path_for().read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["a", "b"]
    assert sink.path_for("2026-01-02").name == "tap-2026-01-02.jsonl"


def _naming(rid: str, record_id: str) -> dict[str, Any]:
    trace = _trace(rid)
    trace["events"][0]["fields"] = {"record_id": record_id, "index_text": "masked text"}
    return trace


def test_memory_sink_erase_drops_traces_naming_an_erased_id() -> None:
    """ADR-095: the hook drops every held root trace naming an erased record id."""
    sink = MemorySink(size=5)
    sink.write(_naming("a", "rec-gone"))
    sink.write(_naming("b", "rec-kept"))
    assert sink.erase(frozenset({"rec-gone"})) == 1
    assert [t["id"] for t in sink.traces()] == ["b"]
    assert sink.erase(frozenset({"rec-gone"})) == 0


def test_jsonl_sink_erase_rewrites_the_day_files(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path / "tap")
    sink.write(_naming("a", "rec-gone"))
    sink.write(_naming("b", "rec-kept"))
    assert sink.erase(frozenset({"rec-gone"})) == 1
    lines = sink.path_for().read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["b"]
    assert [p.name for p in (tmp_path / "tap").iterdir()] == [sink.path_for().name]
