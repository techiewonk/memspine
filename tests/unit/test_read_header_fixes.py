"""Read-path fixes for the cards / profile headers (review round 3, A-1..A-4, A-7, A-9, A-10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core.policies.assembly import estimate_tokens
from memspine.exceptions import ConfigError

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
QUERY = "weather football Ana talked"


def _engine(read: dict[str, Any], **kw: Any) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
        **kw,
    )


async def _seed(
    eng: Engine, turns: list[str], facts: list[str], *, parents: int | None = None
) -> list[str]:
    ids = []
    for i, text in enumerate(turns):
        rec = await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )
        ids.append(rec.record_id)
    for n, fact in enumerate(facts):
        await eng._deposit_mined_fact(
            "a", fact, "Ana", "event", ids[:parents] if parents else ids,
            T0 + timedelta(days=3 + n), "s1", kind="event",
        )  # fmt: skip
    return ids


class _Stub:
    """Word-overlap reranker that records every call."""

    reranker_id = "stub"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append(list(documents))
        words = set(query.lower().split())
        return [float(len(words & set(d.lower().split()))) for d in documents]


def _cards(records: list[Any]) -> list[Any]:
    return [r for r in records if constants.CARDS_TAG in r.tags]


TURNS_12 = [f"we talked about weather and football day {i}" for i in range(12)]
FACTS_10 = [f"Ana event: Ana talked about the weather and football {n}" for n in range(10)]


# A-1 --------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["retrieve", "compose"])
async def test_rerank_keep_with_cards_header_keeps_raw_turns(mode: str) -> None:
    """A-1: ``rerank_keep`` cut the candidates before the header's hide removed the
    facts, so the routed read came back empty (and abstained)."""
    eng = _engine({"cards": "header", "candidate_pool": 4, "rerank_keep": 6})
    await eng.start()
    try:
        await _seed(eng, TURNS_12, FACTS_10)
        stub = _Stub()
        eng._rerank_provider = lambda: stub  # type: ignore[method-assign]
        out = await eng.read(QUERY, namespace="a", mode=mode, top_k=3, budget_tokens=4000)
        assert not out.context.abstained
        raw = [r for r in out.context.records if r.memory_type == "episodic"]
        assert len(raw) >= 3
    finally:
        await eng.stop()


# A-2 --------------------------------------------------------------------------------


async def test_reranker_runs_once_per_search_of_a_read() -> None:
    """A-2: each widening pass re-ran the whole search (rerank included). Now the
    routed read reranks once, on visible records only; the cards header's own
    search is the only other rerank."""
    turns = [f"random chatter {i} about nothing" for i in range(5)]
    facts = [f"Ana event: Ana talked about weather football {n}" for n in range(300)]
    eng = _engine({"cards": "header", "candidate_pool": 3})
    await eng.start()
    try:
        await _seed(eng, turns, facts)
        stub = _Stub()
        eng._rerank_provider = lambda: stub  # type: ignore[method-assign]
        await eng.read(QUERY, namespace="a", mode="retrieve", top_k=5, budget_tokens=4000)
        assert len(stub.calls) == 2  # the header search, then the routed search
        routed = stub.calls[1]
        assert not any("weather football" in doc for doc in routed)  # no hidden fact
    finally:
        await eng.stop()


async def test_hidden_fact_access_count_unchanged() -> None:
    """A-2: a fact the header hides (not shown) is not counted as accessed."""
    turns = [f"we talked about weather and football day {i}" for i in range(4)]
    facts = [f"Ana event: Ana talked about the weather and football {n}" for n in range(30)]
    eng = _engine({"cards": "header", "cards_top_k": 2, "record_access": True})
    await eng.start()
    try:
        await _seed(eng, turns, facts)
        out = await eng.read(QUERY, namespace="a", mode="retrieve", top_k=3, budget_tokens=4000)
        [header] = _cards(out.context.records)
        shown = set(header.source.parents)
        storage = eng._require_started()
        hidden = [
            r
            for r in await storage.list_records("a", "semantic")
            if "atomic_fact" in r.tags and r.record_id not in shown
        ]
        assert hidden
        assert all(r.scoring.access_count == 0 for r in hidden)
    finally:
        await eng.stop()


# A-3 --------------------------------------------------------------------------------


async def test_claims_only_does_not_put_back_a_header_fact() -> None:
    """A-3: B9 re-added the mined fact the header shows; the card had no CLAIM label."""
    eng = _engine(
        {"cards": "header"},
        integrity={"enabled": True, "claims_only_below": 0.99, "admission_threshold": 0.0},
    )
    await eng.start()
    try:
        await _seed(
            eng,
            ["Ana adopted a grey cat named Miso yesterday", "weather was fine"],
            ["Ana pet: Ana adopted a grey cat named Miso"],
            parents=1,
        )
        out = await eng.read(
            "Ana grey cat Miso", namespace="a", mode="retrieve", top_k=3, budget_tokens=2000
        )
        joined = "\n".join(r.content for r in out.context.records)
        assert joined.count("grey cat named Miso") == 1
        [header] = _cards(out.context.records)
        [line] = [ln for ln in header.content.splitlines() if "grey cat named Miso" in ln]
        assert constants.CLAIM_MARKER in line
    finally:
        await eng.stop()


# A-4 --------------------------------------------------------------------------------


async def test_full_mode_with_cards_header_keeps_every_fact() -> None:
    """A-4: full mode hid every atomic fact, but the header shows only cards_top_k."""
    turns = ["Ana adopted a grey cat", "Ana lives in Lisbon", "Bob likes chess"]
    facts = [f"Ana event: Ana fact {n} about chess and Lisbon" for n in range(15)]
    eng = _engine({"cards": "header", "cards_top_k": 2})
    await eng.start()
    try:
        await _seed(eng, turns, facts)
        out = await eng.read("Ana grey cat", namespace="a", mode="full", budget_tokens=8000)
        assert out.mode == "full"
        joined = "\n".join(r.content for r in out.context.records)
        storage = eng._require_started()
        live = [
            r
            for r in await storage.list_records("a", "semantic")
            if "atomic_fact" in r.tags and await eng._live_view(r) is not None
        ]
        assert len(live) > 2  # more facts than the header shows
        for fact in live:
            marker = fact.content.split("Ana event: Ana ", 1)[1]
            assert joined.count(marker) == 1, marker
    finally:
        await eng.stop()


# A-7 --------------------------------------------------------------------------------


async def test_cards_fit_with_their_rendered_said_labels() -> None:
    """A-7: cards were fitted with ``[YYYY-MM-DD]`` but rendered ``[said YYYY-MM-DD]``."""
    facts = [f"Ana event: Ana cat fact number {n} aa" for n in range(10)]
    eng = _engine({"cards": "header", "cards_budget_share": 0.25})
    await eng.start()
    try:
        await _seed(eng, ["Ana adopted a cat"], facts)
        budget = 200
        out = await eng.read(
            "Ana cat fact number", namespace="a", mode="retrieve", top_k=3, budget_tokens=budget
        )
        [header] = _cards(out.context.records)
        assert "[said " in header.content
        assert estimate_tokens(header.content) <= int(budget * 0.25)
    finally:
        await eng.stop()


async def test_card_without_a_source_turn_has_no_date() -> None:
    """A-7: no parent, no "said" date; never the fact's own event date."""
    eng = _engine({"cards": "header"})
    await eng.start()
    try:
        await eng._deposit_mined_fact(
            "a", "Ana pet: Ana adopted a grey cat named Miso", "Ana", "pet", [],
            T0 + timedelta(days=9), "s1", kind="event",
        )  # fmt: skip
        out = await eng.read("Ana grey cat", namespace="a", mode="retrieve", budget_tokens=2000)
        [header] = _cards(out.context.records)
        line = header.content.splitlines()[1]
        assert line == "Ana: Ana adopted a grey cat named Miso"
    finally:
        await eng.stop()


