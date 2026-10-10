"""I52 graded sensitivity, I53 participants / viewer / visibility, I48 inferred provenance.

All three are opt-in: with the keys off a record is stored and read exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from memspine import Engine
from memspine.config.schema import MemspineConfig, ReadConfig, WriteConfig
from memspine.core.inference import (
    INFERRED_TAG,
    classify,
    containment,
    support_count,
    support_tags,
)
from memspine.core.policies.conflict import ConflictPolicy, ConflictVerdict
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.core.sensitivity import (
    grade_rank,
    grade_text,
    label_of,
    label_tags,
    passes_gate,
    query_topics,
)
from memspine.core.visibility import (
    inherit_tags,
    participant_tags,
    visibility_of,
    visibility_tag,
    visible_to,
)
from memspine.services.decision.decider import Decision, HeuristicDecider

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

GENDER = "I am non-binary and my pronouns are they/them"
HOBBY = "I love hiking and pottery on weekends"


class FakeDecider:
    decider_id = "fake"

    def __init__(self, answers: dict[str, tuple[str, float | None]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision:
        self.calls.append(task)
        label, conf = self.answers[task]
        return Decision(label, conf, None, task, self.decider_id)


def _engine(write: dict[str, Any] | None = None, **read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
        write=write or {},
    )


def _rec(content: str, tags: list[str] | None = None, **kw: Any) -> MemoryRecord:
    return MemoryRecord(
        namespace="a", memory_type="semantic", content=content, tags=tags or [], **kw
    )


# -- defaults ------------------------------------------------------------------------------


def test_everything_is_off_by_default() -> None:
    cfg = MemspineConfig()
    assert cfg.write == WriteConfig()
    assert (cfg.write.sensitivity, cfg.write.participants, cfg.write.inferred) == (
        "off",
        "off",
        "off",
    )
    read = ReadConfig()
    assert (read.sensitivity_gate, read.inferred_gate, read.inferred_min_support) == (
        "off",
        "off",
        2,
    )


async def test_default_write_adds_no_labels() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await eng.write(GENDER, namespace="a", memory_type="episodic")
        assert rec.tags == []
    finally:
        await eng.stop()


# -- I52: grade and gate -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "grade", "category"),
    [
        (GENDER, "high", "sexuality_gender"),
        ("My gender identity is something I am still working out", "high", "sexuality_gender"),
        ("My doctor said my condition needs surgery", "high", "health"),
        ("I am a muslim and observe ramadan", "high", "religion"),
        ("My password is hunter2 for the router", "high", "credentials"),
        ("I live at 42 Elm Street near the park", "medium", "location"),
        ("I vote for the labour party", "medium", "politics"),
        ("My salary is paid on the 25th", "medium", "finance"),
        ("My lawyer called about the court date", "medium", "legal"),
    ],
)
def test_grade_text_table(text: str, grade: str, category: str) -> None:
    label = grade_text(text)
    assert label.grade == grade
    assert category in label.categories


def test_ordinary_text_is_not_graded() -> None:
    assert grade_text(HOBBY).grade == "none"
    assert label_tags(grade_text(HOBBY)) == []


def test_tags_round_trip_and_legacy_sensitive_tags_grade() -> None:
    label = grade_text("My doctor said my condition needs surgery and I am a christian")
    assert label_of(label_tags(label)) == label
    legacy = label_of(["sensitive:health"])
    assert legacy.grade == "high" and legacy.categories == ("health",)
    assert label_of(["unrelated"]).grade == "none"
    assert grade_rank("high") > grade_rank("medium") > grade_rank("low") > grade_rank("none")


def test_gate_bar_rises_with_the_grade() -> None:
    high = grade_text(GENDER)
    # a generic question never opens a high-grade memory, whatever else it says
    for q in ("Recommend a gift for my sister", "What should I cook tonight?", "Plan my weekend"):
        assert not passes_gate(high, GENDER, None, q)
    # a question about the topic does
    assert passes_gate(high, GENDER, None, "What is my gender identity?")
    assert passes_gate(high, GENDER, None, "Which pronouns should you use for me?")
    # subject alone is not enough for high, it is for medium
    med = grade_text("Sam gets paid a salary monthly")
    assert passes_gate(med, "Sam gets paid a salary monthly", "Sam", "What does Sam like to eat?")
    assert not passes_gate(high, GENDER, "Sam", "What does Sam like to eat?")
    # two shared content words open a high-grade record, one does not
    doc = "My doctor said my condition needs surgery next spring"
    assert not passes_gate(("high", ("other",)), doc, None, "Is the weather nice?")
    assert not passes_gate(("high", ("other",)), doc, None, "When is spring?")
    assert passes_gate(("high", ("other",)), doc, None, "When is the next spring surgery?")
    assert query_topics("What did the doctor say?") == {"health"}
    # low and none are never gated
    assert passes_gate(("low", ()), "x", None, "anything")
    assert passes_gate(("none", ()), "x", None, "anything")


async def test_write_tags_label_and_category_only_and_never_logs_the_text() -> None:
    eng = _engine({"sensitivity": "heuristic"})
    await eng.start()
    try:
        with capture_logs() as logs:
            rec = await eng.write(GENDER, namespace="a", memory_type="episodic")
        assert "sens:high" in rec.tags and "sensc:sexuality_gender" in rec.tags
        assert all(t.startswith(("sens:", "sensc:")) for t in rec.tags)
        assert rec.pii_tier.value == "high"
        assert "non-binary" not in str(logs) and "they/them" not in str(logs)
        graded = [e for e in logs if e["event"] == "memory.sensitivity_graded"]
        assert graded and graded[0]["grade"] == "high"
        plain = await eng.write(HOBBY, namespace="a", memory_type="episodic")
        assert plain.tags == []
    finally:
        await eng.stop()


async def _seed_sensitive(eng: Engine) -> None:
    for i, text in enumerate([HOBBY, GENDER, "I adopted a dog named Rex", "I like jazz on Sunday"]):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )


async def test_generic_question_does_not_pull_in_a_high_sensitivity_memory() -> None:
    """OP-Bench-style irrelevance: the user's gender identity must stay out of a generic ask."""
    eng = _engine({"sensitivity": "heuristic"}, sensitivity_gate="on")
    await eng.start()
    try:
        await _seed_sensitive(eng)
        generic = "Can you recommend a birthday gift for my sister?"
        hits = await eng.search(generic, namespace="a", top_k=10)
        assert hits and not any("non-binary" in r.content for r, _ in hits)
        for mode in ("full", "retrieve", "replay", "auto"):
            out = await eng.read(generic, namespace="a", mode=mode)
            assert "non-binary" not in str(out.context.records)
            assert out.context.records, mode  # the gate drops it, not the whole context
        about = await eng.search(
            "What are my pronouns and gender identity?", namespace="a", top_k=10
        )
        assert any("non-binary" in r.content for r, _ in about)
        out = await eng.read(
            "What are my pronouns and gender identity?", namespace="a", mode="full"
        )
        assert any("non-binary" in r.content for r in out.context.records)
    finally:
        await eng.stop()


