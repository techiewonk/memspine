"""G1b: the cards header (``read.cards: header``) over mined atomic facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.policies.assembly import estimate_tokens

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)

TURNS = [
    "we talked about the weather today",
    "my sister Ana adopted a grey cat called Miso",
    "then we discussed football for a while",
    "Ana also started pottery classes on Mondays",
    "the match ended in a draw",
]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _seed(eng: Engine) -> list[str]:
    ids = []
    for i, text in enumerate(TURNS):
        rec = await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )
        ids.append(rec.record_id)
    await eng._deposit_mined_fact(
        "a",
        "Ana pet: Ana adopted a grey cat named Miso",
        "Ana",
        "pet",
        ids,
        T0 + timedelta(seconds=30),
        "s1",
        kind="state",
    )
    await eng._deposit_mined_fact(
        "a",
        "Ana event: Ana started pottery classes",
        "Ana",
        "event",
        ids,
        T0 + timedelta(days=1),
        "s1",
        kind="event",
    )
    return ids


def _header(records: list[Any]) -> list[Any]:
    return [r for r in records if constants.CARDS_TAG in r.tags]


@pytest.mark.parametrize("mode", ["retrieve", "replay", "compose", "full"])
async def test_header_opens_the_context_within_its_share(mode: str) -> None:
    eng = _engine(cards="header", cards_budget_share=0.25)
    await eng.start()
    try:
        await _seed(eng)
        budget = 400 if mode != "full" else 4000
        out = await eng.read(
            "what pet does Ana have", namespace="a", mode=mode, top_k=3, budget_tokens=budget
        )
        [header] = _header(out.context.records)
        assert out.context.records[out.context.boundary_index] is header
        assert header.content.startswith(constants.CARDS_MARKER)
        assert "[said 2023-05-07] Ana: Ana adopted a grey cat named Miso" in header.content
        assert estimate_tokens(header.content) <= int(budget * 0.25)
        assert out.context.tokens_used >= estimate_tokens(header.content)
        # No fact twice: mined facts only appear inside the header.
        rest = [r for r in out.context.records if r is not header]
        assert not any("atomic_fact" in r.tags for r in rest)
        joined = "\n".join(r.content for r in out.context.records)
        assert joined.count("grey cat named Miso") == 1
    finally:
        await eng.stop()


async def test_assemble_gets_the_header_and_counts_it() -> None:
    eng = _engine(cards="header")
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.assemble("Ana pottery", namespace="a", budget_tokens=400, top_k=3)
        [header] = _header(out.records)
        assert "Ana: Ana started pottery classes" in header.content
        body = sum(estimate_tokens(r.content) for r in out.records if r is not header)
        assert out.tokens_used >= estimate_tokens(header.content)
        assert out.tokens_used <= 400 and body <= 400 - estimate_tokens(header.content)
    finally:
        await eng.stop()


async def test_header_drops_lines_that_do_not_fit() -> None:
    eng = _engine(cards="header", cards_budget_share=0.1)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read("Ana", namespace="a", mode="retrieve", budget_tokens=200)
        for header in _header(out.context.records):
            assert estimate_tokens(header.content) <= 20
            assert header.content.count("\n") == 1  # one card fits, not two
    finally:
        await eng.stop()


@pytest.mark.parametrize("mode", ["retrieve", "replay", "compose", "full"])
async def test_off_is_byte_identical(mode: str) -> None:
    """``cards: off`` adds nothing: ``read`` equals the routed read it wraps, with the
    mined facts still in the main part (reads are side-effect free here)."""
    eng = _engine(cards="off")
    await eng.start()
    try:
        await _seed(eng)
        budget = 400 if mode != "full" else 4000
        out = await eng.read(
            "what pet does Ana have", namespace="a", mode=mode, top_k=3, budget_tokens=budget
        )
        plain = await eng._read_routed(
            "what pet does Ana have", "a", mode, eng._reply_budget(budget), 3, 2, 3
        )
        assert out.mode == plain.mode
        assert [(r.record_id, r.content) for r in out.context.records] == [
            (r.record_id, r.content) for r in plain.context.records
        ]
        assert out.context.tokens_used == plain.context.tokens_used
        assert out.context.boundary_index == plain.context.boundary_index
        assert not _header(out.context.records)
        assert any("atomic_fact" in r.tags for r in out.context.records)
    finally:
        await eng.stop()


async def test_quarantined_fact_never_reaches_the_header() -> None:
    eng = _engine(cards="header")
    await eng.start()
    try:
        await _seed(eng)
        storage = eng._require_started()
        [pet] = [r for r in await storage.list_records("a", "semantic") if "kind:state" in r.tags]
        await eng.quarantine(pet.record_id, namespace="a")
        out = await eng.read(
            "what pet does Ana have", namespace="a", mode="retrieve", budget_tokens=400
        )
        assert all("Miso" not in h.content for h in _header(out.context.records))
    finally:
        await eng.stop()


async def test_routed_read_keeps_top_k_raw_turns_when_facts_are_hidden() -> None:
    """Smoke 2026-10-05: facts hidden from the routed read AFTER its top_k cut left
    fewer raw turns than top_k. With many facts outranking the turns, the routed
    retrieve must still return top_k raw turns."""
    eng = _engine(cards="header", cards_budget_share=0.25)
    await eng.start()
    try:
        ids = await _seed(eng)
        for n in range(8):  # facts that match the query better than most turns
            await eng._deposit_mined_fact(
                "a", f"Ana event: Ana talked about the weather and football {n}", "Ana",
                "event", ids, T0 + timedelta(hours=n + 2), "s1", kind="event",
            )  # fmt: skip
        out = await eng.read(
            "weather football Ana", namespace="a", mode="retrieve", top_k=3, budget_tokens=4000
        )
        raw = [r for r in out.context.records if r.memory_type == "episodic"]
        assert len(raw) >= 3  # before the fix: 1
    finally:
        await eng.stop()


async def test_cards_skip_temporal_questions() -> None:
    """Smoke 2026-10-05: cards' own dates cost temporal questions; skip them there."""
    eng = _engine(cards="header", cards_skip_temporal=True)
    await eng.start()
    try:
        await _seed(eng)
        when = await eng.read(
            "When did Ana adopt the cat?", namespace="a", mode="retrieve", top_k=3
        )
        what = await eng.read("What pet does Ana have?", namespace="a", mode="retrieve", top_k=3)
        assert not _header(when.context.records)
        assert _header(what.context.records)
    finally:
        await eng.stop()
