# ruff: noqa: E501
"""FU-GAPS: the known non-GPU gaps. Model-free stubs, hash embedder."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_w2_06b_prompt_v2 import WindowLLM, _run
from test_window_write import _facts


# --- 1. write.prompt_v2 partial-parse recovery ------------------------------------------------
class _Edgeless(WindowLLM):
    """facts valid, edges malformed (a string)."""

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        reply = await super().chat(messages, **options)
        if "NEW:\n" not in messages[-1]["content"]:
            return reply
        data = json.loads(reply)
        data["edges"] = "not a list"
        return json.dumps(data)


async def test_valid_facts_survive_malformed_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _Edgeless()
    eng, stats = await _run(monkeypatch, llm, n=3, window_extract="all", prompt_v2=True)
    try:
        assert len(await _facts(eng)) >= 1
        assert stats["sections_dropped"] >= 1
        assert stats["parse_failures"] == 0
    finally:
        await eng.stop()


class _FlakyOnce(WindowLLM):
    """First unified reply is prose; the re-ask is answered properly."""

    def __init__(self, bad_replies: int) -> None:
        super().__init__()
        self.bad = bad_replies
        self.asks = 0

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        last = messages[-1]["content"]
        if "not a valid JSON object" in last:
            self.requests.append((messages, options))
            self.asks += 1
            if self.bad >= 2:
                return "still prose"
            return await super().chat(messages[:-2], **options)
        if "NEW:\n" in last and self.bad and self.unified == 0 and self.asks == 0:
            self.requests.append((messages, options))
            self.unified += 1
            return "I cannot do that"
        return await super().chat(messages, **options)


async def test_whole_reply_unparseable_is_re_asked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _FlakyOnce(1)
    eng, _ = await _run(monkeypatch, llm, n=3, window_extract="all", prompt_v2=True)
    try:
        assert llm.asks == 1
        assert len(await _facts(eng)) >= 1
    finally:
        await eng.stop()


async def test_still_unparseable_after_the_re_ask_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _FlakyOnce(2)
    eng, stats = await _run(monkeypatch, llm, n=3, window_extract="all", prompt_v2=True)
    try:
        assert llm.asks == 1  # one re-ask, never a loop
        assert stats["parse_failures"] >= 1 and len(await _facts(eng)) == 0
    finally:
        await eng.stop()


# --- 2. pre-pass skip with the episode section on -------------------------------------------
async def test_filler_window_asks_for_the_episode_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_window_write import FakeWindowLLM, _send
    from test_window_write import _engine as legacy_engine

    llm = FakeWindowLLM()
    eng = await legacy_engine(monkeypatch, llm, mode="all", prepass="rules")
    try:
        await _send(eng, "a", "s1", [{"role": "user", "content": "Ilse: Hi!"}, {"role": "user", "content": "Bram: thanks"}])
        stats = await eng.flush_windows()
        assert llm.calls == [["episode"]]
        assert stats["windows_prepass_skipped"] == 0
        assert stats["windows_prepass_episode_only"] == 1
        assert len(await _facts(eng)) == 0  # no facts section was asked for
    finally:
        await eng.stop()


async def test_episode_only_in_the_one_call_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_window_write import _send

    llm = WindowLLM()
    eng = await _engine_for(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", [{"role": "user", "content": "Ilse: Hi!"}, {"role": "user", "content": "Bram: thanks"}])
        stats = await eng.flush_windows()
        asked = [m[-1]["content"] for m, _ in llm.requests if "NEW:\n" in m[-1]["content"]]
        assert len(asked) == 1 and "Sections: episode." in asked[0]
        assert stats["windows_prepass_episode_only"] == 1
    finally:
        await eng.stop()


async def _engine_for(monkeypatch: pytest.MonkeyPatch, llm: WindowLLM) -> Any:
    from test_w2_06a_envelope import _engine

    return await _engine(monkeypatch, llm, window_extract="all", prompt_v2=True, prepass="rules")


# --- 3. a contest marks both sides ---------------------------------------------------------
async def _contest_run(monkeypatch: pytest.MonkeyPatch, integration: bool) -> list[Any]:
    from test_w2_10_integration import CityLLM, engine, say

    class Changed(CityLLM):
        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            items = json.loads(await super().chat(messages, **options))
            for it in items:
                it["chg"] = True
            return json.dumps(items)

    eng = await engine(monkeypatch, Changed(), integration_v2=integration)
    try:
        await say(eng, "s0", "Oslo", 0)
        await say(eng, "s1", "Rome", 4)
        rows = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if "window_fact" in r.tags
        ]
        return rows
    finally:
        await eng.stop()


async def test_contest_marks_the_older_peer_too(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = await _contest_run(monkeypatch, True)
    by_city = {r.content.rsplit(" ", 1)[1]: r for r in rows}
    assert set(by_city) == {"Oslo", "Rome"}
    assert "disputed" in by_city["Rome"].tags
    assert "disputed" in by_city["Oslo"].tags
    assert all(r.status.value == "activated" for r in rows)  # both stay live


async def test_flag_off_leaves_the_old_peer_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = await _contest_run(monkeypatch, False)
    assert not any("disputed" in r.tags for r in rows)


# --- 4. learned registries survive a restart --------------------------------------------------
async def _learn_run(
    monkeypatch: pytest.MonkeyPatch, path: Any, persist: bool, *, coin: bool
) -> tuple[list[str], list[str]]:
    from test_w2_06a_envelope import ItemLLM, _engine
    from test_window_write import _send, _turns

    llm = ItemLLM()
    if coin:
        llm.mutations[0] = lambda item: {**item, "kind": "hobby"}
    eng = await _engine(
        monkeypatch, llm, path, prepass="rules", learned_persist=persist
    )
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        await eng.flush_windows()
        env = eng._windows._env  # type: ignore[union-attr]
        return list(env.compiler.kinds.kinds), eng._windows._prepass.known_aliases()  # type: ignore[union-attr]
    finally:
        await eng.stop()


async def test_learned_kinds_and_aliases_survive_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    path = tmp_path / "m.db"
    kinds1, aliases1 = await _learn_run(monkeypatch, path, True, coin=True)
    assert "hobby" in kinds1 and aliases1
    # restart: this run coins nothing, so anything it knows came from the log
    kinds2, aliases2 = await _learn_run(monkeypatch, path, True, coin=False)
    assert "hobby" in kinds2
    assert set(aliases1) <= set(aliases2)


async def test_flag_off_forgets_on_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    path = tmp_path / "m.db"
    await _learn_run(monkeypatch, path, False, coin=True)
    kinds2, _ = await _learn_run(monkeypatch, path, False, coin=False)
    assert "hobby" not in kinds2


def test_kind_registry_restore_respects_the_cap() -> None:
    from memspine.derive.envelope_compiler import KindRegistry

    reg = KindRegistry(cap=3, known=("a",))  # "other" is always there
    assert reg.restore(["b", "c", "d"]) == 1
    assert "d" not in reg.kinds and reg.drain_new() == []
    assert reg("x") == "other" and reg.drain_new() == []


# --- 5. the compiled envelope is stored and read back ---------------------------------------
async def test_get_envelope_reads_back_the_compiled_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_w2_06a_envelope import ItemLLM, _engine
    from test_window_write import _send, _turns

    from memspine.schemas.durable import AssertionEnvelope

    eng = await _engine(monkeypatch, ItemLLM())
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        await eng.flush_windows()
        facts = await _facts(eng)
        assert facts
        got = [await eng.get_envelope(r.record_id, "a") for r in facts]
        assert all(isinstance(e, AssertionEnvelope) for e in got)
        env = got[0]
        assert env is not None and env.evidence and env.time.source_time is not None
        assert await eng.get_envelope("missing", "a") is None
        turn = next(r for r in await eng.list_records("a", "episodic"))
        assert await eng.get_envelope(turn.record_id, "a") is None  # a raw turn has no envelope
    finally:
        await eng.stop()


async def test_envelope_survives_a_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from test_w2_06a_envelope import ItemLLM, _engine
    from test_window_write import _send, _turns

    path = tmp_path / "e.db"
    eng = await _engine(monkeypatch, ItemLLM(), path)
    try:
        await _send(eng, "a", "s1", _turns("trip", 2))
        await eng.flush_windows()
        rid = (await _facts(eng))[0].record_id
        before = await eng.get_envelope(rid, "a")
    finally:
        await eng.stop()
    eng2 = await _engine(monkeypatch, ItemLLM(), path)
    try:
        after = await eng2.get_envelope(rid, "a")
        assert before is not None and after == before
    finally:
        await eng2.stop()


def test_fingerprint_without_an_envelope_dumps_as_before() -> None:
    from memspine.store.records import RecordFingerprint

    assert "envelope" not in RecordFingerprint().model_dump(mode="json")
    assert RecordFingerprint(envelope={"a": 1}).model_dump(mode="json")["envelope"] == {"a": 1}


def test_erasure_scrubs_the_logged_envelope() -> None:
    from memspine.trust.erasure import redact_record

    snap = {
        "record_id": "r1",
        "namespace": "a",
        "content": "Ilse lives in Oslo",
        "fingerprint": {"content": "", "envelope": {"text": "Ilse lives in Oslo"}},
    }
    assert redact_record({"record": snap}, "r1")
    assert "envelope" not in snap["fingerprint"] and snap["content"] == ""


# --- 6. Neo4j walks are single variable-length queries --------------------------------------
async def _graph() -> tuple[Any, Any]:
    from test_f_16e_neo4j_outbox import NS, NS2, FakeNeo4j

    from memspine.services.graph.neo4j import Neo4jGraphStore

    fake = FakeNeo4j()
    g = Neo4jGraphStore(fake)
    for src, dst, w, ns in [
        ("a", "b", 1.0, NS), ("b", "c", 0.8, NS), ("c", "d", 1.0, NS), ("a", "x", 0.0, NS),
        ("a", "y", 1.0, NS2), ("d", "a", 0.5, NS), ("e", "f", 1.0, NS),
    ]:
        await g.upsert_edge(src, dst, "related", {"weight": w}, namespace=ns)
    await g.upsert_edge("b", "z", "likes", {"weight": 1.0}, namespace=NS)
    await g.upsert_node("c", ["semantic"], {"namespace": NS}, namespace=NS)
    fake.calls.clear()
    return g, fake


async def test_neighbors_is_one_query_and_matches_the_stepwise_walk() -> None:
    from test_f_16e_neo4j_outbox import NS

    g, fake = await _graph()
    for depth in (1, 2, 3, 5):
        for ns in (None, NS):
            for rel in (None, "related"):
                fake.calls.clear()
                got = await g.neighbors("a", rel, depth, namespace=ns)
                assert fake.calls == ["walk_nodes"], fake.calls  # one round trip, whatever the depth
                dist, _ = await g._walk(["a"], depth, ns, None, rel)  # the per-hop loop it replaces
                want = sorted((d, n) for n, d in dist.items() if n != "a")
                assert [n.node_id for n in got] == [n for _, n in want], (depth, ns, rel)
    assert [n.node_id for n in await g.neighbors("a", depth=0)] == []
    assert [n.node_id for n in await g.neighbors("zzz", depth=3)] == []


async def test_subgraph_is_one_query_and_matches_the_stepwise_walk() -> None:
    from test_f_16e_neo4j_outbox import NS

    g, fake = await _graph()
    for depth in (1, 2, 4):
        for ns in (None, NS):
            for seeds in (["a"], ["a", "e"], ["c", "nope"]):
                fake.calls.clear()
                got = await g.subgraph(seeds, depth, namespace=ns)
                assert fake.calls == ["walk_edges"], fake.calls
                dist, _ = await g._walk(seeds, depth, ns, None)
                rows = await g._run("edges_of", ids=list(dist))
                keep = [g._edge(r) for r in rows if ns is None or r.get("ns") == ns]
                want = sorted(
                    (e for e in keep if e.weight > 0 and e.src in dist and e.dst in dist),
                    key=lambda e: (e.src, e.dst, e.rel_type),
                )
                assert [(e.src, e.dst, e.rel_type) for e in got] == [
                    (e.src, e.dst, e.rel_type) for e in want
                ], (depth, ns, seeds)


async def test_a_degree_cap_keeps_the_stepwise_walk() -> None:
    g, fake = await _graph()
    got = await g.neighbors("a", depth=2, max_degree=1)
    assert "walk_nodes" not in fake.calls and "edges_of" in fake.calls
    assert [n.node_id for n in got] == ["b"]  # cap 1: b (tie on weight, lower id), then back to a


async def test_the_walk_cypher_is_variable_length_and_depth_is_validated() -> None:
    from memspine.services.graph.neo4j import CYPHER, MAX_WALK_DEPTH, DriverExecutor

    assert "*1..{D}" in CYPHER["walk_nodes"] and "*1..{D}" in CYPHER["walk_edges"]
    texts: list[str] = []

    class Result:
        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

    class Tx:
        async def run(self, text: str, params: Any) -> Any:
            texts.append(text)
            return Result()

    class Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def execute_write(self, work: Any) -> list[Any]:
            return await work(Tx())

    class Driver:
        def session(self, **_: Any) -> Any:
            return Session()

    ex = DriverExecutor(Driver())
    await ex.run("walk_nodes", {"seeds": ["a"], "depth": 3, "ns": None, "rel": None})
    await ex.run("walk_nodes", {"seeds": ["a"], "depth": 10**6, "ns": None, "rel": None})
    assert "*1..3]" in texts[0] and f"*1..{MAX_WALK_DEPTH}]" in texts[1]


@pytest.mark.skipif(
    not os.environ.get("MEMSPINE_TEST_NEO4J_URL"),
    reason="needs a live server: MEMSPINE_TEST_NEO4J_URL",
)
async def test_live_walks_match_the_stepwise_walk() -> None:
    import uuid

    from memspine.services.graph.neo4j import Neo4jGraphStore, executor_from_env

    ex = executor_from_env(
        url_env="MEMSPINE_TEST_NEO4J_URL",
        user_env="MEMSPINE_TEST_NEO4J_USER",
        password_env="MEMSPINE_TEST_NEO4J_PASSWORD",
        node_label=f"MemFu{uuid.uuid4().hex[:10]}",
        rel_type=f"MemFuRel{uuid.uuid4().hex[:10]}",
    )
    g = Neo4jGraphStore(ex)
    try:
        edges = [("a", "b", 1.0), ("b", "c", 0.8), ("c", "d", 1.0), ("a", "x", 0.0), ("d", "a", 0.5)]
        for src, dst, w in edges:
            await g.upsert_edge(src, dst, "related", {"weight": w}, namespace="ns1")
        await g.upsert_edge("a", "y", "related", {"weight": 1.0}, namespace="ns2")
        for depth in (1, 2, 4):
            got = await g.neighbors("a", None, depth, namespace="ns1")
            dist, _ = await g._walk(["a"], depth, "ns1", None, None)
            want = [n for _, n in sorted((d, n) for n, d in dist.items() if n != "a")]
            assert [n.node_id for n in got] == want
            sub = await g.subgraph(["a"], depth, namespace="ns1")
            assert sub and all(e.weight > 0 for e in sub)
    finally:
        await ex.run("clear", {})
        await ex.close()


# --- 7. lease expiry by the database clock on Postgres ----------------------------------------
class _Row:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _PgStub:
    """Stands in for a psycopg connection: answers the database clock and nothing else."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.sql: list[str] = []

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.sql.append(sql)
        assert "clock_timestamp" in sql
        return _Row((self.now,))


