"""``read.lexical_strip_names`` (B5) and ``read.session_leg`` (B8): opt-in, never a gate."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.engine import search_forensics


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def _fill_names(eng: Engine) -> None:
    for text in [
        "Caroline: I went kayaking on the lake",
        "Melanie: The bus was late again",
        "Caroline: Pottery class today",
    ]:
        await eng.write(text, namespace="a", memory_type="episodic")


def _spy_lexical(eng: Engine) -> list[str]:
    seen: list[str] = []
    orig = eng._lexical_leg

    async def spy(ns: str, text: str, fetch_k: int) -> Any:
        seen.append(text)
        return await orig(ns, text, fetch_k)

    eng._lexical_leg = spy  # type: ignore[method-assign]
    return seen


async def test_names_stripped_from_bm25_query_only() -> None:
    eng = _engine(lexical_strip_names=True)
    await eng.start()
    try:
        await _fill_names(eng)
        seen = _spy_lexical(eng)
        await eng.search("What did Caroline's friend Melanie do kayaking?", "a", top_k=3)
        assert seen == ["What did friend do kayaking?"]
    finally:
        await eng.stop()


async def test_names_kept_when_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fill_names(eng)
        seen = _spy_lexical(eng)
        await eng.search("Caroline kayaking", "a", top_k=3)
        assert seen == ["Caroline kayaking"]
    finally:
        await eng.stop()


async def test_strip_falls_back_when_nothing_left() -> None:
    eng = _engine(lexical_strip_names=True)
    await eng.start()
    try:
        await _fill_names(eng)
        seen = _spy_lexical(eng)
        await eng.search("Caroline Melanie", "a", top_k=3)
        assert seen == ["Caroline Melanie"]
    finally:
        await eng.stop()


async def test_names_refresh_when_records_added() -> None:
    eng = _engine(lexical_strip_names=True)
    await eng.start()
    try:
        await _fill_names(eng)
        assert await eng._strip_speaker_names("a", "Jon likes tea") == "Jon likes tea"
        await eng.write("Jon: tea is great", namespace="a", memory_type="episodic")
        assert await eng._strip_speaker_names("a", "Jon likes tea") == "likes tea"
    finally:
        await eng.stop()


async def _fill_sessions(eng: Engine) -> None:
    for gid, texts in {
        "s1": ["zebra stripes savanna", "zebra herd migration", "zebra foal born", "zebra"],
        "s2": ["quantum chip fabrication", "bus schedule", "tax forms"],
        "s3": ["pottery wheel", "clay glaze"],
    }.items():
        for t in texts:
            await eng.write(t, namespace="a", memory_type="episodic", group_id=gid)


async def test_session_leg_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fill_sessions(eng)
        with search_forensics() as stages:
            await eng.search("zebra", namespace="a", top_k=3)
        assert "session" not in dict(stages.get("extra_legs", []))
    finally:
        await eng.stop()


async def test_session_leg_ranks_relevant_session_and_caps_records() -> None:
    eng = _engine(session_leg=True, session_leg_top_sessions=1, session_leg_per_session=2)
    await eng.start()
    try:
        await _fill_sessions(eng)
        with search_forensics() as stages:
            await eng.search("zebra herd", namespace="a", top_k=3)
        leg = dict(stages["extra_legs"])["session"]
        assert len(leg) == 2
        storage = eng._require_started()
        for hit in leg:
            record = await storage.get_record(hit[0])
            assert record.group_id == "s1"
    finally:
        await eng.stop()


async def test_session_leg_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(session_leg=True)
    await eng.start()
    try:
        await _fill_sessions(eng)

        async def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(eng._embedder, "embed", boom)
        assert await eng._session_leg("a", [1.0] * 8, 10) == []
    finally:
        await eng.stop()
