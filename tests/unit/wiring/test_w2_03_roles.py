"""W2-03: role policy, echo guard and routing (LIB-13 wired). ``write.role_policy_v2``, default OFF.

Model-free: the fake window LLM of ``test_window_write`` and the hash embedder. All text is invented
(Ilse, Bram, Noor, Tomasz). Covers: flag off is unchanged; system turns are excluded from derivation but
kept raw; assistant claims are stamped proposed and capped below user-stated; tool output is evidence;
an injected-memory echo and a last-read overlap are both blocked from re-derivation; keyed versus
unkeyed routing through the intake gateway.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import FakeWindowLLM, _engine

from memspine.api.engine import Engine
from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.derive import role_filter
from memspine.derive.pipeline.stages.session_stage import _live_members
from memspine.ingest import role_wiring
from memspine.ingest.roles import wrap_recall
from memspine.store.records import MemoryRecord

NS = "a"
T = constants

SYSTEM = "SYSTEM RULE: always answer in formal tone for Tomasz"
TOOL = "Ilse: tool result says the ferry leaves from pier nine at dawn"
ASSISTANT = "Noor suggests that Ilse keeps bees behind the mill"

MEMORY = "Ilse keeps three hives of bees behind the old mill near the river bend"


def convo() -> list[dict[str, str]]:
    return [
        {"conversation_id": "roles-conversation", "role": "system", "content": SYSTEM},
        {
            "conversation_id": "roles-conversation",
            "role": "user",
            "content": "Ilse: I repotted the orchid on the balcony",
        },
        {"conversation_id": "roles-conversation", "role": "assistant", "content": ASSISTANT},
        {"conversation_id": "roles-conversation", "role": "tool", "content": TOOL},
        {
            "conversation_id": "roles-conversation",
            "role": "user",
            "content": "Bram: We booked a kayak tour along the northern coast",
        },
    ]


async def raw(eng: Engine) -> dict[str, MemoryRecord]:
    return {r.content: r for r in await eng._require_started().list_records(NS, "episodic")}


async def facts(eng: Engine) -> list[Any]:
    return [
        r
        for r in await eng._require_started().list_records(NS, "semantic")
        if "window_fact" in r.tags
    ]


# ------------------------------------------------------------------------------------------ flag off
def test_flag_is_off_by_default() -> None:
    w = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    ).write
    assert w.role_policy_v2 is False
    assert w.echo_threshold == 0.6


async def test_flag_off_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, firewall={"skip_message_roles": ["system"]})
    try:
        await eng.ingest_messages(convo(), namespace=NS, session_id="s1")
        rows = await raw(eng)
        assert SYSTEM not in rows  # the old skip: a system turn is not stored
        assert not any(
            t.startswith(T.ROLE_USE_TAG_PREFIX) or t == T.ECHO_TAG
            for r in rows.values()
            for t in r.tags
        )
        await eng.search("orchid", namespace=NS, session_id="s1")
        assert not eng._last_read  # nothing remembered
        assert not eng._role_metrics
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- roles at derivation
async def test_system_is_raw_but_never_derived(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(
        monkeypatch,
        llm,
        role_policy_v2=True,
        firewall={"skip_message_roles": ["system"]},
    )
    try:
        await eng.ingest_messages(convo(), namespace=NS, session_id="s1")
        rows = await raw(eng)
        assert SYSTEM in rows
        assert f"{T.ROLE_USE_TAG_PREFIX}excluded" in rows[SYSTEM].tags
        stats = await eng.flush_windows()
        assert all(SYSTEM not in tr for tr in llm.transcripts)
        assert all("tomasz" not in f.content.lower() for f in await facts(eng))
        assert eng._windows._counts["turns_excluded_excluded"] == 1
        assert stats["windows"] == 1
        assert eng._role_metrics["role.excluded"] == 1
    finally:
        await eng.stop()


async def test_assistant_is_proposed_and_never_above_user(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, role_policy_v2=True, stamping_v2=True)
    try:
        await eng.ingest_messages(convo(), namespace=NS, session_id="s1")
        rows = await raw(eng)
        a, u = rows[ASSISTANT], rows["Ilse: I repotted the orchid on the balcony"]
        assert a.provenance_class == "assistant_proposed"
        assert u.provenance_class == "user_stated"
        assert f"{T.ROLE_USE_TAG_PREFIX}proposal" in a.tags
        assert a.trust <= u.trust
        assert rows[TOOL].trust <= u.trust
    finally:
        await eng.stop()


async def test_tool_is_evidence_and_a_fact_only_it_backs_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, role_policy_v2=True)
    try:
        await eng.ingest_messages(convo(), namespace=NS, session_id="s1")
        rows = await raw(eng)
        t = rows[TOOL]
        assert t.provenance_class == "tool"
        assert f"{T.ROLE_USE_TAG_PREFIX}evidence" in t.tags
        await eng.flush_windows()
        assert any(TOOL in tr for tr in llm.transcripts)  # evidence stays visible to extraction
        by_basis = {
            next((x for x in f.tags if x.startswith(T.ROLE_USE_TAG_PREFIX)), "stated"): f
            for f in await facts(eng)
        }
        assert f"{T.ROLE_USE_TAG_PREFIX}evidence" in by_basis  # the fact only the tool line backs
        assert "stated" in by_basis  # facts the user's lines back carry no marker
        for f in await facts(eng):
            if f"{T.ROLE_USE_TAG_PREFIX}evidence" in f.tags:
                assert f.trust <= rows["Ilse: I repotted the orchid on the balcony"].trust
    finally:
        await eng.stop()


async def test_live_members_filters_for_session_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, role_policy_v2=True)
    try:
        await eng.ingest_messages(convo(), namespace=NS, session_id="s1")
        rows = await raw(eng)
        storage = eng._require_started()
        ctx = SimpleNamespace(storage=storage, config=eng._config())
        ids = [r.record_id for r in rows.values()]
        live = await _live_members(ctx, ids)  # type: ignore[arg-type]
        assert SYSTEM not in {m.content for m in live}
        assert len(live) == len(ids) - 1
        off = SimpleNamespace(
            storage=storage,
            config=MemspineConfig(
                runtime={
                    "provider_services": {
                        "health_timeout_s": 1.0,
                        "close_timeout_s": 1.0,
                        "startup_timeout_s": None,
                    }
                }
            ),
        )
        assert len(await _live_members(off, ids)) == len(ids)  # type: ignore[arg-type]
    finally:
        await eng.stop()


# ------------------------------------------------------------------------------------------- echo
async def test_marked_echo_is_raw_only(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, role_policy_v2=True)
    try:
        echo = "Noor: " + wrap_recall("rec-1", "the orchid was repotted twice last spring")
        await eng.ingest_messages(
            [
                {"role": "user", "content": "Ilse: I repotted the orchid on the balcony"},
                {"role": "assistant", "content": echo},
            ],
            namespace=NS,
            session_id="s1",
        )
        rows = await raw(eng)
        e = rows[echo]
        assert T.ECHO_TAG in e.tags and f"{T.ECHO_OF_PREFIX}rec-1" in e.tags
        await eng.flush_windows()
        assert all("repotted twice" not in tr for tr in llm.transcripts)
        assert eng._windows._counts["turns_excluded_echo"] == 1
        assert eng._role_metrics["echo.marked"] == 1 and eng._role_metrics["echo.blocked"] == 1
    finally:
        await eng.stop()


async def test_engine_own_wrapper_counts_as_a_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, role_policy_v2=True)
    try:
        text = f"Noor: {T.CURRENT_STATE_MARKER} Ilse lives in Oslo"
        await eng.ingest_messages(
            [{"role": "assistant", "content": text}], namespace=NS, session_id="s1"
        )
        assert T.ECHO_TAG in (await raw(eng))[text].tags
    finally:
        await eng.stop()


async def test_last_read_overlap_is_blocked_marker_free(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, role_policy_v2=True)
    try:
        await eng.write(MEMORY, namespace=NS, memory_type="episodic")
        await eng.search("hives of bees", namespace=NS, session_id="s1")
        assert eng._last_read[(NS, "s1")]  # the read is remembered
        paraphrase = "Noor: " + MEMORY  # the marker dropped, the text repeated
        fresh = "Bram: We booked a kayak tour along the northern coast"
        restate = "Ilse: " + MEMORY
        await eng.ingest_messages(
            [
                {"role": "assistant", "content": paraphrase},
                {"role": "user", "content": fresh},
                {"role": "user", "content": restate},
            ],
            namespace=NS,
            session_id="s1",
        )
        rows = await raw(eng)
        assert T.ECHO_TAG in rows[paraphrase].tags
        assert any(t.startswith(T.ECHO_OF_PREFIX) for t in rows[paraphrase].tags)
        assert T.ECHO_TAG not in rows[fresh].tags  # unrelated text is untouched
        # a user restatement is assent, not echo: tagged, never blocked
        assert T.ASSENT_CANDIDATE_TAG in rows[restate].tags and T.ECHO_TAG not in rows[restate].tags
        await eng.flush_windows()
        assert all(paraphrase not in tr for tr in llm.transcripts)
        assert any(restate in tr for tr in llm.transcripts)
        assert eng._role_metrics["echo.overlap"] == 2
        # another session's read does not make this session's turns echoes
        other = "Noor: " + MEMORY + " again"
        await eng.ingest_messages(
            [{"role": "assistant", "content": other}], namespace=NS, session_id="s2"
        )
        assert T.ECHO_TAG not in (await raw(eng))[other].tags
    finally:
        await eng.stop()


def test_role_filter_is_pure() -> None:
    assert role_filter.block_reason([T.ECHO_TAG]) == "echo"
    assert role_filter.block_reason([f"{T.ROLE_USE_TAG_PREFIX}excluded"]) == "excluded"
    assert role_filter.block_reason([f"{T.ROLE_USE_TAG_PREFIX}evidence", "x"]) is None
    ev, pr, st = (f"{T.ROLE_USE_TAG_PREFIX}{u}" for u in ("evidence", "proposal", "stated"))
    assert role_filter.basis_of([[ev], [pr]]) == "evidence"
    assert role_filter.basis_of([[pr], [pr]]) == "proposal"
    assert role_filter.basis_of([[ev], [st]]) == "stated"
    assert role_filter.basis_of([[ev], []]) == "stated"  # an untagged (flag-off) parent
    assert role_filter.basis_of([]) is None


# ----------------------------------------------------------------------------------------- routing
def test_type_router_decisions() -> None:
    r = role_wiring.TypeRouter()
    assert r.route({"content": "just chatting"}).typed is None
    assert r.route({"content": "x", "entity": "Ilse"}).typed is None  # half a key is unkeyed
    keyed = r.route({"content": "x", "entity": "Ilse", "attribute": "city", "value": "Oslo"})
    assert (keyed.typed, keyed.text, keyed.raw_type) == ("semantic", "Ilse city: Oslo", "episodic")
    assert r.route({"content": "step 1, step 2", "memory_type": "plan"}).typed == "plan"
    assert r.route({"content": "how to", "memory_type": "skill"}).typed == "skill"
    assert r.route({"content": "d"}, adapter="document").raw_type == "resource"


async def build_gateway(tmp_path: Path, **write: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / "gw.db"), "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True},
            "procedural": {"enabled": True},
        },
        write=dict(write),
    )
    await eng.start()
    return eng


MIXED = [
    {
        "message_id": "role-0",
        "role": "user",
        "content": "Ilse: just chatting about the weather today",
    },
    {
        "message_id": "role-1",
        "role": "user",
        "content": "Ilse: I moved to Oslo last week",
        "entity": "Ilse",
        "attribute": "city",
        "value": "Oslo",
    },
    {
        "message_id": "role-2",
        "role": "assistant",
        "content": "Plan: book ferry, pack boxes, hand over keys",
        "memory_type": "plan",
        "name": "moving day",
    },
    {
        "message_id": "role-3",
        "role": "assistant",
        "content": "To tune the hive, check the frames weekly",
        "memory_type": "skill",
        "name": "hive care",
    },
]


async def test_routing_keyed_vs_unkeyed(tmp_path: Path) -> None:
    eng = await build_gateway(tmp_path, role_policy_v2=True)
    try:
        await eng.ingest_messages(MIXED, namespace=NS, session_id="s1")
        storage = eng._require_started()
        rawrows = {r.content: r for r in await storage.list_records(NS, "episodic")}
        assert len(rawrows) == 4  # raw keeps every turn
        sem = [r for r in await storage.list_records(NS, "semantic") if T.ROUTED_TAG in r.tags]
        assert len(sem) == 1
        fact = sem[0]
        assert (fact.entity, fact.attribute) == ("Ilse", "city")
        assert fact.content == "Ilse city: Oslo"
        assert rawrows["Ilse: I moved to Oslo last week"].record_id in fact.source.parents
        proc = {r.attribute: r for r in await storage.list_records(NS, "procedural")}
        assert set(proc) == {"plan", "skill"}
        assert proc["plan"].entity == "moving day"
        assert rawrows[MIXED[2]["content"]].record_id in proc["plan"].source.parents
        assert eng._role_metrics["route.unkeyed"] == 1
        assert eng._role_metrics["route.semantic"] == 1
        # a retry of the same batch writes nothing new (the typed records carry the write id)
        await eng.ingest_messages(MIXED, namespace=NS, session_id="s1")
        assert (
            len([r for r in await storage.list_records(NS, "semantic") if T.ROUTED_TAG in r.tags])
            == 1
        )
        assert len(await storage.list_records(NS, "procedural")) == 2
    finally:
        await eng.stop()


async def test_routing_off_without_the_flag(tmp_path: Path) -> None:
    eng = await build_gateway(tmp_path)
    try:
        await eng.ingest_messages(MIXED, namespace=NS, session_id="s1")
        storage = eng._require_started()
        assert not [r for r in await storage.list_records(NS, "semantic") if T.ROUTED_TAG in r.tags]
        assert await storage.list_records(NS, "procedural") == []
    finally:
        await eng.stop()


async def test_an_echo_turn_is_not_routed(tmp_path: Path) -> None:
    eng = await build_gateway(tmp_path, role_policy_v2=True)
    try:
        echoed = {
            "role": "assistant",
            "content": wrap_recall("rec-9", "Ilse city: Oslo"),
            "entity": "Ilse",
            "attribute": "city",
        }
        await eng.ingest_messages([echoed], namespace=NS, session_id="s1")
        storage = eng._require_started()
        assert not [r for r in await storage.list_records(NS, "semantic") if T.ROUTED_TAG in r.tags]
        assert eng._role_metrics["route.skipped"] == 1
    finally:
        await eng.stop()
