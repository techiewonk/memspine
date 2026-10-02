"""P4: answer-free query rewrites feed the compose read (LLM stubbed)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine import Engine


class _StubChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[object] = []

    async def chat(self, messages: object, **_: object) -> str:
        self.prompts.append(messages)
        return self.reply


class _StubLLM:
    def __init__(self, chat: _StubChat) -> None:
        self.roles = ("query_rewrite",)
        self._chat = chat

    def for_role(self, role: str) -> _StubChat:
        return self._chat


def _engine(on: bool) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "compose_rewrites": on},
    )


async def test_rewrites_become_extra_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(on=True)
    await eng.start()
    try:
        chat = _StubChat("1. Melanie seaside trips\n- Melanie ocean visits\nthird ignored")
        monkeypatch.setattr(eng, "_llm", _StubLLM(chat))
        probes = await eng._query_rewrite_probes("How many times did Melanie go to the beach?")
        assert probes == ["Melanie seaside trips", "Melanie ocean visits"]
        await eng.write(
            "Melanie visited the seaside",
            namespace="a",
            memory_type="episodic",
            valid_from=datetime(2023, 5, 1, tzinfo=UTC),
        )
        out = await eng.read(
            "How many times did Melanie go to the beach?", namespace="a", mode="compose", top_k=2
        )
        assert out.mode == "compose"
        assert chat.prompts  # the rewrite role was consulted
    finally:
        await eng.stop()


async def test_off_or_unbound_adds_nothing() -> None:
    eng = _engine(on=False)
    await eng.start()
    try:
        assert await eng._query_rewrite_probes("any question") == []
    finally:
        await eng.stop()
