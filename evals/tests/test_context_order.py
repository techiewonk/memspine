"""R2-4: --memspine-context-order line orders (offline, stub engine)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from memspine_evals.cli import build_parser
from memspine_evals.readers import QA_PROMPTS
from memspine_evals.systems.memspine_system import (
    OTHER_LINES_HEADER,
    MemspineSystem,
    order_context,
)

IDS = ["n1", "h2", "n2", "n3", "h1", "n4"]
RANKS = {"h1": 1, "h2": 2}


class _Engine:
    async def assemble(self, text: str, **kwargs: Any) -> Any:
        recs = [
            SimpleNamespace(
                record_id=rid,
                content=f"Ann: {rid}",
                valid_from=datetime(2023, 1, day, tzinfo=UTC),
            )
            for day, rid in enumerate(IDS, start=1)
        ]
        return SimpleNamespace(records=recs, tokens_used=0, abstained=False, evidence=None)


async def _no_flush() -> Any:
    return SimpleNamespace(model_calls=0, n_records=0)


async def _ctx(
    order: str, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> tuple[Any, MemspineSystem]:
    @contextlib.contextmanager
    def fake_forensics() -> Any:
        yield {"final": [("h1", 0.9), ("h2", 0.8)]}

    monkeypatch.setattr("memspine.engine.search_forensics", fake_forensics)
    system = MemspineSystem(context_order=order, **kw)
    system._engine = _Engine()
    system.flush = _no_flush  # type: ignore[method-assign]
    for name in ("_calls", "_usage", "_prompt_usage", "_rerank_stats"):
        setattr(system, name, lambda: None)
    system._usage_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._prompt_delta = lambda a, b: {}  # type: ignore[method-assign]
    system._rerank_meta = lambda a, b: {}  # type: ignore[method-assign]
    system._embed_services = lambda texts: {}  # type: ignore[method-assign]
    ctx = await system.query("q", 512, 5)
    return ctx, system


def _day(rid: str) -> str:
    return f"[2023-01-{IDS.index(rid) + 1:02d}] Ann: {rid}"


def _check_spans(ctx: Any) -> None:
    for ev in ctx.evidence:
        a, b = ev.meta["span"]
        assert ctx.text[a:b].endswith(_day(ev.meta["unit_id"]))


async def test_chrono_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, system = await _ctx("chrono", monkeypatch)
    assert ctx.text.splitlines() == [_day(r) for r in IDS]
    assert "context_order" not in system.describe()
    assert "context_order" not in MemspineSystem().describe()
    _check_spans(ctx)


async def test_hits_first(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, system = await _ctx("hits_first", monkeypatch)
    lines = ctx.text.splitlines()
    assert lines == [
        _day("h1"),
        _day("h2"),
        OTHER_LINES_HEADER,
        *[_day(r) for r in ("n1", "n2", "n3", "n4")],
    ]
    assert system.describe()["context_order"] == "hits_first"
    assert [e.turn_id for e in ctx.evidence] == ["h1", "h2", "n1", "n2", "n3", "n4"]
    assert [e.meta["hit_rank"] for e in ctx.evidence][:2] == [1, 2]
    _check_spans(ctx)


async def test_hit_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = await _ctx("hit_blocks", monkeypatch)
    lines = ctx.text.split("\n")
    # h1 (index 4) owns n3 and n4; h2 (index 1) owns n1 and n2 (nearest hit by position).
    assert lines == [
        _day("n3"),
        _day("h1"),
        _day("n4"),
        "",
        _day("n1"),
        _day("h2"),
        _day("n2"),
    ]
    _check_spans(ctx)


async def test_same_line_set_and_no_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    base = set((await _ctx("chrono", monkeypatch))[0].text.splitlines())
    for order in ("hits_first", "hit_blocks"):
        ctx, _ = await _ctx(order, monkeypatch)
        lines = [ln for ln in ctx.text.split("\n") if ln and ln != OTHER_LINES_HEADER]
        assert len(lines) == len(set(lines)) == len(base)
        assert set(lines) == base
        assert sorted(e.turn_id for e in ctx.evidence) == sorted(IDS)


async def test_marks_compose_with_order(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = await _ctx("hits_first", monkeypatch, mark_hits="star")
    assert ctx.text.splitlines()[:2] == [f"* {_day('h1')}", f"* {_day('h2')}"]
    _check_spans(ctx)


def test_order_context_ties_and_no_hits() -> None:
    # tie: n between two hits goes to the higher-ranked one
    assert order_context(["a", "n", "b"], {"a": 2, "b": 1}, "hit_blocks") == [1, 2, "", 0]
    assert order_context(["a", "b"], {}, "hits_first") == [0, 1]
    assert order_context(["a", "b"], {"a": 1}, "hits_first") == [0, OTHER_LINES_HEADER, 1]
    assert order_context(["a", "b"], {"a": 1, "b": 2}, "hits_first") == [0, 1]
    with pytest.raises(ValueError):
        order_context(["a"], {}, "bogus")


def test_invalid_order_rejected() -> None:
    with pytest.raises(ValueError):
        MemspineSystem(context_order="random")


def test_prompt_and_cli() -> None:
    text = QA_PROMPTS["grounded_ordered"]
    assert "{context}" in text and "{question}" in text and "most relevant" in text
    args = build_parser().parse_args(
        [
            "c0-1",
            "--dataset",
            "locomo",
            "--qa-prompt",
            "grounded_ordered",
            "--memspine-context-order",
            "hit_blocks",
        ]
    )
    assert args.memspine_context_order == "hit_blocks"
    assert build_parser().parse_args(["c0-1", "--dataset", "locomo"]).memspine_context_order == (
        "chrono"
    )
