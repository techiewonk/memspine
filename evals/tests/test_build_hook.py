"""The optional ``build`` hook runs once after ingestion and its calls land in K."""

from __future__ import annotations

import asyncio
from typing import Any

from memspine_evals.contracts import DepositResult
from memspine_evals.systems.memspine_system import MemspineSystem


class _FakeEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.slept = 0

    def model_calls(self) -> dict[str, int]:
        return {"extract": self.calls}

    async def sleep(self) -> dict[str, dict[str, Any]]:
        self.slept += 1
        self.calls += 3
        return {"mine_facts": {"status": "ok", "sessions": 3}}


def test_build_measures_sleep_calls() -> None:
    system = MemspineSystem(build_sleep=True)
    fake = _FakeEngine()
    system._engine = fake
    result = asyncio.run(system.build())
    assert fake.slept == 1 and result.model_calls == 3
    assert result.meta["sleep"]["mine_facts"]["sessions"] == 3


def test_build_off_is_a_noop() -> None:
    system = MemspineSystem()
    system._engine = _FakeEngine()
    assert asyncio.run(system.build()) == DepositResult()
    assert system.describe()["build_sleep"] is False
