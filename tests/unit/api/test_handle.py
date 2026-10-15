"""F-13a: the scoped handle ``Memory`` and ``memspine.open()`` (G-16 D, S3-1, ER-29)."""

from __future__ import annotations

from typing import Any

import pytest

import memspine
from memspine.api.engine import Engine
from memspine.api.handle import Memory
from memspine.read.tiers import ReadRequestError
from memspine.schemas.durable import OwnerScope


class _Recorder:
    """Stands in for an Engine: records the verb and its arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, verb: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> str:
            self.calls.append((verb, args, kwargs))
            return verb

        return call


USER = OwnerScope(tenant="acme", owner="alice")
GROUP = OwnerScope(tenant="acme", owner="team", owner_kind="group")


def test_scope_binds_namespace_and_context() -> None:
    mem = Memory(_Recorder(), USER, principal="alice", purpose="support", viewer="alice")  # type: ignore[arg-type]
    assert mem.namespace == "acme/alice"
    assert (mem.principal, mem.purpose, mem.viewer) == ("alice", "support", "alice")
    assert Memory(_Recorder(), "team/a").namespace == "team/a"  # type: ignore[arg-type]


def test_group_scope_requires_viewer() -> None:
    with pytest.raises(ReadRequestError, match="multi-party"):
        Memory(_Recorder(), GROUP, principal="alice")  # type: ignore[arg-type]
    with pytest.raises(ReadRequestError, match="multi-party"):
        Memory(_Recorder(), USER, principal="alice", multi_party=True)  # type: ignore[arg-type]
    mem = Memory(_Recorder(), GROUP, principal="alice", viewer="alice")  # type: ignore[arg-type]
    assert mem.multi_party and mem.viewer == "alice"


def test_viewer_must_be_the_principal() -> None:
    with pytest.raises(ReadRequestError, match="principal"):
        Memory(_Recorder(), USER, viewer="alice")  # type: ignore[arg-type]
    with pytest.raises(ReadRequestError, match="must be the authenticated principal"):
        Memory(_Recorder(), USER, principal="alice", viewer="bob")  # type: ignore[arg-type]


async def test_verbs_delegate_with_scope_filled_in() -> None:
    rec = _Recorder()
    mem = Memory(rec, USER, principal="alice", purpose="support", viewer="alice")  # type: ignore[arg-type]
    await mem.remember("likes tea", tags=["a"])
    await mem.recall("tea", top_k=3)
    await mem.read("tea")
    await mem.compose("tea", 500)
    await mem.correct("rid", "coffee")
    await mem.forget("rid", hard=True)
    await mem.sleep()
    by_verb = {verb: (args, kw) for verb, args, kw in rec.calls}
    assert by_verb["write"] == (("likes tea",), {"namespace": "acme/alice", "tags": ["a"]})
    for verb in ("search", "read", "assemble"):
        kw = by_verb[verb][1]
        assert (kw["namespace"], kw["viewer"], kw["purpose"]) == ("acme/alice", "alice", "support")
    assert by_verb["assemble"][1]["budget_tokens"] == 500
    assert by_verb["search"][1]["top_k"] == 3
    assert by_verb["correct"] == (("rid", "coffee"), {"namespace": "acme/alice"})
    assert by_verb["forget"] == (("rid",), {"namespace": "acme/alice", "hard": True})
    assert by_verb["sleep"] == ((), {})


async def test_caller_cannot_override_bound_fields() -> None:
    mem = Memory(_Recorder(), USER, principal="alice", viewer="alice")  # type: ignore[arg-type]
    for call in (
        mem.remember("x", namespace="other"),
        mem.recall("x", viewer="bob"),
        mem.read("x", namespace="other"),
        mem.compose("x", purpose="p"),
        mem.forget("rid", namespace="other"),
    ):
        with pytest.raises(TypeError, match="fixed by the handle"):
            await call


def test_engine_scope_returns_handle() -> None:
    engine = Engine(template="core", dotenv_path=None)
    mem = engine.scope(GROUP, principal="alice", viewer="alice")
    assert isinstance(mem, Memory) and mem.namespace == "acme/team"
    with pytest.raises(ReadRequestError):
        engine.scope(GROUP, principal="alice")


async def test_open_lifecycle_and_round_trip() -> None:
    engine = await memspine.open(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )
    try:
        assert isinstance(engine, Engine) and engine.is_started
        mem = engine.scope("acme/alice", principal="alice")
        record = await mem.remember("Alice drinks green tea every morning")
        hits = await mem.recall("what tea does Alice drink")
        assert record.record_id in {r.record_id for r, _ in hits}
        assert all(r.namespace == "acme/alice" for r, _ in hits)
    finally:
        await engine.stop()
    assert not engine.is_started


async def test_engine_is_an_async_context_manager() -> None:
    async with Engine(template="core", dotenv_path=None, storage={"path": ":memory:"}) as engine:
        assert engine.is_started
    assert not engine.is_started


def test_public_surface() -> None:
    assert set(memspine.__all__) == {"Engine", "Memory", "__version__", "open"}
