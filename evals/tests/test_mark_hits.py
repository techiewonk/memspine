"""C1/C2: --memspine-mark-hits line markers and the grounded_detail prompt (offline)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from memspine_evals.cli import build_parser
from memspine_evals.readers import QA_PROMPTS
from memspine_evals.systems.memspine_system import (
    MemspineSystem,
    hit_rank_map,
    mark_hit_line,
)


def test_mark_hit_line_modes() -> None:
    assert mark_hit_line("[2023-01-01] A: x", 2, "star") == "* [2023-01-01] A: x"
    assert mark_hit_line("[2023-01-01] A: x", 2, "rank") == "[hit 2] [2023-01-01] A: x"
    assert mark_hit_line("[2023-01-01] A: x", None, "star") == "[2023-01-01] A: x"
    assert mark_hit_line("[2023-01-01] A: x", 1, "off") == "[2023-01-01] A: x"


def test_hit_rank_map_is_one_based() -> None:
    assert hit_rank_map({"final": [("r3", 0.9), ("r1", 0.5)]}) == {"r3": 1, "r1": 2}
    assert hit_rank_map({}) == {}


class _Engine:
    async def assemble(self, text: str, **kwargs: Any) -> Any:
        recs = [
            SimpleNamespace(
                record_id=rid,
                content=f"Ann: {rid}",
                valid_from=datetime(2023, 1, day, tzinfo=UTC),
            )
            for day, rid in enumerate(["n1", "h2", "n2", "h1"], start=1)
        ]
        return SimpleNamespace(records=recs, tokens_used=0, abstained=False, evidence=None)


async def _no_flush() -> Any:
    return SimpleNamespace(model_calls=0, n_records=0)


async def _render(mode: str, monkeypatch: pytest.MonkeyPatch) -> tuple[str, MemspineSystem]:
    @contextlib.contextmanager
    def fake_forensics() -> Any:
        yield {"final": [("h1", 0.9), ("h2", 0.8)]}

    monkeypatch.setattr("memspine.engine.search_forensics", fake_forensics)
    system = MemspineSystem(mark_hits=mode)
    system._engine = _Engine()
    system.flush = _no_flush  # type: ignore[method-assign]
    for name in ("_calls", "_usage", "_prompt_usage", "_rerank_stats"):
        setattr(system, name, lambda: None)
    system._usage_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._prompt_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._rerank_meta = lambda a, b: {}  # type: ignore[method-assign]
    system._embed_services = lambda texts: {}  # type: ignore[method-assign]
    ctx = await system.query("q", 512, 5)
    return ctx.text, system


async def test_star_marks_hits_only_and_keeps_order(monkeypatch: pytest.MonkeyPatch) -> None:
    text, system = await _render("star", monkeypatch)
    assert text.splitlines() == [
        "[2023-01-01] Ann: n1",
        "* [2023-01-02] Ann: h2",
        "[2023-01-03] Ann: n2",
        "* [2023-01-04] Ann: h1",
    ]
    assert system.describe()["mark_hits"] == "star"


async def test_rank_marks_with_hit_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    text, system = await _render("rank", monkeypatch)
    assert text.splitlines() == [
        "[2023-01-01] Ann: n1",
        "[hit 2] [2023-01-02] Ann: h2",
        "[2023-01-03] Ann: n2",
        "[hit 1] [2023-01-04] Ann: h1",
    ]
    assert system.describe()["mark_hits"] == "rank"


async def test_off_is_unchanged_and_not_in_describe(monkeypatch: pytest.MonkeyPatch) -> None:
    text, system = await _render("off", monkeypatch)
    assert "* " not in text and "[hit" not in text
    assert "mark_hits" not in system.describe()
    assert "mark_hits" not in MemspineSystem().describe()


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError):
        MemspineSystem(mark_hits="bold")


def test_grounded_detail_prompt_registered() -> None:
    text = QA_PROMPTS["grounded_detail"]
    assert "{context}" in text and "{question}" in text
    assert "start with * or [hit k]" in text
    assert "one sentence" in text and "every matching item" in text


def test_cli_flags_parse() -> None:
    args = build_parser().parse_args(
        [
            "c0-1",
            "--dataset",
            "locomo",
            "--qa-prompt",
            "grounded_detail",
            "--memspine-mark-hits",
            "star",
        ]
    )
    assert args.qa_prompt == "grounded_detail"
    assert args.memspine_mark_hits == "star"
    default = build_parser().parse_args(["c0-1", "--dataset", "locomo"])
    assert default.memspine_mark_hits == "off"
