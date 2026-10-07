"""ADR-055: question-shape gates on the replay read path (A1, A2, B2, B3, D1, H12)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core import records as records_module
from memspine.core.event_date import label_span
from memspine.core.lead import card_line
from memspine.core.policies.assembly import estimate_tokens
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.core.temporal_query import temporal_leg
from memspine.workers.list_cards import ListCard

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

TURNS = [
    ("Melanie: I went camping with the kids last weekend", 0),
    ("Caroline: I started a new painting of a lake sunset", 3),
    ("Melanie: we ran a charity race for mental health in May", 9),
    ("Melanie: I went camping at the beach again with the family", 20),
    ("Caroline: I joined a support group last Tuesday", 33),
    ("Melanie: the kids loved the pottery class yesterday", 40),
]

#: (text, entity, parent turn index, day offset, happened, said)
MINED = [
    ("Melanie event: went camping with the kids", "Melanie", 0, 0, "2023-04-29", "2023-05-01"),
    ("Melanie event: ran a charity race for mental health", "Melanie", 2, 9, None, None),
    ("Melanie event: went camping at the beach", "Melanie", 3, 20, None, None),
    ("Caroline event: joined a support group", "Caroline", 4, 33, "2023-05-30", "2023-06-03"),
]

AGGREGATE = "What activities has Melanie done?"
COUNT = "How many times did Melanie go camping?"
LOOKUP = "Where did Melanie go camping with the family?"
TEMPORAL = "When did Caroline join the support group?"


@pytest.fixture(autouse=True)
def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid

    counter = iter(range(1, 1_000_000))
    monkeypatch.setattr(records_module, "uuid4", lambda: uuid.UUID(int=next(counter)))


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "reflective": {"enabled": True},
        },
        read={"hybrid": False, "record_access": False, **read},
    )


async def _seed(eng: Engine, ns: str = "a") -> None:
    ids = []
    for text, day in TURNS:
        rec = await eng.write(
            text, namespace=ns, memory_type="episodic", valid_from=T0 + timedelta(day)
        )
        ids.append(rec.record_id)
    facts = []
    for text, entity, parent, day, happened, said in MINED:
        facts.append(
            await eng._deposit_mined_fact(
                ns,
                text,
                entity,
                "event",
                [ids[parent]],
                T0 + timedelta(days=day, hours=2),
                "s1",
                kind="event",
                happened=happened,
                said=said,
                persons=[entity],
            )
        )
    melanie = [f for f in facts if f.entity == "Melanie"]
    await eng._deposit_list_card(
        ns,
        ListCard(
            person="Melanie",
            label="activities",
            text="Melanie — activities: camping (2023-05); charity race (2023-05); "
            "camping at the beach (2023-05)",
            parents=sorted(f.record_id for f in melanie),
            valid_from=max(f.valid_from for f in melanie),
            tags=[constants.LIST_CARD_TAG, "atomic_fact", "person:melanie", "topic:activity"],
        ),
        [],
    )
    await eng._deposit_profile_reflection(
        ns, "Melanie loves camping and outdoor family trips", [ids[0], ids[3]], "s1"
    )
    await eng._deposit_profile_reflection(
        ns, "Caroline is creative and joined a support group", [ids[1], ids[4]], "s1"
    )


def _tagged(out: Any, tag: str) -> list[MemoryRecord]:
    return [r for r in out.context.records if tag in r.tags]


# -- config ------------------------------------------------------------------------


def test_new_keys_default_off() -> None:
    cfg = ReadConfig()
    assert cfg.aggregate_in_replay is False
    assert cfg.list_cards_only_aggregate is False
    assert cfg.temporal_leg_event_dates is False
    assert cfg.cards_temporal == "skip"
    assert cfg.profile_skip_temporal is False
    assert cfg.lead_budget_share is None


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_lead_budget_share_is_bounded(bad: float) -> None:
    with pytest.raises(ValueError):
        ReadConfig(lead_budget_share=bad)


# -- A1: wider pool for list / count questions in replay -----------------------------


async def _replay_top_k(monkeypatch: pytest.MonkeyPatch, query: str, **read: Any) -> list[int]:
    eng = _engine(**read)
    seen: list[int] = []
    inner = eng._assemble_core

    async def spy(q: str, ns: str, budget: int, top_k: int, **kw: Any) -> Any:
        seen.append(top_k)
        return await inner(q, ns, budget, top_k, **kw)

    monkeypatch.setattr(eng, "_assemble_core", spy)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(query, namespace="a", mode="replay", top_k=3, budget_tokens=300)
        assert out.mode == "replay"  # never compose rendering
        assert out.context.tokens_used <= 300
    finally:
        await eng.stop()
    return seen


@pytest.mark.parametrize(
    ("query", "on", "agg_k", "expected"),
    [
        (AGGREGATE, False, 20, 3),
        (AGGREGATE, True, 20, 20),
        (COUNT, True, 20, 20),
        (LOOKUP, True, 20, 3),
        (TEMPORAL, True, 20, 3),
        (AGGREGATE, True, None, 3),
    ],
)
async def test_aggregate_in_replay_widens_only_list_and_count_reads(
    monkeypatch: pytest.MonkeyPatch, query: str, on: bool, agg_k: int | None, expected: int
) -> None:
    seen = await _replay_top_k(monkeypatch, query, aggregate_in_replay=on, aggregate_top_k=agg_k)
    assert seen == [expected]


async def test_aggregate_in_replay_leaves_retrieve_mode_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(aggregate_in_replay=True, aggregate_top_k=20)
    seen: list[int] = []
    inner = eng._assemble_core

    async def spy(q: str, ns: str, budget: int, top_k: int, **kw: Any) -> Any:
        seen.append(top_k)
        return await inner(q, ns, budget, top_k, **kw)

    monkeypatch.setattr(eng, "_assemble_core", spy)
    await eng.start()
    try:
        await _seed(eng)
        await eng.read(AGGREGATE, namespace="a", mode="retrieve", top_k=3)
    finally:
        await eng.stop()
    assert seen == [3]


# -- A2: list cards only for list / count questions -------------------------------------


async def _cards(query: str, **read: Any) -> str:
    eng = _engine(cards="header", **read)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(query, namespace="a", mode="replay", top_k=3, budget_tokens=600)
        return "\n".join(r.content for r in _tagged(out, constants.CARDS_TAG))
    finally:
        await eng.stop()


async def test_list_card_shown_to_a_lookup_when_the_gate_is_off() -> None:
    assert "Melanie — activities" in await _cards(LOOKUP)


@pytest.mark.parametrize(("query", "shown"), [(LOOKUP, False), (AGGREGATE, True), (COUNT, True)])
async def test_list_cards_only_aggregate(query: str, shown: bool) -> None:
    text = await _cards(query, list_cards_only_aggregate=True)
    assert ("Melanie — activities" in text) is shown
    if not shown:
        assert "camping" in text  # the other cards still show


# -- B2: the temporal leg matches happened dates -----------------------------------------


def _rec(said: datetime, *tags: str) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content="fact",
        source=SourceInfo(role="user", channel="test"),
        valid_from=said,
        tags=list(tags),
    )


def test_label_span() -> None:
    assert label_span("2022-03-16") == (date(2022, 3, 16), date(2022, 3, 16))
    assert label_span("2022-02") == (date(2022, 2, 1), date(2022, 2, 28))
    assert label_span("2022-12") == (date(2022, 12, 1), date(2022, 12, 31))
    assert label_span("2022") == (date(2022, 1, 1), date(2022, 12, 31))
    assert label_span("2023-06-02..2023-06-08") == (date(2023, 6, 2), date(2023, 6, 8))
    assert label_span("2023-06-08..2023-06-02") is None
    assert label_span("soon") is None


def test_temporal_leg_finds_an_event_by_its_happened_date() -> None:
    fact = _rec(datetime(2022, 3, 20, tzinfo=UTC), "happened:2022-03-16")
    query = "What did she do on March 16, 2022?"
    assert temporal_leg(query, [fact], 5) == []
    [hit] = temporal_leg(query, [fact], 5, event_dates=True)
    assert hit.record_id == fact.record_id


def test_temporal_leg_event_dates_order_is_deterministic() -> None:
    said_in = _rec(datetime(2022, 3, 16, 12, tzinfo=UTC))
    happened_day = _rec(datetime(2022, 3, 20, tzinfo=UTC), "happened:2022-03-16")
    happened_week = _rec(datetime(2022, 3, 25, tzinfo=UTC), "happened:2022-03-14..2022-03-18")
    happened_month = _rec(datetime(2022, 4, 2, tzinfo=UTC), "happened:2022-03")
    outside = _rec(datetime(2022, 4, 2, tzinfo=UTC), "happened:2022-02")
    pool = [outside, happened_month, happened_week, happened_day, said_in]
    query = "What happened on 16 March 2022?"
    first = [h.record_id for h in temporal_leg(query, pool, 10, event_dates=True)]
    again = [h.record_id for h in temporal_leg(query, list(reversed(pool)), 10, event_dates=True)]
    assert first == again
    # said at midday (the span's middle) first; the three happened spans start at the
    # same clipped instant and fall back to chrono_key; February never enters.
    assert first[0] == said_in.record_id
    assert set(first) == {r.record_id for r in pool if r is not outside}
    # off: the said date only
    assert [h.record_id for h in temporal_leg(query, pool, 10)] == [said_in.record_id]


async def test_engine_passes_the_event_dates_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    import memspine.engine as engine_module

    calls: list[bool] = []
    real = engine_module.temporal_leg

    def spy(query: str, records: Any, top_k: int, *, event_dates: bool = False) -> Any:
        calls.append(event_dates)
        return real(query, records, top_k, event_dates=event_dates)

    monkeypatch.setattr(engine_module, "temporal_leg", spy)
    for on in (False, True):
        eng = _engine(temporal_leg=True, temporal_leg_event_dates=on)
        await eng.start()
        try:
            await _seed(eng)
            out = await eng.read(
                "What did Melanie do on 29 April 2023?", namespace="a", mode="replay", top_k=3
            )
            contents = " ".join(r.content for r in out.context.records)
            if on:  # the mined fact said 05-01 is found by its happened day
                assert "went camping with the kids" in contents
        finally:
            await eng.stop()
    assert calls == [False, True]


# -- B3: cards on temporal questions show event dates -------------------------------------


def test_card_line_happened_first() -> None:
    rec = _rec(datetime(2023, 6, 3, tzinfo=UTC), "happened:2023-05-30")
    said = datetime(2023, 6, 3, tzinfo=UTC)
    assert card_line(rec, said, happened="2023-05-30", happened_first=True).startswith(
        "[happened 2023-05-30 · said 2023-06-03] "
    )
    assert card_line(rec, said, happened="2023-06-03", happened_first=True).startswith(
        "[happened 2023-06-03 · said 2023-06-03] "
    )
    assert card_line(rec, None, happened="2023-05-30", happened_first=True).startswith(
        "[happened 2023-05-30] "
    )
    # off: the #29 rendering, unchanged
    assert card_line(rec, said, happened="2023-05-30").startswith(
        "[said 2023-06-03 · happened 2023-05-30] "
    )


async def test_cards_temporal_skip_keeps_todays_behaviour() -> None:
    assert await _cards(TEMPORAL, cards_skip_temporal=True) == ""
    assert "support group" in await _cards(TEMPORAL)  # no skip: cards as usual


@pytest.mark.parametrize("skip", [False, True])
async def test_cards_temporal_event_dates_shows_only_happened_cards(skip: bool) -> None:
    text = await _cards(TEMPORAL, cards_temporal="event_dates", cards_skip_temporal=skip)
    lines = text.splitlines()
    assert lines[0] == constants.CARDS_MARKER
    assert lines[1:], "the header is shown"
    for line in lines[1:]:
        assert line.startswith("[happened "), line
    assert "[happened 2023-05-30 · said 2023-06-03] Caroline: joined a support group" in lines
    assert "Melanie — activities" not in text  # a list card has no happened date
    assert "charity race" not in text  # no happened date


async def test_cards_temporal_event_dates_leaves_other_questions_alone() -> None:
    assert await _cards(LOOKUP, cards_temporal="event_dates") == await _cards(LOOKUP)


# -- D1: no profile header on temporal questions ------------------------------------------


@pytest.mark.parametrize("packing", [False, True])
@pytest.mark.parametrize(("query", "shown"), [(TEMPORAL, False), (LOOKUP, True)])
async def test_profile_skip_temporal(packing: bool, query: str, shown: bool) -> None:
    async def profile(**read: Any) -> list[MemoryRecord]:
        eng = _engine(profile_header=not packing, profile_header_packing=packing, **read)
        await eng.start()
        try:
            await _seed(eng)
            out = await eng.read(query, namespace="a", mode="replay", top_k=3, budget_tokens=600)
            return _tagged(out, constants.PROFILE_TAG)
        finally:
            await eng.stop()

    assert await profile()  # off: the profile header shows for both
    assert bool(await profile(profile_skip_temporal=True)) is shown


# -- H12: one cap on all lead blocks --------------------------------------------------------


async def _leads(query: str, budget: int = 600, **read: Any) -> tuple[Any, list[MemoryRecord]]:
    eng = _engine(cards="header", profile_header=True, count_timeline=True, **read)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(query, namespace="a", mode="replay", top_k=3, budget_tokens=budget)
        leads = [r for r in out.context.records if constants.LEAD_TAG in r.tags]
        return out, leads
    finally:
        await eng.stop()


def _cost(blocks: list[MemoryRecord]) -> int:
    return sum(estimate_tokens(b.content) for b in blocks)


async def test_lead_budget_share_none_is_unchanged() -> None:
    a, _ = await _leads(LOOKUP)
    b, _ = await _leads(LOOKUP, lead_budget_share=None)
    assert [r.content for r in a.context.records] == [r.content for r in b.context.records]


async def test_lead_budget_share_drops_profile_before_cards() -> None:
    _, uncapped = await _leads(LOOKUP)
    cards = _tagged_list(uncapped, constants.CARDS_TAG)
    profile = _tagged_list(uncapped, constants.PROFILE_TAG)
    assert cards and profile
    # a cap that fits the cards but not cards + profile
    share = (_cost(cards) + 1) / _reply_budget(600)
    assert _cost(cards) + _cost(profile) > int(_reply_budget(600) * share)
    out, capped = await _leads(LOOKUP, lead_budget_share=share)
    assert _tagged_list(capped, constants.CARDS_TAG)
    assert not _tagged_list(capped, constants.PROFILE_TAG)
    assert _cost(capped) <= int(_reply_budget(600) * share)
    assert out.context.tokens_used <= 600


async def test_lead_budget_share_tiny_drops_every_header() -> None:
    _, capped = await _leads(LOOKUP, lead_budget_share=0.001)
    assert capped == []


async def test_lead_budget_share_caps_the_count_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(cards="header", profile_header=True, count_timeline=True, lead_budget_share=0.05)
    shares: list[int] = []
    inner = eng._count_section

    def spy(ns: str, q: str, assembled: Any, allowance: int, headers: Any) -> Any:
        shares.append(allowance)
        return inner(ns, q, assembled, allowance, headers)

    monkeypatch.setattr(eng, "_count_section", spy)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(COUNT, namespace="a", mode="replay", top_k=3, budget_tokens=2000)
    finally:
        await eng.stop()
    cap = int(eng._reply_budget(2000) * 0.05)
    [allowance] = shares
    assert 0 < allowance <= cap
    leads = [r for r in out.context.records if constants.LEAD_TAG in r.tags]
    assert _cost(leads) <= cap


def _tagged_list(blocks: list[MemoryRecord], tag: str) -> list[MemoryRecord]:
    return [b for b in blocks if tag in b.tags]


def _reply_budget(budget: int) -> int:
    reserve = ReadConfig().reply_reserve_tokens  # the core template keeps the default
    return max(1, budget - reserve) if reserve else budget


async def test_lead_budget_share_applies_to_assemble() -> None:
    eng = _engine(cards="header", profile_header=True, lead_budget_share=0.001)
    await eng.start()
    try:
        await _seed(eng)
        ctx = await eng.assemble(LOOKUP, namespace="a", top_k=3, budget_tokens=600)
    finally:
        await eng.stop()
    assert not [r for r in ctx.records if constants.LEAD_TAG in r.tags]


# -- ADR-055 addendum: cards header and mined facts only for list / count questions ----


def _facts(out: Any) -> list[MemoryRecord]:
    return [r for r in out.context.records if "atomic_fact" in r.tags]


def _view(out: Any) -> list[tuple[str, list[str], str]]:
    return [(r.memory_type, sorted(r.tags), r.content) for r in out.context.records]


async def _read_all(
    query: str, modes: tuple[str, ...] = ("replay",), budget: int = 600, **read: Any
) -> dict[str, Any]:
    eng = _engine(**read)
    await eng.start()
    try:
        await _seed(eng)
        out: dict[str, Any] = {
            mode: await eng.read(query, namespace="a", mode=mode, top_k=3, budget_tokens=budget)
            for mode in modes
        }
        out["assemble"] = await eng.assemble(query, namespace="a", top_k=3, budget_tokens=budget)
        return out
    finally:
        await eng.stop()


def test_cards_only_aggregate_defaults_off() -> None:
    assert ReadConfig().cards_only_aggregate is False


@pytest.mark.parametrize("query", [AGGREGATE, COUNT])
async def test_cards_only_aggregate_keeps_the_header_for_list_and_count(query: str) -> None:
    out = await _read_all(query, cards="header", cards_only_aggregate=True)
    off = await _read_all(query, cards="header")
    assert _tagged(out["replay"], constants.CARDS_TAG)
    assert _view(out["replay"]) == _view(off["replay"])


MODES = ("replay", "retrieve", "compose", "auto", "full")


async def test_lookup_has_header_and_facts_when_the_key_is_off() -> None:
    off = await _read_all(LOOKUP, MODES, cards="header")
    assert _tagged(off["replay"], constants.CARDS_TAG)


async def test_cards_only_aggregate_lookup_reads_raw_turns_only() -> None:
    out = await _read_all(LOOKUP, MODES, cards="header", cards_only_aggregate=True)
    for mode in MODES:
        result = out[mode]
        assert not _tagged(result, constants.CARDS_TAG), mode
        assert not _facts(result), mode
        assert result.context.records, mode
    assert not [r for r in out["assemble"].records if "atomic_fact" in r.tags]
    assert not [r for r in out["assemble"].records if constants.CARDS_TAG in r.tags]


async def test_cards_only_aggregate_full_read_matches_mining_off() -> None:
    """Small namespace: ``auto``/``full`` hold every live record, minus mined facts."""
    out = await _read_all(LOOKUP, ("full",), budget=4000, cards="header", cards_only_aggregate=True)
    contents = [r.content for r in out["full"].context.records]
    assert any("Melanie: I went camping at the beach" in c for c in contents)
    assert not _facts(out["full"])


@pytest.mark.parametrize(
    "temporal_cfg",
    [
        {},
        {"cards_skip_temporal": True},
        {"cards_temporal": "event_dates"},
        {"cards_skip_temporal": True, "cards_temporal": "event_dates"},
    ],
)
async def test_cards_only_aggregate_leaves_temporal_questions_alone(
    temporal_cfg: dict[str, Any],
) -> None:
    on = await _read_all(TEMPORAL, MODES, cards="header", cards_only_aggregate=True, **temporal_cfg)
    off = await _read_all(TEMPORAL, MODES, cards="header", **temporal_cfg)
    for mode in MODES:
        assert _view(on[mode]) == _view(off[mode]), mode
    assert [r.content for r in on["assemble"].records] == [
        r.content for r in off["assemble"].records
    ]


async def test_cards_only_aggregate_needs_the_cards_header() -> None:
    """Without ``cards: header`` there is no mining stack to gate: byte-identical."""
    on = await _read_all(LOOKUP, MODES, cards_only_aggregate=True)
    off = await _read_all(LOOKUP, MODES)
    for mode in MODES:
        assert _view(on[mode]) == _view(off[mode]), mode


# -- ADR-055 addendum: a date question that skips the cards header hides mined facts ----


def test_cards_skip_hides_facts_defaults_off() -> None:
    assert ReadConfig().cards_skip_hides_facts is False


async def test_skipped_date_question_shows_mined_facts_when_the_key_is_off() -> None:
    off = await _read_all(TEMPORAL, ("replay",), cards="header", cards_skip_temporal=True)
    assert not _tagged(off["replay"], constants.CARDS_TAG)
    assert _facts(off["replay"])


async def test_cards_skip_hides_facts_date_question_reads_raw_turns() -> None:
    out = await _read_all(
        TEMPORAL, MODES, cards="header", cards_skip_temporal=True, cards_skip_hides_facts=True
    )
    for mode in MODES:
        result = out[mode]
        assert not _tagged(result, constants.CARDS_TAG), mode
        assert not _facts(result), mode
        assert result.context.records, mode
    assert not [r for r in out["assemble"].records if "atomic_fact" in r.tags]


@pytest.mark.parametrize(
    ("query", "read"),
    [
        (LOOKUP, {"cards_skip_temporal": True}),
        (TEMPORAL, {}),
        (TEMPORAL, {"cards_skip_temporal": True, "cards_temporal": "event_dates"}),
    ],
)
async def test_cards_skip_hides_facts_changes_nothing_else(
    query: str, read: dict[str, Any]
) -> None:
    on = await _read_all(query, MODES, cards="header", cards_skip_hides_facts=True, **read)
    off = await _read_all(query, MODES, cards="header", **read)
    for mode in MODES:
        assert _view(on[mode]) == _view(off[mode]), mode


# -- F5 (plan v3.2): a verbatim question reads raw turns only ------------------------

VERBATIM = "What did Melanie say about camping at the beach?"


def test_verbatim_raw_only_defaults_off() -> None:
    assert ReadConfig().verbatim_raw_only is False


async def test_verbatim_question_gets_headers_when_the_key_is_off() -> None:
    off = await _read_all(VERBATIM, ("replay",), cards="header", profile_header=True)
    assert _tagged(off["replay"], constants.CARDS_TAG)


async def test_verbatim_raw_only_drops_every_header_and_mined_fact() -> None:
    out = await _read_all(
        VERBATIM, MODES, cards="header", profile_header=True, verbatim_raw_only=True
    )
    for mode in MODES:
        result = out[mode]
        assert not _tagged(result, constants.CARDS_TAG), mode
        assert not _tagged(result, constants.PROFILE_TAG), mode
        assert not _facts(result), mode
        assert result.context.records, mode


async def test_verbatim_raw_only_changes_nothing_for_other_questions() -> None:
    on = await _read_all(LOOKUP, MODES, cards="header", profile_header=True, verbatim_raw_only=True)
    off = await _read_all(LOOKUP, MODES, cards="header", profile_header=True)
    for mode in MODES:
        assert _view(on[mode]) == _view(off[mode]), mode
