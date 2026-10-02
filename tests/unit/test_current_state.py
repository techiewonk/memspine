"""C4': deterministic current-state view and explicit retraction (FORK-A6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memspine import Engine
from memspine.core.records import RecordStatus, SourceInfo


def _engine(**extra: Any) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
        **extra,
    )


async def test_current_state_view_shows_current_and_history() -> None:
    eng = _engine(read={"current_state_view": True})
    await eng.start()
    try:
        await eng.write(
            "Caroline lives in Boston",
            namespace="a",
            entity="caroline",
            attribute="city",
            valid_from=datetime(2023, 1, 5, tzinfo=UTC),
        )
        await eng.write(
            "Caroline lives in Seattle",
            namespace="a",
            entity="caroline",
            attribute="city",
            valid_from=datetime(2023, 6, 1, tzinfo=UTC),
        )
        ctx = await eng.assemble("where does Caroline live", namespace="a")
        [line] = [r.content for r in ctx.records if "Seattle" in r.content]
        assert line.startswith("CURRENT (since 2023-06-01): Caroline lives in Seattle")
        assert "HISTORY (superseded): 2023-01-05 to 2023-06-01: Caroline lives in Boston" in line
    finally:
        await eng.stop()


async def test_view_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.write(
            "Caroline lives in Boston", namespace="a", entity="caroline", attribute="city"
        )
        ctx = await eng.assemble("where does Caroline live", namespace="a")
        assert all(not r.content.startswith("CURRENT") for r in ctx.records)
    finally:
        await eng.stop()


async def test_retract_ends_the_fact_with_no_successor() -> None:
    eng = _engine()
    await eng.start()
    try:
        fact = await eng.write(
            "Mel is allergic to peanuts",
            namespace="a",
            entity="mel",
            attribute="allergy",
            source=SourceInfo(role="user"),
        )
        await eng.retract(
            "mel",
            "allergy",
            namespace="a",
            reason="tested negative",
            source=SourceInfo(role="user"),
        )
        storage = eng._require_started()
        old = await storage.get_record(fact.record_id)
        assert old is not None and old.status is RecordStatus.ARCHIVED
        assert old.valid_to is not None and old.evolve_to is None  # ended, not replaced
        assert await storage.find_active_fact("a", "mel", "allergy") is None
    finally:
        await eng.stop()


async def test_low_trust_source_cannot_retract_an_operator_fact() -> None:
    eng = _engine()
    await eng.start()
    try:
        fact = await eng.write(
            "MFA is required for contractors",
            namespace="a",
            entity="policy",
            attribute="mfa",
            source=SourceInfo(role="operator"),
        )
        await eng.retract(
            "policy", "mfa", namespace="a", source=SourceInfo(role="tool", channel="web")
        )
        storage = eng._require_started()
        kept = await storage.get_record(fact.record_id)
        assert kept is not None and kept.status is RecordStatus.ACTIVATED  # trust gate held
    finally:
        await eng.stop()


async def test_temporal_leg_surfaces_the_dated_record() -> None:
    """C3': with the temporal leg on, a record whose event time is the date named
    in the query enters the results even when its wording shares nothing with it."""
    eng = _engine(read={"temporal_leg": True, "hybrid": False})
    await eng.start()
    try:
        target = await eng.write(
            "went hiking with the dog",
            namespace="a",
            valid_from=datetime(2023, 5, 7, tzinfo=UTC),
        )
        for i in range(20):
            await eng.write(
                f"filler note {i} about what happened at work",
                namespace="a",
                valid_from=datetime(2023, 8, 1 + i, tzinfo=UTC),
            )
        hits = await eng.search("what happened on 7 May 2023?", namespace="a", top_k=3)
        assert target.record_id in [r.record_id for r, _ in hits]
    finally:
        await eng.stop()


async def test_legs_off_is_unchanged() -> None:
    """Off (default), no C3' leg is built, so fusion sees exactly the base legs."""
    eng = _engine()
    await eng.start()
    try:
        await eng.write("note on 7 May 2023", namespace="a", entity="caroline")
        assert await eng._metadata_legs("a", "Caroline on 7 May 2023", 10) == []
    finally:
        await eng.stop()


