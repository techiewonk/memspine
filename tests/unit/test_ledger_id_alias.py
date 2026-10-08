"""I9: ``ledger_id`` names the B0 read-ledger key (``session_id`` stays as its alias).

On ``read`` / ``assemble`` / ``search`` / ``write`` / ``send`` / ``shared_search`` the
``session_id`` argument is not a session filter: it keys the ledger of what was shown,
whose records become implicit parents of the next write. ``ledger_id`` says so."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine


async def _engine() -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"record_access": False},
    )
    await eng.start()
    await eng.write("Ana went camping by the lake", namespace="a")
    return eng


@pytest.mark.parametrize("keyword", ["ledger_id", "session_id"])
async def test_both_names_key_the_same_ledger(
    monkeypatch: pytest.MonkeyPatch, keyword: str
) -> None:
    eng = await _engine()
    seen: list[Any] = []
    real = eng._record_reads

    def spy(ns: str, session_id: str | None, results: Any) -> None:
        seen.append(session_id)
        real(ns, session_id, results)

    monkeypatch.setattr(eng, "_record_reads", spy)
    try:
        await eng.search("camping", namespace="a", **{keyword: "L1"})
        await eng.assemble("camping", namespace="a", budget_tokens=200, **{keyword: "L2"})
        await eng.read("camping", namespace="a", mode="retrieve", **{keyword: "L3"})
    finally:
        await eng.stop()
    assert {"L1", "L2", "L3"} <= set(seen)


async def test_ledger_id_wins_over_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    seen: list[Any] = []
    real = eng._record_reads

    def spy(ns: str, session_id: str | None, results: Any) -> None:
        seen.append(session_id)
        real(ns, session_id, results)

    monkeypatch.setattr(eng, "_record_reads", spy)
    try:
        await eng.search("camping", namespace="a", session_id="old", ledger_id="new")
    finally:
        await eng.stop()
    assert seen == ["new"]
