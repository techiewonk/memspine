"""G3b: the profile header (``read.profile_header``) over H14 profile insights."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.lead import mentions_any, query_names
from memspine.core.policies.assembly import estimate_tokens

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


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


async def _seed(eng: Engine) -> list[str]:
    ids = []
    for i, text in enumerate(
        [
            "Caroline: I go to church every Sunday with my family",
            "Melanie: I painted a sunset at the lake last week",
            "Caroline: my faith helped me through the hard year",
            "Melanie: we went camping with the kids again",
        ]
    ):
        rec = await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )
        ids.append(rec.record_id)
    await eng._deposit_profile_reflection(
        "a", "Caroline is religious and values her church community", [ids[0], ids[2]], "s1"
    )
    await eng._deposit_profile_reflection(
        "a", "Melanie loves painting and outdoor family trips", [ids[1], ids[3]], "s1"
    )
    return ids


def _profile(records: list[Any]) -> list[Any]:
    return [r for r in records if constants.PROFILE_TAG in r.tags]


def test_query_names() -> None:
    assert query_names("Would Caroline be considered religious?") == ["Caroline"]
    assert query_names("What might John's financial status be?") == ["John"]
    assert query_names("what does she like") == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Tell me what Caroline likes", ["Caroline"]),
        ("Give me Melanie's hobbies", ["Melanie"]),
        ("Yes, and what about Jon?", ["Jon"]),
        ("No. When did Gina move?", ["Gina"]),
        ("I'm curious what Caroline paints", ["Caroline"]),
        ("Please list what Dave bought", ["Dave"]),
        ("Caroline said what to Caroline's sister?", ["Caroline"]),
        ("Summarize everything", []),
        ("Describe the trip", []),
    ],
)
def test_query_names_skips_imperatives_and_openers(query: str, expected: list[str]) -> None:
    """A-6: a capitalised opener is grammar, not a name."""
    assert query_names(query) == expected


def test_mentions_any_is_case_sensitive() -> None:
    assert mentions_any("Will moved to Paris", ["Will"])
    assert not mentions_any("she will move soon", ["Will"])


@pytest.mark.parametrize("mode", ["retrieve", "replay", "compose"])
async def test_about_block_names_the_person_and_fits(mode: str) -> None:
    eng = _engine(profile_header=True, profile_budget_share=0.15)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(
            "Would Caroline be considered religious?",
            namespace="a",
            mode=mode,
            top_k=2,
            budget_tokens=400,
        )
        [header] = _profile(out.context.records)
        assert out.context.records[out.context.boundary_index] is header
        assert header.content.startswith(constants.PROFILE_MARKER)
        assert "about Caroline" in header.content
        assert "Caroline is religious" in header.content
        assert "Melanie loves painting" not in header.content  # names Caroline only
        assert estimate_tokens(header.content) <= int(400 * 0.15)
        # The shown insight is not repeated in the main part.
        rest = " ".join(r.content for r in out.context.records if r is not header)
        assert "Caroline is religious" not in rest
    finally:
        await eng.stop()


async def test_without_a_name_the_most_relevant_insights_are_used() -> None:
    eng = _engine(profile_header=True, profile_budget_share=0.5)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(
            "who loves painting outdoors", namespace="a", mode="retrieve", budget_tokens=400
        )
        [header] = _profile(out.context.records)
        assert " about " not in header.content.splitlines()[0]
        assert "Melanie loves painting" in header.content
    finally:
        await eng.stop()


async def test_profile_follows_the_cards_header() -> None:
    eng = _engine(profile_header=True, cards="header")
    await eng.start()
    try:
        ids = await _seed(eng)
        await eng._deposit_mined_fact(
            "a",
            "Caroline event: Caroline goes to church every Sunday",
            "Caroline",
            "event",
            ids,
            T0,
            "s1",
            kind="event",
        )
        out = await eng.read(
            "Would Caroline be considered religious?",
            namespace="a",
            mode="retrieve",
            budget_tokens=600,
        )
        at = out.context.boundary_index
        assert constants.CARDS_TAG in out.context.records[at].tags
        assert constants.PROFILE_TAG in out.context.records[at + 1].tags
        headers = out.context.records[at : at + 2]
        assert out.context.tokens_used >= sum(estimate_tokens(h.content) for h in headers)
    finally:
        await eng.stop()


async def test_quarantined_insight_never_shows() -> None:
    eng = _engine(profile_header=True)
    await eng.start()
    try:
        await _seed(eng)
        storage = eng._require_started()
        [insight] = [
            r for r in await storage.list_records("a", "reflective") if "Caroline" in r.content
        ]
        await eng.quarantine(insight.record_id, namespace="a")
        out = await eng.read(
            "Would Caroline be considered religious?",
            namespace="a",
            mode="retrieve",
            budget_tokens=400,
        )
        assert all("Caroline is religious" not in h.content for h in _profile(out.context.records))
    finally:
        await eng.stop()


@pytest.mark.parametrize("mode", ["retrieve", "replay", "compose"])
async def test_off_is_byte_identical(mode: str) -> None:
    eng = _engine(profile_header=False)
    await eng.start()
    try:
        await _seed(eng)
        query = "Would Caroline be considered religious?"
        out = await eng.read(query, namespace="a", mode=mode, top_k=2, budget_tokens=400)
        plain = await eng._read_routed(query, "a", mode, eng._reply_budget(400), 2, 2, 3)
        assert [(r.record_id, r.content) for r in out.context.records] == [
            (r.record_id, r.content) for r in plain.context.records
        ]
        assert out.context.tokens_used == plain.context.tokens_used
        assert not _profile(out.context.records)
    finally:
        await eng.stop()
