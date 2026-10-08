"""N34: memspine reads as of each question's date (LongMemEval ``question_date``)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from memspine_evals.systems.memspine_system import MemspineSystem, parse_question_date


def test_parse_question_date() -> None:
    assert parse_question_date("2023/05/30 (Tue) 23:40") == datetime(
        2023, 5, 30, 23, 40, tzinfo=UTC
    )
    assert parse_question_date("2023/05/30") == datetime(2023, 5, 30, 23, 59, tzinfo=UTC)
    assert parse_question_date(None) is None
    assert parse_question_date("not a date") is None


class _SpyEngine:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def assemble(self, text: str, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        return SimpleNamespace(records=[], tokens_used=0, abstained=False, evidence=None)


async def _query(system: MemspineSystem, meta: dict[str, Any]) -> dict[str, Any]:
    spy = _SpyEngine()
    system._engine = spy
    system.flush = _no_flush  # type: ignore[method-assign]
    for name in ("_calls", "_usage", "_prompt_usage", "_rerank_stats"):
        setattr(system, name, lambda: None)
    system._usage_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._prompt_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._rerank_meta = lambda a, b: {}  # type: ignore[method-assign]
    system._embed_services = lambda texts: {}  # type: ignore[method-assign]
    system.set_query_meta(meta)
    with contextlib.suppress(Exception):  # only the engine call matters here
        await system.query("what did I buy?", 512, 5)
    return spy.kwargs[0]


async def _no_flush() -> Any:
    return SimpleNamespace(model_calls=0)


async def test_off_passes_no_as_of() -> None:
    kwargs = await _query(MemspineSystem(), {"question_date": "2023/05/30 (Tue) 23:40"})
    assert "as_of" not in kwargs


async def test_on_passes_the_question_date() -> None:
    system = MemspineSystem(as_of_question_date=True)
    kwargs = await _query(system, {"question_date": "2023/05/30 (Tue) 23:40"})
    assert kwargs["as_of"] == datetime(2023, 5, 30, 23, 40, tzinfo=UTC)
    assert system.describe()["as_of_question_date"] is True


async def test_on_without_a_date_passes_none() -> None:
    kwargs = await _query(MemspineSystem(as_of_question_date=True), {})
    assert "as_of" not in kwargs


def test_off_keeps_the_config_hash_keys() -> None:
    assert "as_of_question_date" not in MemspineSystem().describe()
