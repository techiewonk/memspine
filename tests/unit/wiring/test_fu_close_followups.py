"""FU-CLOSE: session_close records skip the semantic dedup door; the profile card leaves with a
forgotten supporting turn at once (new marker; older marker text redacted on a hard forget)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_ow_a_close_call2_profile import FakeCloseLLM, _derived, _markers, _send, _turns

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive.pipeline.stages import profile_card
from memspine.models.providers.llm.base import LLMRouter
from memspine.store import lineage as lineage_store


def _bytes(path: str) -> bytes:
    return Path(path).read_bytes()


ENTRY = "prefers zanzibar blend tea without sugar"


async def _engine(
    monkeypatch: pytest.MonkeyPatch, llm: FakeCloseLLM, path: str = ":memory:", **offline: Any
) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"session_close": llm})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": path, "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        offline=offline,
        write={"lineage_table": True},
        llm={
            "roles": {
                "session_close": {
                    "provider": "litellm",
                    "settings": {"model": "fake/close", "timeout_seconds": 60.0},
                }
            }
        },
    )
    await eng.start()
    return eng


async def test_extension_is_a_new_version_with_extends_tag_and_nothing_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = "Mira discussed her orchid, a kayak tour and a recital."
    addition = " She then booked a glassblowing class for Saturday."
    llm = FakeCloseLLM()
    llm.episode_texts = [stub, stub + addition]
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        await eng.close_session("a", "s1")
        first = next(r for r in await _derived(eng) if "kind:episode" in r.tags)
        await _send(eng, "s1", _turns(1, start=2))
        hook = (await eng.close_session("a", "s1"))["hook"]
        assert hook["stitched"] == 1 and hook["not_written"] == 0
        episodes = [r for r in await _derived(eng) if "kind:episode" in r.tags]
        assert len(episodes) == 2
        ext = next(r for r in episodes if r.record_id != first.record_id)
        assert f"extends:{first.record_id}" in ext.tags
        assert first.record_id in ext.source.parents
        assert ext.content.endswith(addition.strip())  # the addition survives
        storage = eng._require_started()
        edges = await lineage_store.parents_of(storage._client.engine, [ext.record_id])
        assert {e.parent: e.role for e in edges[ext.record_id]}[first.record_id] == "version_of"
    finally:
        await eng.stop()


async def test_the_idem_tag_makes_a_repeat_deposit_write_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeCloseLLM()
    eng = await _engine(monkeypatch, llm, session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(2))
        await eng.close_session("a", "s1")
        before = len(await _derived(eng))
        rec = (await _derived(eng))[0]
        idem = next(t for t in rec.tags if t.startswith("sc:"))[3:]
        again = await eng._deposit_session_record(
            "a",
            "summary",
            rec.content,
            list(rec.source.parents),
            datetime(2023, 5, 8, tzinfo=UTC),
            "s1",
            idem_key=idem,
        )
        assert again is None and len(await _derived(eng)) == before
    finally:
        await eng.stop()


async def _card_entries(eng: Engine) -> tuple[Any, ...]:
    return (await profile_card.load_card(eng._pipeline_ctx(), "a")).entries


async def _closed(eng: Engine) -> list[str]:
    await _send(eng, "s1", _turns(4))
    await eng.close_session("a", "s1")
    card = await profile_card.load_card(eng._pipeline_ctx(), "a")
    entry = card.get("preferences", "tea")
    assert entry is not None and entry.value == ENTRY
    return list(entry.support_ids)


def _llm() -> FakeCloseLLM:
    llm = FakeCloseLLM()
    llm.ops = [
        {
            "op": "add",
            "entry_type": "attribute",
            "key": "tea",
            "value": ENTRY,
            "stance": "stated",
            "cites": [4],
        }
    ]
    return llm


@pytest.mark.parametrize("hard", [False, True])
async def test_forgetting_the_supporting_turn_drops_the_entry_at_once(
    monkeypatch: pytest.MonkeyPatch, hard: bool
) -> None:
    eng = await _engine(monkeypatch, _llm(), session_close_v2=True, profile_card_v2=True)
    try:
        support = await _closed(eng)
        before = len(await _markers(eng, "profile_card_v2"))
        await eng.forget(support[0], namespace="a", hard=hard)  # no sleep in between
        assert await _card_entries(eng) == ()
        assert len(await _markers(eng, "profile_card_v2")) == before + 1
    finally:
        await eng.stop()


async def test_forgetting_an_unrelated_turn_leaves_the_card_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = await _engine(monkeypatch, _llm(), session_close_v2=True, profile_card_v2=True)
    try:
        support = await _closed(eng)
        other = next(
            r.record_id
            for r in await eng._require_started().list_records("a", "episodic")
            if r.record_id not in support
        )
        before = len(await _markers(eng, "profile_card_v2"))
        await eng.forget(other, namespace="a", hard=True)
        assert len(await _card_entries(eng)) == 1
        assert len(await _markers(eng, "profile_card_v2")) == before
    finally:
        await eng.stop()


async def test_flag_off_forget_appends_no_card_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(monkeypatch, _llm(), session_close_v2=True)
    try:
        await _send(eng, "s1", _turns(4))
        await eng.close_session("a", "s1")
        rid = (await eng._require_started().list_records("a", "episodic"))[0].record_id
        await eng.forget(rid, namespace="a", hard=True)
        assert await _markers(eng, "profile_card_v2") == []
    finally:
        await eng.stop()


async def test_the_entry_text_is_on_disk_before_the_forget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sanity for the byte-scan test below: the text is findable in the file before erasure."""
    db = str(tmp_path / "card.db")
    eng = await _engine(monkeypatch, _llm(), path=db, session_close_v2=True, profile_card_v2=True)
    try:
        await _closed(eng)
        await eng._require_started()._client.checkpoint()
        assert ENTRY.encode() in _bytes(db)
    finally:
        await eng.stop()


async def test_hard_forget_leaves_no_entry_text_in_the_sqlite_file_or_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = str(tmp_path / "card.db")
    eng = await _engine(monkeypatch, _llm(), path=db, session_close_v2=True, profile_card_v2=True)
    needle = ENTRY.encode()
    try:
        support = await _closed(eng)
        await eng.forget(support[0], namespace="a", hard=True)
        assert await _card_entries(eng) == ()
        events = await eng._require_started().read_events(after_seq=0, limit=10000)
        assert not any(ENTRY in str(e.payload) for e in events if e.payload)
        # engine still open: the WAL may hold pages; the forget checkpoints it
        for name in ("card.db", "card.db-wal"):
            f = tmp_path / name
            if f.exists():
                assert needle not in f.read_bytes(), name
    finally:
        await eng.stop()
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT payload FROM memory_events").fetchall()
    finally:
        con.close()
    assert not any(needle in bytes(r[0]) for r in rows)
    for name in ("card.db", "card.db-wal"):
        f = tmp_path / name
        if f.exists():
            assert needle not in f.read_bytes(), name
