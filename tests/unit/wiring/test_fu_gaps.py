# ruff: noqa: E501
"""FU-GAPS: the known non-GPU gaps. Model-free stubs, hash embedder."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_w2_06b_prompt_v2 import WindowLLM, _run
from test_window_write import _facts

pytestmark = pytest.mark.asyncio


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
