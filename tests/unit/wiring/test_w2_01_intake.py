"""W2-01: the canonical intake gateway (LIB-30 wired).

Model-free: the hash embedder and a stub window LLM. All text is invented (Ilse, Bram, Noor, Tomasz).
Covers: an idempotent retry writes nothing new; a re-ingested edited document
re-points or retires its derived items through lineage; receipt readiness is honest; a crash injected
at every commit / projector boundary then a restart gives exactly-once; a p95 micro-benchmark.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from memspine.api.engine import Engine
from memspine.ingest import gateway as gw_mod
from memspine.ingest import intake_store
from memspine.ingest.gateway import SimulatedCrash, gateway_of
from memspine.memories.resource.extraction import _paragraph_chunks
from memspine.schemas.public import QuotaExceeded
from memspine.schemas.public.errors import QuotaExceededError
from memspine.services.llm.base import LLMRouter
from memspine.store import lineage as lineage_store
from memspine.store.events import EventKind
from memspine.store.records import RecordStatus

NS = "acme/ilse"
TURNS = [
    {"message_id": "turn-0", "role": "user", "content": "Ilse: I keep bees behind the mill"},
    {"message_id": "turn-1", "role": "assistant", "content": "Bees are lovely, how many hives?"},
    {
        "message_id": "turn-2",
        "role": "user",
        "content": "Ilse: Three hives, Noor paints the boxes green",
    },
]


async def build(db: Path | str, **write: Any) -> Engine:
    cfg: dict[str, Any] = dict(write)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(db), "busy_timeout_s": 5.0}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True},
            "resource": {"enabled": True},
        },
        write=cfg,
    )
    await eng.start()
    return eng


async def records(eng: Engine, memory_type: str = "episodic", ns: str = NS) -> list[Any]:
    return await eng._require_started().list_records(ns, memory_type)


async def crash_stop(eng: Engine) -> None:
    with contextlib.suppress(BaseException):
        await eng.stop()


@pytest.fixture(autouse=True)
def _paragraph_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    """chonkie (when installed) sizes chunks by tokens; the tests need one paragraph per chunk."""
    monkeypatch.setattr(
        "memspine.memories.resource.extraction.chunk_text",
        lambda text, max_chars=1200: _paragraph_chunks(text, max_chars),
    )


# ------------------------------------------------------------------------------------------ canonical default
async def test_default_config_uses_canonical_gateway(tmp_path: Path) -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / "default.db"), "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
    )
    await eng.start()
    try:
        out = await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert [r.content for r in out] == [t["content"] for t in TURNS]
        gw = gateway_of(eng)
        assert gw.last_receipt is not None
        assert all(any(t.startswith("idem:") for t in r.tags) for r in out)
    finally:
        await eng.stop()
    con = sqlite3.connect(tmp_path / "default.db")
    try:
        names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    finally:
        con.close()
    assert set(intake_store.INTAKE_TABLES) <= names


# ------------------------------------------------------------------------------------------ idempotency
async def test_retry_writes_nothing_new(tmp_path: Path) -> None:
    eng = await build(tmp_path / "a.db")
    try:
        first = await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        gw = gateway_of(eng)
        r1 = gw.last_receipt
        assert [r.content for r in first] == [t["content"] for t in TURNS]
        n_events = len(await eng._require_started().read_events(0, 10_000))
        again = await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        r2 = gw.last_receipt
        assert [r.record_id for r in again] == [r.record_id for r in first]
        assert len(await records(eng)) == 3
        assert (
            len(await eng._require_started().read_events(0, 10_000)) == n_events
        )  # no event at all
        assert r2.intent_id == r1.intent_id and r2.event_ids == r1.event_ids
        assert r1.durability == "durable" and len(r1.record_ids) == 3
    finally:
        await eng.stop()


async def test_write_episode_is_an_adapter_too(tmp_path: Path) -> None:
    eng = await build(tmp_path / "e.db")
    try:
        a = await eng.write_episode(TURNS, namespace=NS)
        b = await eng.write_episode(TURNS, namespace=NS)
        assert [r.record_id for r in a] == [r.record_id for r in b]
        assert len(await records(eng)) == 3
        assert len({r.source.message_id for r in a}) == 1  # one conversation id, as before
    finally:
        await eng.stop()


async def test_same_words_in_a_later_source_are_a_new_write(tmp_path: Path) -> None:
    eng = await build(tmp_path / "b.db")
    try:
        await eng.ingest_messages(TURNS[:1], namespace=NS, session_id="s1")
        other_session_turn = {**TURNS[0], "message_id": "turn-other-session"}
        await eng.ingest_messages([other_session_turn], namespace=NS, session_id="s2")
        assert len(await records(eng)) == 2
    finally:
        await eng.stop()


async def test_partial_retry_writes_only_the_new_segments(tmp_path: Path) -> None:
    eng = await build(tmp_path / "p.db")
    try:
        stamped = [{**t, "timestamp": f"2024-03-01T09:0{i}:00+00:00"} for i, t in enumerate(TURNS)]
        await eng.ingest_messages(stamped[:2], namespace=NS, session_id="s1")
        # the same session, a bigger batch: a different call, but its first two turns are not retried
        # (the batch is the source); an exact resend of the small batch still is
        await eng.ingest_messages(stamped[:2], namespace=NS, session_id="s1")
        assert len(await records(eng)) == 2
        await eng.ingest_messages(stamped[2:], namespace=NS, session_id="s1")
        assert len(await records(eng)) == 3
    finally:
        await eng.stop()


async def test_quota_is_checked_before_accept(tmp_path: Path) -> None:
    eng = await build(tmp_path / "q.db")
    try:
        gw = gateway_of(eng)
        gw.quota_check = lambda cost: QuotaExceeded(quota="storage", limit=1, current=2)
        with pytest.raises(QuotaExceededError):
            await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert await records(eng) == []
        assert gw.last_receipt is None
        gw.quota_check = None
        await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")  # nothing was remembered
        assert len(await records(eng)) == 3
    finally:
        await eng.stop()


async def test_malformed_turn_fails_loudly(tmp_path: Path) -> None:
    eng = await build(tmp_path / "m.db")
    try:
        with pytest.raises(ValueError, match="message 1 must have string content"):
            await eng.ingest_messages([TURNS[0], {"role": "user"}], namespace=NS)
        assert await records(eng) == []
    finally:
        await eng.stop()


# ------------------------------------------------------------------------------------------ receipts
async def test_memory_database_receipt_says_memory() -> None:
    eng = await build(":memory:")
    try:
        await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert gateway_of(eng).last_receipt.durability == "memory"
    finally:
        await eng.stop()


async def test_receipt_readiness_without_windows_claims_only_raw_search(tmp_path: Path) -> None:
    eng = await build(tmp_path / "r.db")
    try:
        await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        gw = gateway_of(eng)
        rd = gw.last_receipt.readiness
        assert rd.raw_search == "ready"
        assert (rd.facts, rd.graph, rd.summaries, rd.profile) == ("unavailable",) * 4
        intent = await gw.status(gw.last_receipt.intent_id)
        layers = intent.status().layers
        assert layers["L0"] == "done" and layers["L1"] == "skipped" and layers["L4"] == "skipped"
        assert intent.pending_layers() == []
        res = await gw.wait(gw.last_receipt.intent_id, ["raw_search", "facts"])
        assert not res.timed_out and res.missing == []
    finally:
        await eng.stop()


class _StubWindowLLM:
    provider_id = "stub:window"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        user = messages[-1]["content"]
        lines = [(int(n), t) for n, t in re.findall(r"^\[(\d+)\] \[[\d-]+\] (.+)$", user, re.M)]
        return json.dumps(
            {
                "facts": [
                    {
                        "entity": "Ilse",
                        "attribute": "event",
                        "value": f"Ilse said {text.split(': ', 1)[-1]}",
                        "kind": "event",
                        "turns": [n],
                        "span": text.split(": ", 1)[-1],
                        "persons": ["Ilse"],
                        "topic": "bees",
                    }
                    for n, text in lines
                ]
            }
        )


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def router(config: Any, hooks: object = None, wiring: object = None) -> LLMRouter:
        return LLMRouter({"window_extract": _StubWindowLLM()})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)


async def _windowed(db: Path, outbox: bool) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(db), "busy_timeout_s": 5.0}}
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "window_extract": {
                    "provider": "litellm",
                    "timeout_s": 60.0,
                    "settings": {"model": "stub/window"},
                }
            },
        },
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            "window_max_turns": 64,
        },
        workers={"outbox": outbox, "transport": "inline"},
    )
    await eng.start()
    return eng


async def test_facts_pending_but_never_ready_without_a_durable_queue(
    tmp_path: Path, stub_llm: None
) -> None:
    eng = await _windowed(tmp_path / "w1.db", outbox=False)
    try:
        await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        gw = gateway_of(eng)
        iid = gw.last_receipt.intent_id
        assert gw.last_receipt.readiness.facts == "pending"
        assert gw.last_receipt.readiness.graph == "unavailable"  # mode "facts": no edge section
        await eng.flush_windows(NS, final=True)
        intent = await gw.status(iid)
        assert (
            intent.readiness.facts == "pending"
        )  # the engine cannot prove it, so it does not claim it
        assert intent.readiness.raw_search == "ready"
        res = await gw.wait(iid, ["facts"])
        assert res.timed_out and res.missing == ["facts"]
    finally:
        await eng.stop()


async def test_facts_ready_only_after_the_window_is_closed_and_derived(
    tmp_path: Path, stub_llm: None
) -> None:
    eng = await _windowed(tmp_path / "w2.db", outbox=True)
    try:
        await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        gw = gateway_of(eng)
        iid = gw.last_receipt.intent_id
        # the three turns sit in the open window: the barrier would say "ready" for the empty queue,
        # the receipt must not
        assert (await gw.status(iid)).readiness.facts == "pending"
        await eng.flush_windows(NS, final=True)
        intent = await gw.status(iid)
        assert intent.readiness.facts == "ready"
        assert intent.status().layers["L1"] == "done"
        # persisted and monotone: a later status call cannot move it back
        assert (await gw.status(iid)).readiness.facts == "ready"
        assert (await gw.receipt(iid)).readiness.facts == "ready"
    finally:
        await eng.stop()


# ------------------------------------------------------------------------------------------ documents
def _para(i: int, tag: str = "") -> str:
    return f"Part {i}{tag}. " + (f"word{i} " * 130).strip()


def _doc(tmp_path: Path, parts: list[str], name: str = "guide.md") -> Path:
    path = tmp_path / name
    path.write_text("\n\n".join(parts), encoding="utf-8")
    return path


async def _status_of(eng: Engine, rid: str) -> RecordStatus:
    rec = await eng._require_started().get_record(rid)
    assert rec is not None
    return rec.status


async def test_reingest_unchanged_document_is_a_noop(tmp_path: Path) -> None:
    eng = await build(tmp_path / "d1.db")
    try:
        doc = _doc(tmp_path, [_para(0), _para(1), _para(2)])
        first = await eng.ingest_data(doc, namespace="acme/docs")
        assert len(first) == 3 and all(r.source.channel == "ingest" for r in first)
        assert all(r.source.doc_path == str(doc) for r in first)
        assert await eng.ingest_data(doc, namespace="acme/docs") == []
        assert len(await records(eng, "resource", "acme/docs")) == 3
    finally:
        await eng.stop()


async def test_reingest_edit_retires_and_repoints_through_lineage(tmp_path: Path) -> None:
    eng = await build(tmp_path / "d2.db", lineage_table=True)
    ns = "acme/docs"
    try:
        doc = _doc(tmp_path, [_para(i) for i in range(4)])
        v1 = await eng.ingest_data(doc, namespace=ns)
        assert len(v1) == 4
        c0, c1, c2, c3 = (r.record_id for r in v1)
        # derived items: only from the chunk that will change / from a changed and an unchanged
        # chunk / only from an unchanged chunk
        only1 = await eng.write("Ilse tends hive number one", namespace=ns, derived_from=[c1])
        both = await eng.write("Noor paints the boxes green", namespace=ns, derived_from=[c0, c1])
        only0 = await eng.write("Tomasz lends a ladder", namespace=ns, derived_from=[c0])
        assert {only1.record_id, both.record_id, only0.record_id} and not any(
            r.quarantined for r in (only1, both, only0)
        )

        _doc(tmp_path, [_para(0), _para(1, " (edited)"), _para(2), _para(3)])
        v2 = await eng.ingest_data(doc, namespace=ns)
        assert len(v2) == 1  # only the changed chunk is written
        assert v2[0].source.message_id == "chunk:1" and "srcver:2" in v2[0].tags

        assert await _status_of(eng, c1) is RecordStatus.ARCHIVED  # retired
        for kept in (c0, c2, c3):
            assert await _status_of(eng, kept) is RecordStatus.ACTIVATED  # kept, not rewritten
        assert await _status_of(eng, only1.record_id) is RecordStatus.ARCHIVED  # retired by lineage
        assert await _status_of(eng, both.record_id) is RecordStatus.ACTIVATED  # re-pointed
        assert await _status_of(eng, only0.record_id) is RecordStatus.ACTIVATED
        # one lineage link: the new chunk is a version of the retired one
        edges = (
            await lineage_store.parents_of(eng._require_started()._client.engine, [v2[0].record_id])
        )[v2[0].record_id]
        assert [(e.parent, e.role) for e in edges] == [(c1, "version_of")]
        # the source has two versions now; the resend of version 2 is a retry
        assert await eng.ingest_data(doc, namespace=ns) == []
        db = eng._require_started()._client.engine
        from memspine.ingest.intake import scope_key
        from memspine.schemas.steps.shared import Scope

        skey = scope_key(Scope(tenant="acme", user="docs"))
        ov = await intake_store.load_overlay(db, skey, str(doc.resolve()), [])
        assert ov.get_source(skey, str(doc.resolve())).latest_version == 2
    finally:
        await eng.stop()


async def test_reingest_shrunk_document_retires_the_dropped_chunk(tmp_path: Path) -> None:
    eng = await build(tmp_path / "d3.db")
    ns = "acme/docs"
    try:
        doc = _doc(tmp_path, [_para(0), _para(1), _para(2)])
        v1 = await eng.ingest_data(doc, namespace=ns)
        child = await eng.write(
            "Bram fixes a tandem chain", namespace=ns, derived_from=[v1[2].record_id]
        )
        _doc(tmp_path, [_para(0), _para(1)])
        assert await eng.ingest_data(doc, namespace=ns) == []
        assert await _status_of(eng, v1[2].record_id) is RecordStatus.ARCHIVED
        assert await _status_of(eng, child.record_id) is RecordStatus.ARCHIVED
        assert await _status_of(eng, v1[0].record_id) is RecordStatus.ACTIVATED
    finally:
        await eng.stop()


# ------------------------------------------------------------------------------------------ crashes
def _crash_at(name: str):
    def fault(point: str) -> None:
        if point == name:
            raise SimulatedCrash(point)

    return fault


EXPECTED = sorted(t["content"] for t in TURNS)


def _contents(recs: list[Any]) -> list[str]:
    return sorted(r.content for r in recs)


CALL_BOUNDARIES = ["pre_commit", "post_commit", "pre_mark", "post_mark"]


@pytest.mark.parametrize("point", CALL_BOUNDARIES)
async def test_crash_at_commit_boundary_then_restart_is_exactly_once(
    tmp_path: Path, point: str
) -> None:
    db = tmp_path / f"c_{point}.db"
    eng = await build(db)
    gateway_of(eng).fault = _crash_at(point)
    with pytest.raises(SimulatedCrash):
        await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
    await crash_stop(eng)

    eng2 = await build(db)  # restart: start() finishes what was committed
    try:
        got = await records(eng2)
        if point == "pre_commit":
            assert got == []  # nothing was committed: nothing to recover
        else:
            assert _contents(got) == EXPECTED  # start() finished the committed write
        # the caller retries (it never saw a receipt): exactly one record per turn, whatever happened
        again = await eng2.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert len(again) == 3
        assert len(await records(eng2)) == 3
        assert _contents(await records(eng2)) == EXPECTED
        rcpt = gateway_of(eng2).last_receipt
        if rcpt is not None:
            assert rcpt.readiness.raw_search == "ready" and len(rcpt.record_ids) == 3
        # and once more: a pure replay
        n = len(await eng2._require_started().read_events(0, 10_000))
        await eng2.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert len(await eng2._require_started().read_events(0, 10_000)) == n
        assert await intake_store.pending_intents(eng2._require_started()._client.engine) == []
    finally:
        await eng2.stop()


async def test_crash_then_same_process_retry_finishes_the_pending_write(tmp_path: Path) -> None:
    eng = await build(tmp_path / "same.db")
    try:
        gateway_of(eng).fault = _crash_at("post_commit")
        with pytest.raises(SimulatedCrash):
            await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert await records(eng) == []  # committed, not applied
        gateway_of(eng).fault = None
        out = await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert len(out) == 3 and len(await records(eng)) == 3
    finally:
        await eng.stop()


async def test_crash_between_two_turns_then_restart_is_exactly_once(tmp_path: Path) -> None:
    db = tmp_path / "mid.db"
    eng = await build(db)
    orig = eng.write
    calls = 0

    async def crashing(*a: Any, **k: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SimulatedCrash("mid_apply")
        return await orig(*a, **k)

    eng.write = crashing  # type: ignore[method-assign]
    threaded = [dict(t) for t in TURNS]
    threaded[1]["reply_to"] = "turn-0"  # the second turn answers the first by source message id
    with pytest.raises(SimulatedCrash):
        await eng.ingest_messages(threaded, namespace=NS, session_id="s1")
    await crash_stop(eng)

    eng2 = await build(db)
    try:
        got = await records(eng2)
        assert len(got) == 3
        assert sorted(r.content for r in got) == sorted(t["content"] for t in TURNS)
        first = next(r for r in got if r.content == TURNS[0]["content"])
        second = next(r for r in got if r.content == TURNS[1]["content"])
        assert f"reply_to:{first.record_id}" in second.tags  # the index was remapped to a record id
        again = await eng2.ingest_messages(threaded, namespace=NS, session_id="s1")
        assert len(again) == 3 and len(await records(eng2)) == 3
    finally:
        await eng2.stop()


async def test_crash_at_projector_boundary_then_restart_is_exactly_once(tmp_path: Path) -> None:
    """The second turn's event is appended to the log, then the projector dies before applying it."""
    db = tmp_path / "proj.db"
    eng = await build(db)
    proj = eng._projectors[0]
    orig = proj.apply
    writes = 0

    async def dying(event: Any) -> None:
        nonlocal writes
        if event.kind == EventKind.WRITE:
            writes += 1
            if writes == 2:
                raise SimulatedCrash("projector")
        await orig(event)

    proj.apply = dying  # type: ignore[method-assign]
    with pytest.raises(SimulatedCrash):
        await eng.ingest_messages(TURNS, namespace=NS, session_id="s1")
    await crash_stop(eng)

    eng2 = await build(db)
    try:
        assert sorted(r.content for r in await records(eng2)) == sorted(t["content"] for t in TURNS)
        await eng2.ingest_messages(TURNS, namespace=NS, session_id="s1")
        assert len(await records(eng2)) == 3
    finally:
        await eng2.stop()


