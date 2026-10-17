"""TI-1: engine trace hooks behind ``observability.inspect``. Hash embedder, stub LLMs, an invented
conversation (Bram, Ilse, Noor, Tomasz). Flag off: no new event and the stored records are the
same; flag on: each event shows up with the ``inspect_scope`` correlation keys."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import FakeWindowLLM

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.models.providers.llm.base import LLMRouter
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.prompts.structured import STRUCTURED_SYSTEM

T0 = datetime(2023, 3, 1, 9, 0, tzinfo=UTC)
LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)

NEW_EVENTS = {
    "store.index.record",
    "write.turn.bound",
    "write.window.members",
    "write.extract.io",
    "write.envelope.trace",
    "write.integrate.ladder",
    "write.edges.pending",
    "write.edges.consumed",
    "job.stage.item",
    "compose.headers",
    "read.rerank.components",
    "read.planner.io",
    "read.retrieve.writeback",
}


@pytest.fixture(autouse=True)
def _reset():
    tap.shutdown()
    insp.configure(False)
    yield
    tap.shutdown()
    insp.configure(False)


class CityLLM:
    """One ``state`` item per transcript line ``Ilse: I live in <City>`` (the span is the city)."""

    provider_id = "fake:city"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        assert messages[0]["content"] == STRUCTURED_SYSTEM
        items = []
        for n, text in LINE.findall(messages[-1]["content"]):
            who, said = text.split(": ", 1)
            city = said.rsplit(" ", 1)[1]
            items.append(
                {
                    "s": who, "r": "lives_in", "o": city, "text": f"{who} lives in {city}",
                    "kind": "state", "mode": "asserted", "neg": False, "when": None,
                    "until": None, "lines": [int(n)], "span": city, "speaker": who,
                }
            )  # fmt: skip
        return json.dumps(items)


class PlanLLM:
    provider_id = "fake:plan"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        return json.dumps({"mode": "lookup", "entities": ["Bram"], "subqueries": []})


def _flat(trace: dict[str, Any]) -> list[dict[str, Any]]:
    out = [dict(e, request=trace["kind"]) for e in trace["events"]]
    for child in trace["children"]:
        out += _flat(child)
    return out


_SNAP: list[dict[str, Any]] = []


async def _stop(eng: Engine) -> None:
    """Snapshot the memory sink first: an engine that owns the tap shuts it down on stop."""
    _SNAP[:] = tap.memory_traces()
    await eng.stop()


def _events(name: str | None = None) -> list[dict[str, Any]]:
    traces = tap.memory_traces() or _SNAP
    flat = [e for t in traces for e in _flat(t)]
    return [e for e in flat if name is None or e["stage"] == name]


def _fields(name: str) -> list[dict[str, Any]]:
    return [e["fields"] for e in _events(name)]


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    window_llm: Any,
    *,
    inspect: bool,
    content: bool = False,
    path: Any = ":memory:",
    roles: dict[str, Any] | None = None,
    write: dict[str, Any] | None = None,
    **extra: Any,
) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": window_llm, **(roles or {})})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    kw: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {
            "provider": "sqlite",
            "settings": {"path": str(path), "busy_timeout_s": 5.0},
        },
        "embedding": {"provider": "hash", "settings": {}},
        "read": {"rerank": "off", "record_access": True},
        "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        "write": {
            "window_extract": "all",
            "window_async": False,
            "window_idle_s": 0.0,
            **(write or {}),
        },
        "llm": {
            "roles": {
                "window_extract": {
                    "provider": "litellm",
                    "settings": {"model": "fake/window", "timeout_seconds": 30.0}
                }
            }
        },
        "observability": {
            "inspect": inspect,
            "inspect_content": content,
            "tap": {"enabled": True, "sink": "memory"},
        },
    }
    kw.update(extra)
    eng = Engine(**kw)
    await eng.start()
    return eng


def _msgs(count: int, speaker: str = "Bram") -> list[dict[str, str]]:
    things = [
        "I repotted the orchid on the balcony",
        "We booked a kayak tour along the northern coast",
        "The violin recital starts at seven on Friday",
        "My brother moved to a flat near the harbour",
        "Our dog Pip learned to open the garden gate",
    ]
    return [
        {
            "role": "user",
            "content": f"{speaker}: {things[i % len(things)]}",
            "timestamp": (T0 + timedelta(minutes=i)).isoformat(),
            "message_id": f"inspect-message-{i}",
        }
        for i in range(count)
    ]


async def _run(eng: Engine) -> None:
    """write (inside scopes) -> window flush -> sleep -> read, with the correlation keys on."""
    for i, msg in enumerate(_msgs(4)):
        with insp.inspect_scope(run_id="run1", item="conv1", turn_id=f"D1:{i + 1}"):
            await eng.ingest_messages(
                [msg], namespace="a", session_id="s1", conversation_id="conv1", group_id="s1"
            )
    with insp.inspect_scope(run_id="run1", item="conv1", phase="flush_windows"):
        await eng.flush_windows()
    with insp.inspect_scope(run_id="run1", item="conv1", phase="sleep"):
        await eng.sleep()
    with insp.inspect_scope(run_id="run1", item="conv1", query_id="Q1"):
        await eng.assemble("What did Bram say about the orchid?", namespace="a", top_k=5)


async def _stored(eng: Engine) -> list[tuple[str, tuple[str, ...], str]]:
    storage = eng._require_started()
    rows = []
    for mt in ("episodic", "semantic"):
        for r in await storage.list_records("a", mt):
            content = re.sub(r"[0-9a-f]{12}$", "", r.content)
            tags = tuple(
                sorted(
                    re.sub(r"[0-9a-f]{12}", "", t)
                    for t in r.tags
                    if not t.startswith("idem:")
                )
            )
            rows.append((content, tags, r.status.value))
    return sorted(rows)


# --- flag off ------------------------------------------------------------------------------
def test_config_defaults_are_off() -> None:
    obs = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    ).observability
    assert obs.inspect is False and obs.inspect_content is False


async def test_flag_off_emits_no_new_event_and_scope_is_inert(monkeypatch) -> None:
    eng = await _engine(monkeypatch, FakeWindowLLM(), inspect=False)
    try:
        await _run(eng)
        assert not insp.inspect_enabled()
        names = {e["stage"] for e in _events()}
        assert names and not (names & NEW_EVENTS)
        assert all("run_id" not in e["fields"] and "item" not in e["fields"] for e in _events())
    finally:
        await _stop(eng)


async def test_flag_on_leaves_the_stored_records_unchanged(monkeypatch) -> None:
    off = await _engine(monkeypatch, FakeWindowLLM(), inspect=False)
    try:
        await _run(off)
        before = await _stored(off)
    finally:
        await _stop(off)
    tap.shutdown()
    on = await _engine(monkeypatch, FakeWindowLLM(), inspect=True, content=True)
    try:
        await _run(on)
        after = await _stored(on)
    finally:
        await _stop(on)
    assert before and before == after


# --- flag on: correlation ------------------------------------------------------------------
async def test_scope_keys_ride_every_event_inside_the_scope(monkeypatch) -> None:
    eng = await _engine(monkeypatch, FakeWindowLLM(), inspect=True)
    try:
        await _run(eng)
    finally:
        await _stop(eng)
    bound = _fields("write.turn.bound")
    assert [b["turn_id"] for b in bound] == ["D1:1", "D1:2", "D1:3", "D1:4"]
    assert all(b["run_id"] == "run1" and b["item"] == "conv1" for b in bound)
    assert len({b["rid"] for b in bound}) == 4
    # pre-record events of the same turn carry the same keys (and no rid): that is the join
    pre = [f for f in _fields("write.role.turn") + _fields("write.stamp.turn") if "turn_id" in f]
    assert all(f["run_id"] == "run1" for f in pre)
    # the raw-turn index events are keyed by turn
    idx = _fields("store.index.record")
    by_rid = {f["record_id"]: f for f in idx}
    for b in bound:
        assert by_rid[b["rid"]]["turn_id"] == b["turn_id"]
        assert by_rid[b["rid"]]["vector"] is True and by_rid[b["rid"]]["index_text_len"] > 0
    q = [e["fields"] for e in _events("read.retrieve.writeback")]
    assert q and all(f["query_id"] == "Q1" and f["item"] == "conv1" for f in q)


async def test_nested_scopes_merge_and_inner_wins() -> None:
    tap.configure(
        __import__("memspine.config.schema.observability", fromlist=["TapConfig"]).TapConfig(
            enabled=True, sink="memory"
        )
    )
    insp.configure(True)
    with insp.inspect_scope(run_id="r", item="a", turn_id="t1"):
        with insp.inspect_scope(item="b", window_id="w9", skipped=None):
            tap.event("probe.nested", n=1)
        tap.event("probe.outer", n=2)
    tap.event("probe.after", n=3)
    got = {e["stage"]: e["fields"] for e in _events()}
    assert got["probe.nested"] == {
        "run_id": "r",
        "item": "b",
        "turn_id": "t1",
        "window_id": "w9",
        "n": 1,
    }
    assert got["probe.outer"] == {"run_id": "r", "item": "a", "turn_id": "t1", "n": 2}
    assert got["probe.after"] == {"n": 3}


def test_scope_is_a_null_context_when_off() -> None:
    from contextlib import nullcontext

    assert isinstance(insp.inspect_scope(run_id="x"), type(nullcontext()))
    assert not insp.inspect_enabled() and not insp.inspect_content()


# --- flag on: each event ------------------------------------------------------------------
async def test_window_extraction_and_edges_and_sleep_items(monkeypatch) -> None:
    eng = await _engine(
        monkeypatch,
        FakeWindowLLM(),
        inspect=True,
        content=True,
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True, "policies": {"extract_graph": {"mode": "all"}}},
        },
    )
    try:
        await _run(eng)
    finally:
        await _stop(eng)
    rids = {f["rid"] for f in _fields("write.turn.bound")}
    members = _fields("write.window.members")
    assert members and members[0]["status"] == "planned"
    assert set(members[0]["member_record_ids"]) <= rids and members[0]["window_id"]
    assert members[0]["cut_reason"] and members[0]["run_id"] == "run1"
    io = _fields("write.extract.io")
    assert io and io[0]["window_id"] == members[0]["window_id"]
    assert io[0]["sections"] and io[0]["cached"] is False
    assert "parsed_counts" in io[0]
    pending = _fields("write.edges.pending")
    assert pending and pending[0]["edges"] >= 1
    consumed = _fields("write.edges.consumed")
    assert consumed and members[0]["window_id"] in consumed[0]["window_ids"]
    ladder = _fields("write.integrate.ladder")
    assert ladder and {"rid", "verdict", "rule", "action"} <= set(ladder[0])
    assert {f["action"] for f in ladder} & {"added", "updated", "merged"}
    # mined facts carry the window id through the whole write door
    assert any(f.get("window_id") == members[0]["window_id"] for f in _fields("store.index.record"))
    items = _fields("job.stage.item")
    assert items and all(f["stage"] for f in items)
    assert any(f.get("output_ids") and f.get("channel") for f in items)
    assert {"consolidate", "extract_graph"} <= {f["stage"] for f in items}
    # TI-5 follow-ups: the cycle number rides on every item, the extraction call has token counts,
    # and a consolidate item (payload session_key + base session_key) no longer fails the stage
    assert all(isinstance(f["cycle"], int) and f["cycle"] >= 1 for f in items)
    assert io[0]["tokens"] and io[0]["tokens"]["prompt"] > 0 and io[0]["tokens"]["completion"] > 0
    ends = {f["stage"]: f for f in _fields("job.stage.end")}
    assert ends["consolidate"]["status"] != "partial" and not ends["consolidate"].get("failed")
    assert any(f.get("event") == "memory.link" and f.get("src") for f in items)


async def test_skipped_and_failed_windows_still_report_their_members(monkeypatch) -> None:
    llm = FakeWindowLLM()
    llm.garbage_when_joint = True
    eng = await _engine(monkeypatch, llm, inspect=True)
    try:
        for i, msg in enumerate(_msgs(3)):
            with insp.inspect_scope(turn_id=f"D2:{i + 1}"):
                await eng.ingest_messages([msg], namespace="a", session_id="s2", group_id="s2")
        await eng.flush_windows()
    finally:
        await _stop(eng)
    statuses = {f["status"] for f in _fields("write.window.members")}
    assert "planned" in statuses
    assert "failed" in statuses
    members = _fields("write.window.members")
    assert all(f["member_record_ids"] for f in members)


async def test_content_off_drops_text_and_on_keeps_it(monkeypatch) -> None:
    eng = await _engine(monkeypatch, FakeWindowLLM(), inspect=True, content=False)
    try:
        await _run(eng)
    finally:
        await _stop(eng)
    io = _fields("write.extract.io")[0]
    assert "prompt_text" not in io and "reply_text" not in io and io["prompt_hash"]
    assert "index_text" not in _fields("store.index.record")[0]
    tap.shutdown()
    eng = await _engine(monkeypatch, FakeWindowLLM(), inspect=True, content=True)
    try:
        await _run(eng)
    finally:
        await _stop(eng)
    first = _fields("write.extract.io")[0]
    assert "Bram" in first.get("parsed_text", "") or "Bram" in first.get("prompt_text", "")
    assert "orchid" in _fields("store.index.record")[0]["index_text"]


async def test_envelope_lifecycle_occ_to_rid(monkeypatch) -> None:
    eng = await _engine(
        monkeypatch,
        CityLLM(),
        inspect=True,
        content=True,
        write={
            "window_extract": "facts",
            "envelope_v2": True,
            "temporal_v2": True,
            "grounding_v2": True,
            "integration_v2": True,
        },
    )
    try:
        for i, city in enumerate(("Oslo", "Bergen")):
            msg = [
                {
                    "role": "user",
                    "content": f"Ilse: I live in {city}",
                    "timestamp": (T0 + timedelta(days=i)).isoformat(),
                }
            ]
            with insp.inspect_scope(run_id="run1", item="conv1", turn_id=f"D1:{i + 1}"):
                await eng.ingest_messages(msg, namespace="a", session_id="s1", group_id="s1")
        with insp.inspect_scope(run_id="run1", item="conv1"):
            await eng.flush_windows()
        facts = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if "window_fact" in r.tags
        ]
    finally:
        await _stop(eng)
    trace = _fields("write.envelope.trace")
    stages = [f["stage"] for f in trace]
    assert {"compile", "temporal", "integration", "grounding", "deposit"} <= set(stages)
    deposits = [f for f in trace if f["stage"] == "deposit"]
    assert len(deposits) == len(facts) == 2
    assert {d["record_id"] for d in deposits} == {f.record_id for f in facts}
    assert all(d["status"] == "kept" and d["occ"] and d["run_id"] == "run1" for d in deposits)
    # one trace row per envelope per stage, joined by occ
    occ = deposits[0]["occ"]
    assert {f["stage"] for f in trace if f["occ"] == occ} >= {"compile", "integration", "deposit"}
    integ = [f for f in trace if f["stage"] == "integration"]
    assert all(f["status"] in {"kept", "rewritten"} for f in integ)
    assert any(f["fields_changed"] and "verdict" in f["fields_changed"] for f in integ)
    assert any(f["integrate"]["op"] in {"supersede", "append"} for f in integ)
    ladder = _fields("write.integrate.ladder")
    assert {f["action"] for f in ladder} & {"updated", "added"}


async def test_read_side_events(monkeypatch) -> None:
    eng = await _engine(
        monkeypatch,
        FakeWindowLLM(),
        inspect=True,
        content=True,
        roles={"plan": PlanLLM()},
        read={"rerank": "off", "record_access": True, "planner": "llm"},
        llm={
            "roles": {
                "window_extract": {
                    "provider": "litellm",
                    "settings": {"model": "fake/window", "timeout_seconds": 30.0}
                },
                "plan": {
                    "provider": "litellm",
                    "settings": {"model": "fake/plan", "timeout_seconds": 30.0},
                },
            }
        },
    )
    try:
        for _i, msg in enumerate(_msgs(4)):
            await eng.ingest_messages([msg], namespace="a", session_id="s1", group_id="s1")
        with insp.inspect_scope(query_id="Q9"):
            await eng.search("What did Bram say about the orchid?", namespace="a", top_k=3)
            await eng.assemble("What did Bram say about the orchid?", namespace="a", top_k=3)
            await eng.read("What did Bram say about the orchid?", namespace="a", mode="auto")
            assert await eng._llm_read_plan("What did Bram say about the orchid?") is not None
    finally:
        await _stop(eng)
    comp = _fields("read.rerank.components")
    assert comp and comp[0]["items"] and comp[0]["query_id"] == "Q9"
    row = comp[0]["items"][0]
    assert {"rid", "rank", "rel", "rec", "imp", "util", "trust", "score"} <= set(row)
    wb = _fields("read.retrieve.writeback")
    assert wb and wb[0]["n"] >= 1 and wb[0]["access_event"] is True
    heads = _fields("compose.headers")
    assert heads and all(h["query_id"] == "Q9" and "headers" in h for h in heads)
    plans = _fields("read.planner.io")
    assert plans and plans[0]["role"] == "plan"


async def test_planner_unbound_is_reported() -> None:
    from memspine.config.schema.observability import TapConfig
    from memspine.read.pipeline.request import _llm_read_plan

    tap.configure(TapConfig(enabled=True, sink="memory"))
    insp.configure(True)

    class _Eng:
        _llm = None
        _prompts = None

    with tap.request("read"):
        assert await _llm_read_plan(_Eng(), "Where does Noor live?") is None
    assert _fields("read.planner.io")[0]["outcome"] == "unbound"


async def test_outbox_prepare_and_commit_run_in_a_job_request(monkeypatch, tmp_path) -> None:
    eng = await _engine(
        monkeypatch,
        FakeWindowLLM(),
        inspect=True,
        path=tmp_path / "ob.db",
        workers={"outbox": True},
    )
    try:
        for _i, msg in enumerate(_msgs(3)):
            await eng.ingest_messages([msg], namespace="a", session_id="s1", group_id="s1")
        await eng.flush_windows()
    finally:
        await _stop(eng)
    jobs = []

    def walk(t: dict[str, Any]) -> None:
        if t["kind"] == "job" and str(t["fields"].get("stage", "")).startswith("outbox_"):
            jobs.append(t)
        for c in t["children"]:
            walk(c)

    for t in _SNAP:
        walk(t)
    assert jobs
    stages = {e["stage"] for j in jobs for e in j["events"]}
    assert "runtime.outbox.prepare.start" in stages and "runtime.outbox.commit_item.end" in stages
    assert _fields("write.window.members")
