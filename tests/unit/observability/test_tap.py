"""WT-01 wiretap: core API, guard, sinks, gap report, HTML, off-path overhead."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

import pytest

from memspine.config.schema.observability import ObservabilityConfig, TapConfig
from memspine.observability import tap, tap_report


@pytest.fixture(autouse=True)
def _tap_off():
    tap.shutdown()
    yield
    tap.shutdown()


def _on(**kw):
    tap.configure(TapConfig(enabled=True, sink=kw.pop("sink", "memory"), **kw))


def test_off_by_default_no_events_no_files(tmp_path):
    assert ObservabilityConfig().tap.enabled is False
    assert not tap.enabled()
    with tap.request("read", namespace="n"):
        tap.event("read.x.y", n=1)
        with tap.span("read.z"):
            pass
    assert tap.memory_traces() == []
    assert tap.content("secret text") == ""
    assert list(tmp_path.iterdir()) == []


def test_configure_disabled_stays_off():
    tap.configure(TapConfig(enabled=False))
    assert not tap.enabled()


def test_memory_sink_records_events_per_request():
    _on()
    with tap.request("read", namespace="ns"):
        tap.event("read.leg.vector", n=3, ids=["a", "b", "c"], score=0.5)
        with tap.span("read.rerank"):
            pass
    with tap.request("write"):
        tap.event("write.firewall.verdict", decision="allow")
    traces = tap.memory_traces()
    assert [t["kind"] for t in traces] == ["read", "write"]
    r = traces[0]
    assert r["fields"] == {"namespace": "ns"}
    assert [e["stage"] for e in r["events"]] == [
        "read.leg.vector",
        "read.rerank.start",
        "read.rerank.end",
    ]
    assert [e["seq"] for e in r["events"]] == [0, 1, 2]
    assert "duration_ms" in r["events"][2]["fields"]


def test_nested_requests_attach_to_parent():
    _on()
    with tap.request("read"):
        tap.event("a.b.c", n=1)
        with tap.request("read", sub=1):
            tap.event("a.b.child", n=2)
    traces = tap.memory_traces()
    assert len(traces) == 1
    child = traces[0]["children"][0]
    assert child["parent_id"] == traces[0]["id"]
    assert child["events"][0]["stage"] == "a.b.child"


def test_event_outside_request_makes_adhoc_trace():
    _on()
    tap.event("x.y.z", n=1)
    assert tap.memory_traces()[0]["kind"] == "adhoc"


def test_field_guard_drops_free_text_with_marker():
    _on()
    with tap.request("read"):
        tap.event("read.q", query="what is my name?", n=2, ok=True, ids=["a", "b b"], bad=object())
    ev = tap.memory_traces()[0]["events"][0]["fields"]
    assert "query" not in ev and "bad" not in ev
    assert ev["n"] == 2 and ev["ok"] is True
    assert set(ev["field_rejected"]) == {"query", "ids", "bad"}


def test_content_opt_in_truncates_and_masks():
    _on(content=True, max_content_chars=30)
    with tap.request("read"):
        tap.event("read.q", text=tap.content("mail bob@example.com about the very long plan " * 3))
    t = tap.memory_traces()[0]["events"][0]["fields"]["text"]
    assert len(t) <= 30 and "bob@example.com" not in t
    assert tap.content_enabled()


def test_content_off_is_absent_not_rejected():
    _on()
    with tap.request("read"):
        tap.event("read.q", text=tap.content("hello world"), n=1)
    assert tap.memory_traces()[0]["events"][0]["fields"] == {"n": 1}


def test_kinds_filter_and_sample_rate():
    _on(kinds=["read"])
    with tap.request("write"):
        tap.event("w.a.b", n=1)
    assert tap.memory_traces() == []
    tap.shutdown()
    _on(sample_rate=0.0)
    with tap.request("read"):
        tap.event("r.a.b", n=1)
    assert tap.memory_traces() == []


def test_async_isolation():
    _on()

    async def one(i):
        with tap.request("read", i=i):
            await asyncio.sleep(0)
            tap.event("r.a.b", i=i)

    async def main():
        await asyncio.gather(*(one(i) for i in range(5)))

    asyncio.run(main())
    traces = tap.memory_traces()
    assert len(traces) == 5
    assert all(t["events"][0]["fields"]["i"] == t["fields"]["i"] for t in traces)


def test_traced_decorator_and_exception_in_span():
    _on()

    @tap.traced("job")
    async def sleep(namespace="d"):
        tap.event("job.x.y", n=1)

    asyncio.run(sleep(namespace="d"))
    t = tap.memory_traces()[0]
    assert t["kind"] == "job" and t["fields"]["op"] == "sleep"
    with pytest.raises(ValueError), tap.request("read"), tap.span("r.boom"):
        raise ValueError("x")
    end = tap.memory_traces()[1]["events"][-1]["fields"]
    assert end["error"] == "ValueError"


def test_forensics_bridge_emits_cuts():
    from memspine.core import trace_sink

    _on()
    with tap.request("read"):
        trace_sink.cut("theta_abstain", "rec1", score=0.1)
        trace_sink.FORENSICS.get()["vector"] = [("rec1", 0.9), ("rec2", 0.4)]
    ev = {e["stage"]: e["fields"] for e in tap.memory_traces()[0]["events"]}
    assert ev["read.forensics.cut"]["reason"] == "theta_abstain"
    assert ev["read.forensics.vector"]["ids"] == ["rec1", "rec2"]
    assert trace_sink.FORENSICS.get() is None


def test_jsonl_sink(tmp_path):
    tap.configure(TapConfig(enabled=True, sink="jsonl", dir=str(tmp_path)))
    with tap.request("read"):
        tap.event("r.a.b", n=1)
    tap.shutdown()
    files = list(tmp_path.glob("tap-*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows[0]["events"][0]["stage"] == "r.a.b"
    assert len(tap_report.load_traces(tmp_path)) == 1


def test_sqlite_sink(tmp_path):
    tap.configure(TapConfig(enabled=True, sink="sqlite", dir=str(tmp_path)))
    with tap.request("read"):
        tap.event("r.a.b", n=1)
        with tap.request("read"):
            tap.event("r.c.d", n=2)
    tap.shutdown()
    db = sqlite3.connect(tmp_path / "tap.sqlite")
    assert db.execute("select count(*) from requests").fetchone()[0] == 2
    assert db.execute("select count(*) from events").fetchone()[0] == 2
    db.close()
    loaded = tap_report.load_traces(tmp_path)
    assert len(loaded) == 1 and len(loaded[0]["children"]) == 1


def test_log_sink(caplog):
    with caplog.at_level(logging.INFO, logger="memspine.tap"):
        _on(sink="log")
        with tap.request("read"):
            tap.event("r.a.b", n=1)
    assert any("tap.request" in r.getMessage() for r in caplog.records)


def test_otel_sink_clear_error_without_package():
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        with pytest.raises(tap.TapConfigError):
            tap.configure(TapConfig(enabled=True, sink="otel"))
        assert not tap.enabled()


def test_none_sink_still_enabled():
    tap.configure(TapConfig(enabled=True, sink="none"))
    with tap.request("read"):
        tap.event("r.a.b", n=1)
    assert tap.enabled()


def _seeded():
    _on()
    with tap.request("read"):
        tap.event("read.leg.lexical", n=0)
        tap.event("read.gate.dropped", reason="visibility", ids=["a", "b"], n=2)
        tap.event("read.rerank.skipped", reason="no_reranker")
        tap.event("read.llm.call", outcome="timeout", duration_ms=9000)
        tap.event("read.forensics.cut", id="x", reason="budget")
        tap.event("read.q", text="free text here")
    with tap.request("write"):
        pass
    return tap.memory_traces()


def test_gap_report_finds_seeded_gaps():
    traces = _seeded()
    gaps = tap_report.analyse(traces)
    rules = {g.rule for gl in gaps.values() for g in gl}
    assert {
        "empty_leg",
        "dropped",
        "rerank_skipped",
        "model_timeout",
        "slow_stage",
        "field_rejected",
        "no_events",
    } <= rules
    text = tap_report.render_text(traces, gaps)
    assert "dropped by visibility: 2 (a, b)" in text
    table = tap_report.summary_table(traces, gaps)
    assert any(r["rule"] == "empty_leg" and r["requests"] == 1 for r in table)


def test_custom_rule_is_data():
    traces = _seeded()
    rule = tap_report.Rule("mine", "info", ("read.q",), "q seen")
    gaps = tap_report.analyse(traces, [rule])
    assert "mine" in [g.rule for g in gaps[traces[0]["id"]]]


def test_html_renders_and_escapes(tmp_path):
    traces = _seeded()
    out = tmp_path / "r.html"
    jl = tmp_path / "t.jsonl"
    jl.write_text("\n".join(json.dumps(t) for t in traces))
    assert tap_report.main([str(jl), "--html", str(out)]) == 0
    page = out.read_text()
    assert page.startswith("<!doctype html>") and "read.leg.lexical" in page
    assert "empty_leg" in page and "<script" not in page


def test_report_request_filter(capsys, tmp_path):
    traces = _seeded()
    jl = tmp_path / "t.jsonl"
    jl.write_text("\n".join(json.dumps(t) for t in traces))
    tap_report.main([str(jl), "--request", traces[1]["id"]])
    assert "no_events" in capsys.readouterr().out


def test_off_path_overhead():
    n = 200_000
    t0 = time.perf_counter()
    for _ in range(n):
        tap.event("r.a.b", n=1)
    per_call_us = (time.perf_counter() - t0) / n * 1e6
    assert per_call_us < 5.0  # generous: a bool test plus a call