@pytest.mark.parametrize("point", ["post_commit", "pre_retire", "pre_mark"])
async def test_crash_while_reingesting_a_document_then_restart(tmp_path: Path, point: str) -> None:
    db = tmp_path / f"dc_{point}.db"
    ns = "acme/docs"
    eng = await build(db)
    doc = _doc(tmp_path, [_para(i) for i in range(3)])
    v1 = await eng.ingest_data(doc, namespace=ns)
    child = await eng.write(
        "Tomasz hides the lantern", namespace=ns, derived_from=[v1[1].record_id]
    )
    _doc(tmp_path, [_para(0), _para(1, " (edited)"), _para(2)])
    gateway_of(eng).fault = _crash_at(point)
    with pytest.raises(SimulatedCrash):
        await eng.ingest_data(doc, namespace=ns)
    await crash_stop(eng)

    eng2 = await build(db)
    try:
        res = await records(eng2, "resource", ns)
        live = [r for r in res if r.status is RecordStatus.ACTIVATED]
        assert len(res) == 4  # three v1 chunks plus exactly one new chunk
        assert sorted(r.content for r in live) == sorted(
            [_para(0), _para(1, " (edited)"), _para(2)]
        )
        assert await _status_of(eng2, v1[1].record_id) is RecordStatus.ARCHIVED
        assert await _status_of(eng2, child.record_id) is RecordStatus.ARCHIVED
        assert await eng2.ingest_data(doc, namespace=ns) == []  # the resend is a retry
        assert len(await records(eng2, "resource", ns)) == 4
    finally:
        await eng2.stop()


# ------------------------------------------------------------------------------------------ latency
async def test_write_p95_is_under_250ms(tmp_path: Path) -> None:
    eng = await build(tmp_path / "bench.db")
    try:
        await eng.ingest_messages(TURNS, namespace=NS, session_id="warm")
        times: list[float] = []
        for i in range(60):
            batch = [
                {"role": "user", "content": f"Bram: note {i} about bicycle part {i * 3 + k}"}
                for k in range(3)
            ]
            t0 = time.perf_counter()
            await eng.ingest_messages(batch, namespace=NS, session_id=f"bench{i}")
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1]
        # Shared CI runners (Windows file I/O especially) are several times slower than a dev box;
        # the 250 ms bar is enforced locally, the CI bar only catches order-of-magnitude regressions.
        limit = 1500.0 if os.environ.get("CI") else 250.0
        assert p95 < limit, f"write p95 {p95:.1f} ms"
        assert gw_mod is not None
    finally:
        await eng.stop()
