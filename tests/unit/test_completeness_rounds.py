"""G-17: ``read.completeness_rounds`` bounds the completeness loop of a checked compose."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig


def test_one_round_by_default() -> None:
    assert ReadConfig().completeness_rounds == 1


@pytest.mark.parametrize(("rounds", "expected"), [(1, 2), (2, 3), (3, 3)])
async def test_rounds_add_new_queries_until_none_appear(rounds: int, expected: int) -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"record_access": False, "completeness_check": True, "completeness_rounds": rounds},
    )
    await eng.start()
    composes: list[list[str]] = []
    answers = iter([["q1"], ["q2"], ["q2"], ["q3"]])  # the 3rd round finds nothing new

    async def compose(*args: Any, **kwargs: Any) -> Any:
        composes.append(list(kwargs.get("extra_probes", [])))
        return SimpleNamespace(context=SimpleNamespace(abstained=False, records=[1]))

    async def missing(query: str, context: Any) -> list[str]:
        return next(answers)

    async def no_rewrites(query: str) -> list[str]:
        return []

    eng._compose = compose  # type: ignore[method-assign]
    eng._missing_info_queries = missing  # type: ignore[method-assign]
    eng._query_rewrite_probes = no_rewrites  # type: ignore[method-assign]
    try:
        await eng._compose_checked(
            "how many trips?",
            "a",
            400,
            5,
            3,
            hide=None,
            extra_probes=[],
            replay_window=2,
            session_id=None,
            legs=[],
        )
    finally:
        await eng.stop()
    assert len(composes) == expected
    assert composes[-1] == ["q1", "q2"][: expected - 1]