async def test_gate_off_returns_the_sensitive_memory_as_today() -> None:
    eng = _engine({"sensitivity": "heuristic"})  # graded, but read.sensitivity_gate stays off
    await eng.start()
    try:
        await _seed_sensitive(eng)
        hits = await eng.search("Can you recommend a birthday gift?", namespace="a", top_k=10)
        assert any("non-binary" in r.content for r, _ in hits)
    finally:
        await eng.stop()


async def test_decider_raises_an_ungraded_text_and_opens_the_scope_gate() -> None:
    fake = FakeDecider({"sensitivity": ("sensitive", 0.9), "sensitivity_scope": ("about", 0.9)})
    eng = _engine({"sensitivity": "decider"}, sensitivity_gate="decider", decider="opendecider")
    eng.set_decider(fake)
    await eng.start()
    try:
        rec = await eng.write("Ana told me about her private worries", namespace="a")
        assert "sens:medium" in rec.tags and "sensc:other" in rec.tags
        low = FakeDecider({"sensitivity": ("sensitive", 0.4)})
        eng.set_decider(low)
        again = await eng.write("Ana told me a different private story", namespace="a")
        assert again.tags == []  # not sure enough: the lexicon (none) stands
        # the scope task opens the gate for a message the cues miss
        eng.set_decider(fake)
        await eng.write(GENDER, namespace="a", memory_type="episodic")
        hits = await eng.search("Tell me everything you know about me", namespace="a", top_k=10)
        assert any("non-binary" in r.content for r, _ in hits)
        assert "sensitivity_scope" in fake.calls
    finally:
        await eng.stop()