def test_postgres_store_judges_lease_time_by_the_database_clock() -> None:
    from memspine.derive.job import LeaseStore

    store = LeaseStore.__new__(LeaseStore)  # no connection: only the clock choice is under test
    store.clock = lambda: 1_000.0  # this host's (skewed) clock
    store._pg = True
    store._db_time = True
    con = _PgStub(5_000.0)  # the database says it is much later
    assert store._lease_now(con) == 5_000.0 and con.sql


def test_sqlite_and_injected_clocks_are_unchanged() -> None:
    from memspine.derive.job import LeaseStore

    sq = LeaseStore(":memory:", clock=lambda: 42.0)
    assert sq._db_time is False and sq._lease_now(None) == 42.0
    assert sq.lease_state("nobody") == (None, None)  # SQLite: the caller's clock applies
    pg = LeaseStore.__new__(LeaseStore)
    pg.clock, pg._pg = (lambda: 7.0), True
    pg._db_time = False  # a PG store with an injected clock (the existing tests) keeps it
    assert pg._lease_now(None) == 7.0


def test_default_postgres_store_uses_the_database_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    import memspine.derive.job as job

    monkeypatch.setattr(job.LeaseStore, "_conn", lambda self: _NoConn())
    default = job.LeaseStore("postgresql://u@h/db")
    injected = job.LeaseStore("postgresql://u@h/db", clock=lambda: 1.0)
    assert default._db_time is True and injected._db_time is False


class _NoConn:
    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def executescript(self, _: str) -> None:
        return None


@pytest.mark.skipif(
    not os.environ.get("MEMSPINE_TEST_PG_URL"), reason="Postgres leases: set MEMSPINE_TEST_PG_URL"
)
def test_live_pg_lease_expiry_ignores_a_skewed_process_clock() -> None:
    from memspine.derive.job import LeaseStore

    url = os.environ["MEMSPINE_TEST_PG_URL"]
    ns = f"fu-skew-{os.getpid()}"
    fast = LeaseStore(url)  # default: the database clock
    slow = LeaseStore(url)
    fast.clock = lambda: 10**10  # a host whose clock is far ahead must not matter
    slow.clock = lambda: 0.0
    lease = fast.claim(ns, "p1", "job-1", 60.0)
    assert lease is not None
    assert slow.claim(ns, "p2", "job-2", 60.0) is None  # still live by the database's time
    assert fast.release(lease)
