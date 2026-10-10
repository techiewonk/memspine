"""I5 / I39: the adapter hands each turn's speaker and chat role to ``write_messages``.

Default runs write exactly what they always wrote (role ``user``, no speaker key); with the
perspective layer configured (or ``perspective_metadata=True``) the speaker and role travel as
message metadata. The stored text is identical in both cases.
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine_evals.contracts import Turn
from memspine_evals.systems.memspine_system import MemspineSystem

_HASH = {"embedding": {"provider": "hash"}, "dotenv_path": None}


def _turn(speaker: str, text: str, n: int) -> Turn:
    return Turn(turn_id=f"D1:{n}", session_id="s1", speaker=speaker, text=text)


def test_default_message_is_unchanged() -> None:
    sys_ = MemspineSystem(config=_HASH)
    msg = sys_._message(_turn("Caroline", "hi", 1), "Caroline: hi")
    assert msg == {"role": "user", "content": "Caroline: hi"}


def test_metadata_for_named_speakers_and_chat_roles() -> None:
    sys_ = MemspineSystem(config=_HASH, perspective_metadata=True)
    named = sys_._message(_turn("Caroline", "hi", 1), "Caroline: hi")
    assert named == {"role": "user", "content": "Caroline: hi", "speaker": "Caroline"}
    bot = sys_._message(_turn("assistant", "hello", 2), "assistant: hello")
    assert bot["role"] == "assistant"
    assert bot["speaker"] == "assistant"


def test_enabled_by_the_engine_policy_and_end_to_end_tags() -> None:
    config: dict[str, Any] = {
        **_HASH,
        "memories": {"episodic": {"enabled": True, "policies": {"perspective": "heuristic"}}},
        "read": {"hybrid": False, "record_access": False},
    }
    sys_ = MemspineSystem(template="core", config=config)
    assert sys_._perspective_metadata is True
    assert MemspineSystem(config=_HASH)._perspective_metadata is False

    async def run() -> list[Any]:
        await sys_.reset("item")
        await sys_.insert(_turn("Caroline", "My cousin needs a job.", 1))
        await sys_.insert(_turn("Melanie", "Does your cousin like cooking?", 2))
        records = await sys_._engine._records(sys_.namespace)
        return sorted(records, key=lambda r: r.content)

    records = asyncio.run(run())
    by_text = {r.content: r for r in records}
    first = by_text["Caroline: My cousin needs a job."]
    assert {"spk:caroline", "sub:cousin@caroline"} <= set(first.tags)
    second = by_text["Melanie: Does your cousin like cooking?"]
    assert {"spk:melanie", "addr:caroline", "mod:question"} <= set(second.tags)