async def test_decider_never_lowers_a_lexicon_grade() -> None:
    fake = FakeDecider({"sensitivity": ("not_sensitive", 0.99)})
    eng = _engine({"sensitivity": "decider"}, decider="opendecider")
    eng.set_decider(fake)
    await eng.start()
    try:
        rec = await eng.write(GENDER, namespace="a", memory_type="episodic")
        assert "sens:high" in rec.tags
    finally:
        await eng.stop()


def test_heuristic_decider_has_rules_for_the_new_tasks() -> None:
    h = HeuristicDecider()
    assert h.decide_sync("sensitivity", GENDER).label == "sensitive"
    assert h.decide_sync("sensitivity", HOBBY).label == "not_sensitive"
    assert h.decide_sync("sensitivity_scope", "What is my diagnosis?").label == "about"
    assert h.decide_sync("sensitivity_scope", "Plan my weekend").label == "not_about"


# -- I53: participants, viewer, visibility ---------------------------------------------------


def test_visibility_rules() -> None:
    private = [*participant_tags(["Sam", "Pat"]), visibility_tag("private")]
    assert visible_to(private, "sam") and not visible_to(private, "Pat")
    group = [*participant_tags(["Sam", "Pat"]), visibility_tag("participants")]
    assert visible_to(group, "Pat") and not visible_to(group, "Lee")
    shared = [*participant_tags(["Sam"]), visibility_tag("owner_shared")]
    assert visible_to(shared, "Lee")
    assert visible_to(participant_tags(["Sam", "Pat"]), "Pat")  # participants default
    assert not visible_to(participant_tags(["Sam"]), "Lee")
    assert visible_to([], "Lee") and visible_to(["x"], "Lee")  # unmarked: today's behaviour
    with pytest.raises(ValueError, match="visibility"):
        visibility_tag("public")


def test_derived_records_inherit_participants_and_the_strictest_visibility() -> None:
    a = [*participant_tags(["Sam", "Pat"]), visibility_tag("participants")]
    b = [*participant_tags(["Sam", "Lee"]), visibility_tag("private")]
    tags = inherit_tags([a, b])
    assert visibility_of(tags) == "private"
    assert [t for t in tags if t.startswith("participant:")] == [
        "participant:Sam",
        "participant:Pat",
        "participant:Lee",
    ]
    assert inherit_tags([[], ["x"]]) == []


async def test_viewer_filters_search_assemble_and_read() -> None:
    eng = _engine()
    await eng.start()
    try:
        w = eng.write
        await w(
            "Sam diary entry about the garden",
            namespace="a",
            participants=["Sam", "Pat"],
            visibility="private",
        )
        await w(
            "Pat note about the garden",
            namespace="a",
            participants=["Pat", "Sam"],
            visibility="private",
        )
        await w(
            "Household garden plan", namespace="a", participants=["Sam"], visibility="owner_shared"
        )
        await w("Sam and Pat chat about the garden", namespace="a", participants=["Sam", "Pat"])
        await w("Lee mentions the garden", namespace="a", participants=["Lee"])
        await w("Unmarked garden remark", namespace="a")
        q = "the garden"

        async def seen(viewer: str | None) -> set[str]:
            hits = await eng.search(q, namespace="a", top_k=20, viewer=viewer)
            return {r.content.split(" about")[0].split(" chat")[0] for r, _ in hits}

        everyone = await seen(None)
        assert len(everyone) == 6
        sam = await seen("Sam")
        assert (
            "Sam diary entry" in sam
            and "Pat note" not in sam
            and "Lee mentions the garden" not in sam
        )
        assert {"Household garden plan", "Sam and Pat", "Unmarked garden remark"} <= sam
        pat = await seen("Pat")
        assert "Pat note" in pat and "Sam diary entry" not in pat
        lee = await seen("Lee")
        assert lee == {"Lee mentions the garden", "Household garden plan", "Unmarked garden remark"}

        ctx = await eng.assemble(q, namespace="a", viewer="Lee")
        assert not any("Sam diary" in r.content for r in ctx.records)
        for mode in ("full", "retrieve", "replay", "auto"):
            out = await eng.read(q, namespace="a", mode=mode, viewer="Lee")
            texts = " ".join(r.content for r in out.context.records)
            assert "Sam diary" not in texts and "Pat note" not in texts, mode
            assert "Lee mentions" in texts, mode
        # no viewer: nothing filtered, as before
        out = await eng.read(q, namespace="a", mode="full")
        assert "Sam diary" in " ".join(r.content for r in out.context.records)
    finally:
        await eng.stop()


