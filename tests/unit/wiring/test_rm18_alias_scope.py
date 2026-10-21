"""RM-18: learned pre-pass aliases are namespace-scoped, never engine-wide. Model-free stubs.

An alias the W2-13 pre-pass learns from namespace A's envelopes must not reach namespace B: not as
a KNOWN hint, not in the window skip verdict, not in the order-500 cross-check, not through the
FU-GAPS ``learned.alias`` markers (flush attribution or restart reload)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_w2_06a_envelope import ItemLLM, _engine
from test_window_write import _send, _turns

from memspine.derive.known_sources import prepass_known
from memspine.derive.learned_state import LEARNED_ALIAS_MARKER, LearnedState
from memspine.derive.prepass import Prepass
from memspine.store.events import EventKind

ALIAS = "Zelkova"  # the speaker of namespace A's turns becomes a learned subject surface
PROBE = (
    "[1] [2023-06-01] i ran into zelkova at the market"  # lower case: only the alias path sees it
)


def _probe() -> list[Any]:
    return [SimpleNamespace(record_id="p1", content="i ran into zelkova at the market")]


async def _learn_in_a(monkeypatch: pytest.MonkeyPatch, path: Any, **write: Any) -> Any:
    eng = await _engine(monkeypatch, ItemLLM(), path, prepass="rules", **write)
    await _send(eng, "a", "s1", _turns("trip", 3, speaker=ALIAS))
    await eng.flush_windows()
    return eng


# --- reproduction: engine-level leak -------------------------------------------------------------
async def test_alias_learned_in_a_is_not_a_hint_in_b(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _learn_in_a(monkeypatch, ":memory:")
    try:
        pre = eng._windows._prepass  # type: ignore[union-attr]
        same = await pre.ahints(_probe(), namespace="a")
        other = await pre.ahints(_probe(), namespace="b")
        assert [k.casefold() for k in same.known] == ["zelkova"]  # same namespace unchanged
        assert other.known == ()  # RM-18: A's alias never hints in B
        assert not any(s.label == "alias" for s in other.entities)
    finally:
        await eng.stop()


async def test_alias_learned_in_a_is_not_known_prompt_context_in_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = await _learn_in_a(monkeypatch, ":memory:")
    try:
        source = prepass_known(eng._windows._prepass)  # type: ignore[union-attr]
        assert "zelkova" in {e.name.casefold() for e in await source("a", PROBE)}
        assert "zelkova" not in {e.name.casefold() for e in await source("b", PROBE)}
    finally:
        await eng.stop()


# --- persistence: markers are namespace-bound on write and on reload ------------------------------
async def test_restart_reloads_aliases_into_their_own_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    path = tmp_path / "m.db"
    eng = await _learn_in_a(monkeypatch, path, learned_persist=True)
    try:
        markers = [
            e
            for e in await eng._storage.read_events(after_seq=0, limit=10_000)  # type: ignore[union-attr]
            if e.kind is EventKind.MARKER and e.payload.get("marker") == LEARNED_ALIAS_MARKER
        ]
        assert markers and {e.namespace for e in markers} == {"a"}
    finally:
        await eng.stop()
    eng = await _engine(monkeypatch, ItemLLM(), path, prepass="rules", learned_persist=True)
    try:
        pre = eng._windows._prepass  # type: ignore[union-attr]
        await eng._windows._env.learned.load()  # type: ignore[union-attr]
        assert ALIAS in pre.known_aliases("a")
        assert pre.known_aliases("b") == []
        assert (await pre.ahints(_probe(), namespace="b")).known == ()
    finally:
        await eng.stop()


async def test_erase_namespace_drops_its_learned_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    path = tmp_path / "m.db"
    eng = await _learn_in_a(monkeypatch, path, learned_persist=True)
    try:
        await _send(eng, "c", "s1", _turns("trip", 3, speaker="Quillon"))
        await eng.flush_windows()
        pre = eng._windows._prepass  # type: ignore[union-attr]
        assert ALIAS in pre.known_aliases("a")
        await eng.erase_namespace("a")
        assert pre.known_aliases("a") == []
        assert "Quillon" in pre.known_aliases("c")  # another namespace is untouched
    finally:
        await eng.stop()
    eng = await _engine(monkeypatch, ItemLLM(), path, prepass="rules", learned_persist=True)
    try:
        pre = eng._windows._prepass  # type: ignore[union-attr]
        await eng._windows._env.learned.load()  # type: ignore[union-attr]
        assert pre.known_aliases("a") == []  # the reset marker survives the restart
        assert "Quillon" in pre.known_aliases("c")
    finally:
        await eng.stop()


# --- unit level ----------------------------------------------------------------------------------
async def test_prepass_learn_restore_and_drain_are_per_namespace() -> None:
    pre = Prepass("rules")
    pre.learn(["Zelkova"], namespace="a")
    pre.learn(["Quillon"], namespace="b")
    assert pre.known_aliases("a") == ["Zelkova"] and pre.known_aliases("b") == ["Quillon"]
    assert pre.drain_new("a") == ["Zelkova"] and pre.drain_new("a") == []
    assert pre.drain_new("b") == ["Quillon"]
    assert pre.restore(["Orrin", "Zelkova"], namespace="a") == 1
    assert pre.known_aliases("a") == ["Zelkova", "Orrin"] and pre.drain_new("a") == []
    assert pre.known_aliases("z") == []
    pre.forget("a")
    assert pre.known_aliases("a") == [] and pre.known_aliases("b") == ["Quillon"]


async def test_extra_alias_source_is_asked_per_namespace() -> None:
    asked: list[str] = []

    def extra(ns: str) -> list[str]:
        asked.append(ns)
        return ["Orrin"] if ns == "a" else []

    pre = Prepass("rules", aliases=extra)
    assert pre.known_aliases("a") == ["Orrin"] and pre.known_aliases("b") == []
    assert asked == ["a", "b"]


class _Log:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append_event(self, event: Any) -> None:
        self.events.append(event.model_copy(update={"seq": len(self.events) + 1}))

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[Any]:
        return [e for e in self.events if e.seq > after_seq][:limit]


async def test_flush_attributes_only_the_flushing_namespace() -> None:
    log = _Log()
    pre = Prepass("rules")
    state = LearnedState(lambda: log, prepass=pre)
    pre.learn(["Zelkova"], namespace="a")
    pre.learn(["Quillon"], namespace="b")
    assert await state.flush("b") == 1
    assert [(e.namespace, e.payload["names"]) for e in log.events] == [("b", ["Quillon"])]
    assert await state.flush("a") == 1
    assert (log.events[-1].namespace, log.events[-1].payload["names"]) == ("a", ["Zelkova"])

    # replay into a fresh pre-pass equals the live per-namespace state
    fresh = Prepass("rules")
    await LearnedState(lambda: log, prepass=fresh).load()
    for ns in ("a", "b", "c"):
        assert fresh.known_aliases(ns) == pre.known_aliases(ns)
