"""C-1 adapter fix: LoCoMo/LME dates become event time; speaker names stay in text."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

pytest.importorskip("memspine")

from memspine_evals.contracts import Turn
from memspine_evals.systems.memspine_system import MemspineSystem, parse_turn_time


def test_parse_benchmark_stamps() -> None:
    assert parse_turn_time("1:56 pm on 8 May, 2023") == datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
    assert parse_turn_time("2023/05/20 (Sat) 02:21") == datetime(2023, 5, 20, 2, 21, tzinfo=UTC)
    assert parse_turn_time(None) is None
    assert parse_turn_time("not a date") is None


def test_insert_keeps_speaker_and_date_and_renders_dates() -> None:
    async def run() -> str:
        system = MemspineSystem(config={"embedding": {"provider": "hash"}, "dotenv_path": None})
        await system.reset("conv-1")
        await system.insert(
            Turn(
                turn_id="D1:1",
                session_id="session_1",
                speaker="Caroline",
                text="I went to the LGBTQ support group yesterday.",
                timestamp="1:56 pm on 8 May, 2023",
            )
        )
        ctx = await system.query("When did Caroline go to the support group?", 2048, 5)
        await system.close()
        return ctx.text

    text = asyncio.run(run())
    assert "Caroline: I went to the LGBTQ support group" in text
    assert "[2023-05-08]" in text
