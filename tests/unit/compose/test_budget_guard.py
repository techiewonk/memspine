"""ADR-092: the final budget guard on the context a read or assemble returns."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.compose.pack import AssembledContext, HitManifest, enforce_budget, estimate_tokens
from memspine.config import constants
from memspine.store.records import MemoryRecord, SourceInfo


def words(text: str) -> int:
    return len(text.split())


def _rec(text: str, *, kind: str = "episodic", channel: str = "chat") -> MemoryRecord:
    return MemoryRecord(
        namespace="a", memory_type=kind, content=text, source=SourceInfo(channel=channel)
    )


def _block(text: str, tag: str, parents: list[MemoryRecord] | None = None) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=text,
        tags=[constants.LEAD_TAG, tag],
        source=SourceInfo(
            role="system", channel="lead", parents=[p.record_id for p in parents or []]
        ),
    ).as_engine_block()


def _layout() -> tuple[AssembledContext, dict[str, MemoryRecord]]:
    """persona | fact (stable) || cards, graph facts, profile, caption, t1, t2, asset(t2)."""
    persona = _rec("persona: be brief", kind="working", channel="persona")
    fact = _rec("stable fact here", kind="semantic")
    t1 = _rec("turn one two three")
    t2 = _rec("turn four five six")
    cards = _block("cards a b c", constants.CARDS_TAG)
    graph = _block("graph a b c", constants.GRAPH_FACTS_TAG)
    profile = _block("profile a b c", constants.PROFILE_TAG)
    caption = _block("caption x y z", constants.RETRIEVED_CAPTION_TAG, [t1, t2])
    asset = _block("asset evidence line", "asset_evidence", [t2])
    records = [persona, fact, cards, graph, profile, caption, t1, t2, asset]
    named = {
        "persona": persona,
        "fact": fact,
        "t1": t1,
        "t2": t2,
        "cards": cards,
        "graph": graph,
        "profile": profile,
        "caption": caption,
        "asset": asset,
    }
    ctx = AssembledContext(records=records, boundary_index=2, tokens_used=0)
    return ctx, named


def _names(ctx: AssembledContext, named: dict[str, MemoryRecord]) -> list[str]:
    by_id = {id(r): n for n, r in named.items()}
    return [by_id[id(r)] for r in ctx.records]


def test_a_context_that_fits_is_unchanged() -> None:
    ctx, named = _layout()
    total = sum(words(r.content) for r in ctx.records)
    out = enforce_budget(ctx, total, words)
    assert out.budget_status == "within" and len(out.records) == len(named)


def test_trim_order_retrieved_then_headers_then_blocks() -> None:
    expected = [
        # 1. retrieved from the end: t2 (with its asset line), then t1 (with the caption)
        ["persona", "fact", "cards", "graph", "profile", "caption", "t1"],
        ["persona", "fact", "cards", "graph", "profile"],
        # 2. header blocks in LEAD_BLOCK_DROP_ORDER: profile, (entity summaries), graph, cards
        ["persona", "fact", "cards", "graph"],
        ["persona", "fact", "cards"],
        ["persona", "fact"],
    ]
    budgets = [26, 18, 14, 10, 6]  # persona 3 + fact 3 = 6 words of stable prefix
    for budget, kept in zip(budgets, expected, strict=True):
        ctx, named = _layout()
        out = enforce_budget(ctx, budget, words)
        assert _names(out, named) == kept, budget
        assert sum(words(r.content) for r in out.records) <= budget
        assert out.budget_status == "trimmed"
        assert out.tokens_used == sum(words(r.content) for r in out.records)
        assert out.boundary_index == 2


def test_other_engine_blocks_go_after_the_header_order() -> None:
    persona = _rec("persona: be brief", kind="working", channel="persona")
    head = _block("user header line", "user_header")
    note = _block("weak evidence note", constants.WEAK_EVIDENCE_TAG)
    cards = _block("cards a b", constants.CARDS_TAG)
    # cards (header order) first, then the other blocks from the end: note, then head
    kept = {
        9: ["user header line", "persona: be brief", "weak evidence note"],
        6: ["user header line", "persona: be brief"],
        3: ["persona: be brief"],
    }
    for budget, contents in kept.items():
        ctx = AssembledContext(records=[head, persona, cards, note], boundary_index=2)
        out = enforce_budget(ctx, budget, words)
        assert [r.content for r in out.records] == contents, budget
    assert out.boundary_index == 1


def test_protected_records_stay_and_report_oversize() -> None:
    persona = _rec("persona " * 20, kind="working", channel="persona")
    turn = _rec("turn words")
    ctx = AssembledContext(records=[persona, turn], boundary_index=1)
    out = enforce_budget(ctx, 5, words)
    assert [r.record_id for r in out.records] == [persona.record_id]
    assert out.budget_status == "oversize"


def test_packer_oversize_is_kept_when_the_guard_trims() -> None:
    ctx, _ = _layout()
    ctx.budget_status = "oversize"
    assert enforce_budget(ctx, 12, words).budget_status == "oversize"


def test_negative_budget_is_zero() -> None:
    ctx = AssembledContext(records=[_rec("a b"), _rec("c d")], boundary_index=0)
    out = enforce_budget(ctx, -5, words)
    assert out.records == [] and out.budget_status == "trimmed"


def test_manifests_stay_consistent_after_a_trim() -> None:
    fact = _rec("fact line", kind="semantic")
    turn_a = _rec("support turn a")
    turn_b = _rec("support turn b")
    other = _rec("other record words")
    hits = {
        fact.record_id: HitManifest(
            fact.record_id,
            needed_turns=(turn_a.record_id, turn_b.record_id),
            included_turns=(turn_a.record_id, turn_b.record_id),
        ),
        other.record_id: HitManifest(other.record_id),
    }
    ctx = AssembledContext(records=[fact, turn_a, turn_b, other], boundary_index=0, hits=hits)
    # 11 words; budget 5 removes `other` (3) then `turn_b` (3)
    out = enforce_budget(ctx, 5, words)
    assert [r.record_id for r in out.records] == [fact.record_id, turn_a.record_id]
    assert other.record_id not in out.hits
    hit = out.hits[fact.record_id]
    assert hit.included_turns == (turn_a.record_id,)
    assert hit.dropped_turns == (turn_b.record_id,)
    assert hit.status == "partial" and out.partial_hits == [fact.record_id]
    for rid in out.hits:
        assert rid in {r.record_id for r in out.records}


def test_the_guard_is_deterministic() -> None:
    first, named = _layout()
    second = AssembledContext(records=list(first.records), boundary_index=2)
    a = enforce_budget(first, 11, words)
    b = enforce_budget(second, 11, words)
    assert [r.record_id for r in a.records] == [r.record_id for r in b.records]
    assert _names(a, named)


# ---------------------------------------------------------------- the engine


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


_EXTRAS: dict[str, Any] = {
    "recent_exchanges": 3,  # a header block (C2)
    "section_captions": True,  # C7 caption
    "span_line": True,  # duration line
    "evidence_signal": True,
    "evidence_line": True,
    "evidence_weak_below": 100.0,  # every read is weak: the note is always added
    "entity_check": "note",  # read overlay note
    "user_header": "on",
    "perspective_asker": "Ana",
    "contract_rules": True,  # the typed [contract] header in read()
}

_QUERY = "How long after Ana moved to Lyon did Bruno adopt the dog?"


async def _seed(eng: Engine) -> None:
    for text, when in (
        ("Ana: I moved to Lyon today, the flat is near the river", datetime(2023, 3, 1)),
        ("Ana: we adopted a dog called Pip from the shelter", datetime(2023, 4, 12)),
        ("Ana: the weather is grey and the dog hates the rain", datetime(2023, 4, 20)),
        (" ".join(f"Ana: Lyon note {i} is long." for i in range(40)), datetime(2023, 5, 2)),
    ):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=when.replace(tzinfo=UTC)
        )


@pytest.mark.parametrize("mode", ["retrieve", "replay", "compose", "auto"])
async def test_read_never_exceeds_the_budget_with_every_extra_on(mode: str) -> None:
    eng = _engine(**_EXTRAS)
    await eng.start()
    try:
        await _seed(eng)
        for budget in [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377]:
            result = await eng.read(_QUERY, namespace="a", mode=mode, budget_tokens=budget)
            ctx = result.context
            spent = sum(estimate_tokens(r.content) for r in ctx.records)
            assert spent <= max(0, budget), (mode, budget, spent, ctx.budget_status)
            assert ctx.budget_status in ("within", "trimmed", "oversize")
            shown = {r.record_id for r in ctx.records}
            assert all(rid in shown for rid in ctx.hits)
    finally:
        await eng.stop()


async def test_assemble_never_exceeds_the_budget_with_every_extra_on() -> None:
    eng = _engine(**_EXTRAS)
    await eng.start()
    try:
        await _seed(eng)
        trimmed = False
        for budget in range(0, 200, 7):
            ctx = await eng.assemble(_QUERY, namespace="a", budget_tokens=budget)
            spent = sum(estimate_tokens(r.content) for r in ctx.records)
            assert spent <= budget, (budget, spent, ctx.budget_status)
            trimmed = trimmed or ctx.budget_status == "trimmed"
        assert trimmed  # the extras do push some budgets over: the guard ran
    finally:
        await eng.stop()


async def test_headers_larger_than_the_budget_never_give_a_negative_routed_budget() -> None:
    """``read.lead_budget_share`` unset: the header blocks can exceed the whole budget; the
    routed read then gets 0 (never negative) and the final guard trims the headers."""
    eng = _engine(section_captions=True)
    await eng.start()
    seen: list[int] = []
    routed = eng._read_routed

    async def spy(query: str, ns: str, mode: str, budget: int, *args: Any, **kw: Any) -> Any:
        seen.append(budget)
        return await routed(query, ns, mode, budget, *args, **kw)

    async def big_headers(ns: str, *args: Any, **kw: Any) -> list[MemoryRecord]:
        return [_block("profile " * 200, constants.PROFILE_TAG)]

    eng._read_routed = spy  # type: ignore[method-assign]
    eng._read_headers = big_headers  # type: ignore[method-assign]
    try:
        await _seed(eng)
        for budget in (1, 2, 4, 8, 64):
            result = await eng.read("Lyon", namespace="a", mode="retrieve", budget_tokens=budget)
            ctx = result.context
            spent = sum(estimate_tokens(r.content) for r in ctx.records)
            assert spent <= budget
            assert ctx.budget_status in ("trimmed", "oversize")
            assert all(constants.PROFILE_TAG not in r.tags for r in ctx.records)
        assert seen == [0, 0, 0, 0, 0]
    finally:
        await eng.stop()