async def test_write_messages_tags_session_participants() -> None:
    eng = _engine({"participants": "session"})
    await eng.start()
    try:
        recs = await eng.write_messages(
            [
                {"role": "user", "name": "Sam", "content": "The garden needs water"},
                {"role": "user", "name": "Pat", "content": "I will water the garden"},
                {"role": "assistant", "content": "Noted about the garden"},
            ],
            namespace="a",
            session_id="s1",
        )
        assert [t for t in recs[0].tags if t.startswith("participant:")][:1] == ["participant:Sam"]
        assert "participant:Pat" in recs[0].tags and "participant:assistant" in recs[0].tags
        assert next(t for t in recs[1].tags if t.startswith("participant:")) == "participant:Pat"
        hits = await eng.search("garden", namespace="a", top_k=10, viewer="Zed")
        assert hits == []
        hits = await eng.search("garden", namespace="a", top_k=10, viewer="Pat")
        assert len(hits) == 3
    finally:
        await eng.stop()


async def test_mined_fact_inherits_the_privacy_of_its_source_turn() -> None:
    eng = _engine({"participants": "session"})
    await eng.start()
    try:
        recs = await eng.write_messages(
            [
                {
                    "role": "user",
                    "name": "Sam",
                    "content": "I keep a secret garden",
                    "visibility": "private",
                }
            ],
            namespace="a",
            session_id="s1",
        )
        fact = await eng._deposit_mined_fact(
            "a", "Sam hobby: gardening", "Sam", "hobby", [recs[0].record_id], T0, "s1", kind="state"
        )
        assert visibility_of(fact.tags) == "private" and "participant:Sam" in fact.tags
        assert not visible_to(fact.tags, "Pat")
        hits = await eng.search("Sam gardening", namespace="a", top_k=10, viewer="Pat")
        assert hits == []
    finally:
        await eng.stop()


async def test_viewer_never_crosses_the_namespace() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.write(
            "Sam shared the garden plan",
            namespace="a",
            participants=["Sam"],
            visibility="owner_shared",
        )
        await eng.write(
            "Sam shared the garden plan",
            namespace="b",
            participants=["Sam"],
            visibility="owner_shared",
        )
        hits = await eng.search("garden plan", namespace="a", top_k=10, viewer="Sam")
        assert len(hits) == 1 and all(r.namespace == "a" for r, _ in hits)
    finally:
        await eng.stop()


# -- I48: inferred provenance -----------------------------------------------------------------


def test_classify_separates_statements_from_inferences() -> None:
    turns = [
        ("t1", "user", "I really enjoy long distance running before work"),
        ("t2", "user", "Marathon training starts again in March"),
        ("t3", "assistant", "You might like trail running too"),
    ]
    inferred, support = classify("Sam enjoys long distance running", turns)
    assert not inferred and support == []  # t1 states it
    inferred, support = classify(
        "Sam is a disciplined marathon athlete", turns, support_overlap=0.25
    )
    assert inferred and support == ["t2"]
    assert classify("Sam likes trail running", turns[2:])[0] is True
    assert containment("", "x") == 0.0
    assert support_count(support_tags(["a", "b", "a"])) == 2


def _write_cfg() -> dict[str, Any]:
    return {"inferred": "on"}