# A-9 --------------------------------------------------------------------------------


def test_header_shares_must_leave_room_for_the_read() -> None:
    with pytest.raises(ConfigError):
        ReadConfig(
            cards="header",
            profile_header=True,
            cards_budget_share=0.6,
            profile_budget_share=0.5,
        )
    # Shares of a header that is off do not count.
    ReadConfig(cards="off", profile_header=True, cards_budget_share=0.9)
    ReadConfig(cards="header", profile_header=True)


# A-10 -------------------------------------------------------------------------------


@pytest.mark.parametrize("cards", ["off", "header"])
async def test_header_searches_use_the_session_ledger(cards: str) -> None:
    """A-10: the header searches passed no session_id, polluting the anonymous ledger."""
    eng = _engine(
        {"cards": cards},
        integrity={"enabled": True, "implicit_parents": "session", "admission_threshold": 0.0},
    )
    await eng.start()
    try:
        await _seed(
            eng,
            ["Ana adopted a grey cat named Miso yesterday", "weather was fine"],
            ["Ana pet: Ana adopted a grey cat named Miso"],
            parents=1,
        )
        eng._read_ledger.clear()
        await eng.assemble(
            "Ana grey cat Miso", namespace="a", top_k=3, budget_tokens=2000, session_id="s1"
        )
        assert list(eng._read_ledger) == [("a", "s1")]
        eng._read_ledger.clear()
        await eng.read("Ana grey cat Miso", namespace="a", mode="retrieve", session_id="s1")
        assert list(eng._read_ledger) == [("a", "s1")]
    finally:
        await eng.stop()
