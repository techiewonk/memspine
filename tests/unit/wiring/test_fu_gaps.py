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