async def _turns(eng: Engine) -> list[str]:
    ids = []
    for i, (role, text) in enumerate(
        [
            ("user", "I ran a long endurance race on Sunday morning"),
            ("user", "Another early endurance session before work today"),
            ("assistant", "Perhaps you would enjoy a marathon someday"),
        ]
    ):
        rec = await eng.write(
            text,
            namespace="a",
            memory_type="episodic",
            source=SourceInfo(role=role, channel="messages"),
            valid_from=T0 + timedelta(minutes=i),
        )
        ids.append(rec.record_id)
    return ids


async def test_mined_inference_is_tagged_capped_and_listed_for_review() -> None:
    eng = _engine(_write_cfg())
    await eng.start()
    try:
        ids = await _turns(eng)
        stated = await eng._deposit_mined_fact(
            "a", "Sam ran a long race on Sunday", "Sam", "race", ids[:1], T0, "s1", kind="event"
        )
        assert INFERRED_TAG not in stated.tags  # the user said it
        guess = await eng._deposit_mined_fact(
            "a", "Sam is a dedicated athlete", "Sam", "trait", ids, T0, "s1", kind="state"
        )
        assert INFERRED_TAG in guess.tags
        assert guess.trust <= 0.4 < 0.7
        assert support_count(guess.tags) == 0
        listed = await eng.review_inferred("a")
        assert [r.record_id for r in listed] == [guess.record_id]
        assert [r.record_id for r in await eng.review_inferred("a", include_supported=True)] == [
            guess.record_id
        ]
    finally:
        await eng.stop()


async def test_inferred_gate_needs_two_distinct_user_turns() -> None:
    eng = _engine(_write_cfg(), inferred_gate="on")
    await eng.start()
    try:
        ids = await _turns(eng)
        weak = await eng._deposit_mined_fact(
            "a",
            "Sam is a dedicated athlete training seriously",
            "Sam",
            "trait",
            [ids[0], ids[2]],
            T0,
            "s1",
        )
        strong = await eng._deposit_mined_fact(
            "a",
            "Sam: serious endurance",
            "Sam",
            "habit",
            ids[:2],
            T0,
            "s1",
            kind="state",
        )
        assert INFERRED_TAG in weak.tags and INFERRED_TAG in strong.tags
        assert support_count(strong.tags) >= 2 and support_count(weak.tags) < 2
        hits = await eng.search("Sam training", namespace="a", top_k=20, tags=["atomic_fact"])
        got = {r.record_id for r, _ in hits}
        assert strong.record_id in got and weak.record_id not in got
        assert [r.record_id for r in await eng.review_inferred("a")] == [weak.record_id]
    finally:
        await eng.stop()


async def test_inferred_off_is_unchanged() -> None:
    eng = _engine()
    await eng.start()
    try:
        ids = await _turns(eng)
        fact = await eng._deposit_mined_fact(
            "a", "Sam is a dedicated athlete", "Sam", "trait", ids, T0, "s1", kind="state"
        )
        assert INFERRED_TAG not in fact.tags
        assert await eng.review_inferred("a") == []
    finally:
        await eng.stop()


def test_conflict_ladder_stated_beats_inferred() -> None:
    def rec(text: str, trust: float, inferred: bool, when: datetime) -> MemoryRecord:
        return _rec(
            text,
            [INFERRED_TAG] if inferred else [],
            entity="sam",
            attribute="likes",
            valid_from=when,
        ).model_copy(update={"trust": trust})

    later = T0 + timedelta(days=1)
    stated = rec("Sam likes jazz", 0.7, False, T0)
    guess = rec("Sam likes techno", 0.4, True, later)
    on = ConflictPolicy.bind({"inferred_defers": True})
    off = ConflictPolicy.bind({})
    assert on.resolve(guess, stated) is ConflictVerdict.NOOP  # never overrides a statement
    assert off.resolve(guess, stated) is not ConflictVerdict.NOOP  # default ladder unchanged
    assert on.resolve(rec("Sam likes folk", 0.7, False, T0 - timedelta(days=5)), guess) is (
        ConflictVerdict.UPDATE
    )  # a statement supersedes an inference even when it is older
    assert on.resolve(guess, rec("Sam likes pop", 0.4, True, T0)) is not ConflictVerdict.NOOP
