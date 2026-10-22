"""WT-02: the wiretap on the write and derive paths and the model-call layer."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema.observability import TapConfig
from memspine.observability import tap, tap_report
from memspine.services.llm.base import LLMRouter


@pytest.fixture(autouse=True)
def _tap_reset():
    tap.shutdown()
    yield
    tap.shutdown()


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )


def _stages(trace: dict[str, Any]) -> list[str]:
    out = [e["stage"] for e in trace["events"]]
    for child in trace["children"]:
        out += _stages(child)
    return out


async def test_model_free_write_has_write_trace() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    eng = _engine()
    await eng.start()
    try:
        await eng.write("Alice lives in Paris", namespace="a", entity="alice", attribute="city")
        await eng.ingest_messages(
            [{"role": "user", "content": "Bob: I like tea"}, {"role": "system", "content": "x"}],
            namespace="a",
            session_id="s1",
        )
    finally:
        await eng.stop()
    traces = [t for t in tap.memory_traces() if t["kind"] == "write"]
    assert traces
    seen = {s for t in traces for s in _stages(t)}
    assert {
        "write.received",
        "write.firewall.verdict",
        "write.route",
        "write.deposit",
        "write.messages.received",
    } <= seen
    deposit = next(e for t in traces for e in t["events"] if e["stage"] == "write.deposit")
    assert deposit["fields"]["action"] == "added"
    assert not any("field_rejected" in e["fields"] for t in traces for e in t["events"])


async def test_sleep_produces_job_trace_with_stage_children() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    eng = _engine()
    await eng.start()
    try:
        await eng.write("Alice lives in Paris", namespace="a")
        tap.configure(TapConfig(enabled=True, sink="memory"))  # drop the write trace
        stats = await eng.sleep()
    finally:
        await eng.stop()
    jobs = [t for t in tap.memory_traces() if t["kind"] == "job"]
    assert len(jobs) == 1
    stages = [c["fields"].get("stage") for c in jobs[0]["children"]]
    assert "consolidate" in stages and "extract_graph" in stages
    ends = [
        e["fields"]
        for c in jobs[0]["children"]
        for e in c["events"]
        if e["stage"] == "job.stage.end"
    ]
    assert len(ends) == len(stats)
    assert any(f.get("status") == "skipped" and f.get("skipped") for f in ends)
    gaps = tap_report.analyse(jobs)
    assert any(g.rule == "stage_skipped" for gl in gaps.values() for g in gl)


class _Failing:
    provider_id = "stub:failing"
    model = "stub-model"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        raise TimeoutError("boom")


class _Fine:
    provider_id = "stub:fine"
    model = "stub-model"
    last_usage = (11, 3)

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        return "ok"


async def test_failing_model_call_is_a_gap() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    router = LLMRouter({"extract": _Failing(), "judge": _Fine()})
    with tap.request("job", stage="x"):
        assert await router.for_role("judge").chat([{"role": "user", "content": "hi"}]) == "ok"
        with pytest.raises(TimeoutError):
            await router.for_role("extract").chat([{"role": "user", "content": "hi"}])
    traces = tap.memory_traces()
    calls = [e for e in traces[0]["events"] if e["stage"] == "model.call"]
    assert [c["fields"]["outcome"] for c in calls] == ["ok", "timeout"]
    assert calls[0]["fields"]["prompt_tokens"] == 11
    assert calls[0]["fields"]["completion_tokens"] == 3
    assert calls[1]["fields"]["error"] == "TimeoutError"
    assert calls[1]["fields"]["role"] == "extract"
    rules = {g.rule for gl in tap_report.analyse(traces).values() for g in gl}
    assert {"model_error", "model_timeout"} <= rules


async def test_tap_off_records_nothing() -> None:
    router = LLMRouter({"extract": _Failing()})
    with pytest.raises(TimeoutError):
        await router.for_role("extract").chat([])
    eng = _engine()
    await eng.start()
    try:
        await eng.write("Alice lives in Paris", namespace="a")
        await eng.sleep()
    finally:
        await eng.stop()
    assert not tap.enabled()
    assert tap.memory_traces() == []


def test_new_rules_fire() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    with tap.request("write"):
        tap.event("write.quarantine", id="r1", reasons=["instruction_flag"])
        tap.event("write.extract.rejected", reason="parent_gone")
        tap.event("write.extract.facts", returned=3, accepted=1, invalid=1, ungrounded=1, uncited=0)
        tap.event("write.time.parsed", anchored=True, unknown_tz=True)
        tap.event("write.window.cut", reason="idle", turns=1, tokens=5)
        tap.event("job.stage.end", stage="mine_facts", status="partial")
        tap.event("job.stage.end", stage="reflect", status="error", failed=True)
    rules = {g.rule for gl in tap_report.analyse(tap.memory_traces()).values() for g in gl}
    assert {
        "quarantine",
        "extraction_rejected",
        "time_unanchored",
        "window_anomaly",
        "stage_partial",
        "stage_failed",
    } <= rules