async def test_resolve_relative_dates_annotates_assembled_records() -> None:
    """H1: a record written on 15 July 2023 that says "last Friday" is assembled with the
    absolute date; the stored record is unchanged; off by default."""
    for on in (True, False):
        eng = _engine(read={"resolve_relative_dates": on, "hybrid": False})
        await eng.start()
        try:
            rec = await eng.write(
                "Caroline went to the support group last Friday",
                namespace="a",
                valid_from=datetime(2023, 7, 15, tzinfo=UTC),
            )
            ctx = await eng.assemble("support group", namespace="a")
            [line] = [r.content for r in ctx.records if r.record_id == rec.record_id]
            if on:
                assert line == "Caroline went to the support group last Friday [= Fri 2023-07-14]"
            else:
                assert line == "Caroline went to the support group last Friday"
            assert rec.content == "Caroline went to the support group last Friday"
        finally:
            await eng.stop()


async def test_candidate_pool_and_relative_floor() -> None:
    """H11/H4: a wider candidate pool lets the budget, not K, decide how much enters;
    the relative floor then removes weak candidates. Defaults are unchanged."""
    counts = {}
    for name, read in {
        "default": {"hybrid": False},
        "pool": {"hybrid": False, "candidate_pool": 4},
        "pool+floor": {"hybrid": False, "candidate_pool": 4, "assembly": {"relative_floor": 0.99}},
    }.items():
        eng = _engine(read=read)
        await eng.start()
        try:
            for i in range(12):
                await eng.write(f"note {i} about the beach trip", namespace="a")
            ctx = await eng.assemble("beach trip", namespace="a", top_k=2, budget_tokens=4096)
            counts[name] = len(ctx.records)
        finally:
            await eng.stop()
    assert counts["default"] == 2
    assert counts["pool"] == 8
    assert counts["pool+floor"] < counts["pool"]


async def test_dated_render_and_time_order_for_ordering_questions() -> None:
    eng = _engine(read={"hybrid": False, "render": "dated", "order_by_time_for_ordering": True})
    await eng.start()
    try:
        for day in (20, 3, 11):
            await eng.write(
                f"Melanie read a new book on day {day}",
                namespace="a",
                memory_type="episodic",
                valid_from=datetime(2023, 5, day, tzinfo=UTC),
            )
        ctx = await eng.assemble("What is the latest book Melanie read?", namespace="a")
        lines = [r.content for r in ctx.records]
        assert lines == sorted(lines)  # dated prefixes sort by date
        assert lines[0].startswith("[2023-05-03 Wed] ")
    finally:
        await eng.stop()


async def test_contested_fact_keeps_one_current_and_shows_the_dispute() -> None:
    """H9: two equal-standing statements on one key at the same event time are both kept;
    exactly one stays current; the current-state view flags the dispute."""
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True, "policies": {"conflict": {"contest_ties": True}}}},
        read={"current_state_view": True, "hybrid": False},
    )
    await eng.start()
    try:
        when = datetime(2023, 5, 1, tzinfo=UTC)
        await eng.write(
            "Ana lives in Lyon", namespace="a", entity="ana", attribute="city", valid_from=when
        )
        await eng.write(
            "Ana lives in Nice", namespace="a", entity="ana", attribute="city", valid_from=when
        )
        facts = [r for r in await eng.retrieve(namespace="a", memory_type="semantic")]
        current = [r for r in facts if r.valid_to is None]
        assert len(current) == 1 and "disputed" in current[0].tags
        assert {"Ana lives in Lyon", "Ana lives in Nice"} <= {r.content for r in facts}
        ctx = await eng.assemble("where does Ana live", namespace="a")
        assert any("[DISPUTED" in r.content for r in ctx.records)
    finally:
        await eng.stop()


async def test_core_terms_leg_adds_a_lexical_probe() -> None:
    """H13: the leg is built only when enabled and the lexical store exists."""
    for on in (False, True):
        eng = _engine(read={"core_terms_leg": on})
        await eng.start()
        try:
            await eng.write("Melanie went to the beach with her kids", namespace="a")
            legs = await eng._metadata_legs(
                "a", "How many times has Melanie gone to the beach?", 10
            )
            assert (len(legs) == 1) is on
        finally:
            await eng.stop()
