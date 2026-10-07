"""S6c audit (2026-10-07): session ids never reach the answering context.

Backboard's LongMemEval harness wrote each session's id into the ingested text, and
every LongMemEval evidence session id starts with ``answer_``: the reader could read the
gold location off the context. memspine must never render a turn's session id, turn id
or message id into what the reader sees, in any read mode.
"""

from __future__ import annotations

import asyncio

import pytest
from memspine_evals.contracts import Turn
from memspine_evals.systems.memspine_system import MemspineSystem

pytest.importorskip("memspine")

_HASH = {"embedding": {"provider": "hash"}, "dotenv_path": None}


def _turns() -> list[Turn]:
    return [
        Turn(
            turn_id=f"{sid}:{i}",
            session_id=sid,
            speaker="user",
            text=text,
            timestamp="2023/05/20 (Sat) 02:21",
        )
        for i, (sid, text) in enumerate(
            [
                ("answer_4be1b6b4_1", "I adopted a beagle called Pip last spring."),
                ("sharegpt_xyz_0", "Can you suggest a pasta recipe?"),
                ("answer_4be1b6b4_2", "Pip loves the beach near our flat."),
            ]
        )
    ]


@pytest.mark.parametrize("mode", [None, "replay", "full", "auto"])
def test_session_and_turn_ids_never_reach_the_context(mode: str | None) -> None:
    async def run() -> str:
        system = MemspineSystem(config=_HASH, read_mode=mode)
        await system.reset("lme-q1")
        for turn in _turns():
            await system.insert(turn)
        ctx = await system.query("What is my dog called?", 2048, 5)
        await system.close()
        return ctx.text

    text = asyncio.run(run())
    assert "Pip" in text
    for leak in ("answer_", "sharegpt_", "4be1b6b4", ":0", ":1"):
        assert leak not in text, leak
