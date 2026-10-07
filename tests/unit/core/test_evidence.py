"""W3 (plan v3.2, G02): the evidence-sufficiency signal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine.core.evidence import answer_type, contains_answer_type, evidence_signal
from memspine.core.records import MemoryRecord, SourceInfo

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _rec(text: str, day: int = 0, channel: str = "chat") -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=text,
        valid_from=T0 + timedelta(days=day),
        source=SourceInfo(role="user", channel=channel),
    )


@pytest.mark.parametrize(
    ("query", "kind"),
    [
        ("When did Caroline join the support group?", "date"),
        ("How many days ago did she leave?", "date"),
        ("How many times has Melanie gone to the beach?", "number"),
        ("Where did Caroline move from?", "place"),
        ("Which city did they visit?", "place"),
        ("Who gave Jon the dance shoes?", "name"),
        ("What is the name of her dog?", "name"),
        ("Why did Jon open a dance studio?", None),
    ],
)
def test_answer_type(query: str, kind: str | None) -> None:
    assert answer_type(query) == kind


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("Melanie: we went camping last weekend", "date", True),
        ("Melanie: we went camping by the lake", "date", False),
        ("Jon: I have three dogs", "number", True),
        ("Jon: I love dogs", "number", False),
        ("Caroline: I moved here from Sweden", "place", True),
        ("Caroline: I moved to a small town", "place", True),
        ("Caroline: I feel great today", "place", False),
        ("Gina: Maria gave me these shoes", "name", True),
        ("Gina: someone gave me these shoes", "name", False),
    ],
)
def test_contains_answer_type(text: str, kind: str, expected: bool) -> None:
    assert contains_answer_type(text, kind) is expected


def test_signal_scores_spread_and_days() -> None:
    scored = [
        (_rec("Caroline: I joined a support group last Tuesday", 0), 0.9),
        (_rec("Caroline: the group meets weekly", 1), 0.5),
        (_rec("Melanie: I went camping", 2), 0.3),
    ]
    sig = evidence_signal("When did Caroline join the support group?", scored)
    assert sig.top_score == 0.9
    assert sig.spread == pytest.approx(0.5)
    assert sig.distinct_days == 3
    assert sig.candidates == 3
    assert sig.answer_type == "date"
    assert sig.type_match is True
    assert sig.weak is False


def test_signal_weak_when_no_top_candidate_holds_the_answer_type() -> None:
    scored = [(_rec("Caroline: the support group helps me a lot"), 0.9)]
    sig = evidence_signal("When did Caroline join the support group?", scored)
    assert sig.type_match is False
    assert sig.weak is True


def test_signal_weak_below_threshold_only_when_set() -> None:
    scored = [(_rec("Jon: I opened the studio because I lost my job"), 0.2)]
    query = "Why did Jon open a dance studio?"
    assert evidence_signal(query, scored).weak is False
    assert evidence_signal(query, scored, weak_below=0.5).weak is True


def test_persona_is_not_evidence() -> None:
    scored = [(_rec("I am a helpful assistant", channel="persona"), 1.0)]
    sig = evidence_signal("Who is Jon?", scored)
    assert sig.candidates == 0
    assert sig.weak is True


def test_second_round_probe_collects_new_names_and_dates() -> None:
    """N04 (plan v3.2)."""
    from memspine.core.evidence import second_round_probe

    texts = [
        "Caroline: I went to the gallery with Maria on 12 March 2023",
        "Caroline: Maria loves the Tate",
    ]
    probe = second_round_probe("Who went to the gallery with Caroline?", texts)
    assert probe.split() == ["Maria", "March", "2023", "Tate"]
    assert second_round_probe("hi", []) == ""


async def test_second_round_read_is_well_formed() -> None:
    from memspine import Engine

    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "second_round": True, "evidence_weak_below": 10.0},
    )
    await eng.start()
    try:
        for text in ["Caroline: I went to the gallery with Maria", "Maria: the Tate was great"]:
            await eng.write(text, namespace="a", memory_type="episodic")
        out = await eng.read("Who went to the gallery?", namespace="a", mode="retrieve", top_k=2)
        assert out.context.records
    finally:
        await eng.stop()
