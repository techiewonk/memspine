"""I42 / I43 / I44 / I47 / I49 / I50 / I55 (remainders): the perspective layer's open items."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memspine import Engine
from memspine.core.perspective import (
    PerspectiveOptions,
    acknowledges,
    as_of_subject_ok,
    due_window_for,
    factor,
    hearsay_about,
    inherit_perspective_tags,
    is_past_plan,
    record_marker,
    record_view,
    resolve_question,
    resolve_write,
    unacknowledged_claim,
)
from memspine.core.read_filters import as_of_scope
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.semantic.write_pipeline import GraphWritePipeline
from memspine.prompts.models import ExtractedEdge

TWO = {"caroline", "melanie"}
CHAT = {"user", "assistant"}


def _rec(content: str, tags: list[str], **kw: Any) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="episodic", content=content, tags=tags, **kw)


def _engine(*, firewall: dict[str, Any] | None = None, policy: Any = None, **read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": {"perspective": policy or "heuristic"}},
            "semantic": {"enabled": True},
        },
        firewall=firewall or {},
        read={"hybrid": False, "record_access": False, **read},
    )


# ------------------------------------------------------------------ I43 reported speech


def test_i43_hearsay_is_weaker_evidence_about_its_source() -> None:
    p = resolve_write("Caroline: My mom said she loves tennis.", known=TWO)
    assert "rep:mother@caroline" in p.tags()
    rv = record_view(_rec("x", p.tags()))
    about_mom = resolve_question("Does Caroline's mom love tennis?", asker="melanie", known=TWO)
    about_other = resolve_question("Does Melanie love tennis?", asker="caroline", known=TWO)
    assert hearsay_about(about_mom, rv)
    assert not hearsay_about(about_other, rv)
    mult, parts = factor(about_mom, rv, 0.4, ("hearsay",))
    assert parts == {"hearsay": 0.8} and mult == 0.8
    assert factor(about_other, rv, 0.4, ("hearsay",))[0] == 1.0
    # an unspecified source ("I heard ...") is hearsay for any bound question
    vague = record_view(_rec("x", resolve_write("I heard Rome is lovely.", role="user").tags()))
    assert vague.reported == "unspecified"
    assert hearsay_about(resolve_question("What do I like?", known=CHAT), vague)


async def test_i43_hearsay_trust_is_capped() -> None:
    eng = _engine(firewall={"hearsay_trust_cap": 0.3})
    await eng.start()
    try:
        first, hear = await eng.write_messages(
            [
                {"role": "user", "content": "Caroline: I love tennis.", "speaker": "Caroline"},
                {
                    "role": "user",
                    "content": "Caroline: My mom said she loves tennis.",
                    "speaker": "Caroline",
                },
            ],
            namespace="a",
        )
        assert first.trust > 0.3  # first-hand, untouched
        assert "rep:mother@caroline" in hear.tags
        assert hear.trust <= 0.3
    finally:
        await eng.stop()


async def test_i43_cap_is_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        [hear] = await eng.write_messages(
            [{"role": "user", "content": "My mom said she loves tennis.", "speaker": "Caroline"}],
            namespace="a",
        )
        assert hear.trust > 0.3
    finally:
        await eng.stop()


# ------------------------------------------------------------------ I44 certainty


def test_i44_hedged_statements_are_weaker_fact_evidence() -> None:
    hedged = record_view(_rec("x", ["spk:caroline", "sub:caroline", "mod:fact", "cert:hedged"]))
    firm = record_view(_rec("x", ["spk:caroline", "sub:caroline", "mod:fact"]))
    q = resolve_question("Where does Caroline live?", asker="melanie", known=TWO)
    assert factor(q, hedged, 0.4, ("certainty",)) == (0.8, {"certainty": 0.8})
    assert factor(q, firm, 0.4, ("certainty",))[0] == 1.0
    belief = resolve_question("Where does Caroline probably live?", asker="melanie", known=TWO)
    assert belief.hedged
    assert factor(belief, hedged, 0.4, ("certainty",))[0] == 1.0  # the question hedges too
    assert factor(q, hedged, 0.4, ("subject",))[0] == 1.0  # axis not selected


def test_i44_hedged_marker_only_when_asked() -> None:
    rec = _rec("Maybe I live in Rome", ["cert:hedged", "spk:caroline", "sub:caroline"])
    assert record_marker(rec) is None
    assert record_marker(rec, hedge=True) == "[hedged]"


async def test_i44_render_marks_hedged_records() -> None:
    eng = _engine(perspective_marker=True, perspective_axes=["subject", "certainty"])
    await eng.start()
    try:
        await eng.write_messages(
            [{"role": "user", "content": "Maybe I live in Lisbon.", "speaker": "Caroline"}],
            namespace="a",
        )
        ctx = await eng.assemble("where does Caroline live", namespace="a", budget_tokens=500)
        assert any(r.content.startswith("[hedged]") for r in ctx.records)
        assert not any(r.content.startswith("[hedged]") for r in await eng._records("a"))
    finally:
        await eng.stop()


# ------------------------------------------------------------------ I47 acknowledgement


def test_i47_acknowledgement_cues() -> None:
    for yes in ("Yes, exactly.", "That's right", "yeah I do", "Correct!", "You're right."):
        assert acknowledges(yes), yes
    for no in (
        "No, that's not right.",
        "Actually I hate jazz",
        "Tell me about trains",
        "Not really",
    ):
        assert not acknowledges(no), no


def test_i47_unacknowledged_assistant_claim_is_excluded_from_profile_questions() -> None:
    p = resolve_write("You like jazz.", role="assistant", known=CHAT)
    rv = record_view(_rec("You like jazz.", p.tags()))
    mine = resolve_question("What music do I like?", known=CHAT)
    assert unacknowledged_claim(mine, rv)
    assert factor(mine, rv, 0.4, ("ack",))[1] == {"ack": 0.6}
    confirmed = record_view(_rec("You like jazz.", p.tags()))
    from dataclasses import replace

    assert not unacknowledged_claim(mine, replace(confirmed, acked=True))
    # a question about the assistant, and the assistant's own facts, are untouched
    assert not unacknowledged_claim(resolve_question("What did you say?", known=CHAT), rv)
    own = record_view(
        _rec(
            "I recommend Alps",
            resolve_write("I recommend Alps.", role="assistant", known=CHAT).tags(),
        )
    )
    assert not unacknowledged_claim(mine, own)


async def test_i47_user_turn_acknowledges_the_assistant_turn_before_it() -> None:
    eng = _engine(
        policy={"mode": "heuristic", "ack": True},
        perspective_mode="subject_filter",
        perspective_min_keep=0,
        perspective_axes=["subject", "ack"],
    )
    await eng.start()
    try:
        a1, u1, a2, u2 = await eng.write_messages(
            [
                {"role": "assistant", "content": "You like jazz."},
                {"role": "user", "content": "Yes, exactly."},
                {"role": "assistant", "content": "You work at a bakery."},
                {"role": "user", "content": "Tell me about trains."},
            ],
            namespace="a",
        )
        assert f"ack:{a1.record_id}" in u1.tags
        assert not any(t.startswith("ack:") for t in u2.tags)
        # a restart re-seeds the confirmed set from the stored tags
        eng._persp_state.clear()
        state = await eng._persp_state_for("a")
        assert state["acked"] == {a1.record_id}
        qp = resolve_question("What do I like?", known=CHAT)
        out = eng._apply_perspective([(a1, 1.0), (a2, 1.0)], qp)
        assert [r.record_id for r, _ in out] == [a1.record_id]  # a2 stays proposed: dropped
    finally:
        await eng.stop()


async def test_i47_ack_off_writes_no_ack_tags() -> None:
    eng = _engine()
    await eng.start()
    try:
        recs = await eng.write_messages(
            [
                {"role": "assistant", "content": "You like jazz."},
                {"role": "user", "content": "Yes, exactly."},
            ],
            namespace="a",
        )
        assert not any(t.startswith("ack:") for r in recs for t in r.tags)
    finally:
        await eng.stop()


def test_i47_decider_task_exists() -> None:
    from memspine.services.decision.decider import TASKS, HeuristicDecider

    assert "acknowledges" in TASKS
    d = HeuristicDecider().decide_sync("acknowledges", "Yes, that's right", "You like jazz.")
    assert d.label == "ack"
    assert PerspectiveOptions.parse({"mode": "heuristic", "ack": True}).ack


# ------------------------------------------------------------------ I49 plan windows


def test_i49_absolute_and_relative_windows() -> None:
    said = datetime(2026, 1, 5, tzinfo=UTC)
    assert due_window_for("We fly to Rome on 12 March 2026.", said) == ("2026-03-12", "2026-03-12")
    assert due_window_for("I plan to move in June 2026.", said) == ("2026-06-01", "2026-06-30")
    assert due_window_for("I'm going to Rome next week", said) == ("2026-01-12", "2026-01-19")
    assert due_window_for("I'm thinking of moving someday", said) == (None, None)
    # a date already past is not a forward window
    assert due_window_for("Planning a talk about my trip in 2019", said) == (None, None)
    # a year-less date takes its next occurrence
    assert due_window_for("I'll visit on March 3.", said) == ("2026-03-03", "2026-03-03")
    assert due_window_for("I'll visit on January 2.", said) == ("2027-01-02", "2027-01-02")


def test_i49_window_tags_and_past_plan_marker() -> None:
    said = datetime(2026, 1, 5, tzinfo=UTC)
    p = resolve_write("I'm going to fly to Rome on 12 March 2026.", role="user", when=said)
    assert {"due_from:2026-03-12", "due_to:2026-03-12"} <= set(p.tags())
    rec = _rec("I'm going to fly to Rome on 12 March 2026.", p.tags())
    assert record_view(rec).due_from == "2026-03-12"
    assert not is_past_plan(rec, datetime(2026, 3, 12, tzinfo=UTC))
    assert record_marker(rec, datetime(2026, 4, 1, tzinfo=UTC)) == "[past plan]"
    assert record_marker(rec, datetime(2026, 2, 1, tzinfo=UTC)) is None
    # a fact with a date is not a plan: no window
    fact = resolve_write("I flew to Rome on 12 March 2025.", role="user", when=said)
    assert not any(t.startswith("due_") for t in fact.tags())


# ------------------------------------------------------------------ I50 subject card


async def _state(eng: Engine, content: str, attribute: str, entity: str, sub: str, t: int) -> None:
    await eng.write(
        content,
        namespace="a",
        memory_type="semantic",
        entity=entity,
        attribute=attribute,
        tags=["kind:state", f"sub:{sub}"],
        valid_from=datetime(2024, t, 1, tzinfo=UTC),
    )


async def test_i50_subject_card_rolls_up_per_subject_and_matches_persons() -> None:
    eng = _engine(profile_slots_header=True, profile_subject_card=True)
    await eng.start()
    try:
        await _state(eng, "Caroline home: Paris", "home", "caroline", "caroline", 1)
        await _state(eng, "Caroline job: nurse", "job", "caroline", "caroline", 2)
        await _state(eng, "Mother home: Lyon", "home", "mother", "mother@caroline", 3)
        await _state(eng, "Melanie home: Oslo", "home", "melanie", "melanie", 4)
        block = await eng._subject_card("a", "Where is the home of Caroline's mother?", 1000)
        assert block is not None
        text = block.content
        assert "[mother@caroline]" in text and "Mother home: Lyon" in text
        assert "Caroline home: Paris" not in text  # the question names the mother only
        assert "Melanie" not in text
        mixed = await eng._subject_card("a", "Where is my home, Melanie?", 1000)
        assert mixed is None or "Melanie home: Oslo" in mixed.content
        none = await eng._subject_card("a", "What is the weather", 1000)
        assert none is None  # no resolved person: nothing injected
        named = await eng._subject_card("a", "What is Caroline's job and home?", 1000)
        assert named is not None
        assert "[caroline]" in named.content
        assert "Caroline job: nurse" in named.content and "Caroline home: Paris" in named.content
    finally:
        await eng.stop()


async def test_i50_card_is_off_by_default() -> None:
    eng = _engine(profile_slots_header=True)
    await eng.start()
    try:
        await _state(eng, "Caroline home: Paris", "home", "caroline", "caroline", 1)
        block = await eng._slots_section("a", "Where does Caroline live, home?", 1000)
        assert block is not None and "[caroline]" not in block.content
    finally:
        await eng.stop()


# ------------------------------------------------------------------ I55 inheritance


def test_i55_inherit_perspective_tags_rules() -> None:
    one = _rec(
        "x", ["sub:caroline", "sub:cousin@caroline", "scope:standing", "pol:neg", "mod:fact"]
    )
    assert inherit_perspective_tags([one]) == [
        "sub:caroline",
        "sub:cousin@caroline",
        "scope:standing",
        "pol:neg",
    ]
    mixed = _rec("x", ["sub:caroline", "scope:event", "scope:habit", "pol:neg", "pol:pos"])
    assert inherit_perspective_tags([mixed]) == ["sub:caroline"]  # ambiguous axes say nothing
    other = _rec("x", ["sub:melanie", "scope:standing", "pol:neg"])
    assert inherit_perspective_tags([one, other]) == [
        "scope:standing",
        "pol:neg",
    ]  # only what both share
    assert inherit_perspective_tags([_rec("x", [])]) == []
    assert inherit_perspective_tags([]) == []


async def test_i55_mined_fact_inherits_and_the_conflict_key_uses_it() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": {"perspective": "heuristic"}},
            "semantic": {"enabled": True, "policies": {"conflict": {"perspective_key": True}}},
        },
        read={"hybrid": False, "record_access": False},
    )
    await eng.start()
    try:
        turns = await eng.write_messages(
            [
                {"role": "user", "content": "I really like jazz.", "speaker": "Caroline"},
                {"role": "user", "content": "My cousin likes rock.", "speaker": "Caroline"},
            ],
            namespace="a",
        )
        mine = await eng._deposit_mined_fact(
            "a", "Caroline likes: jazz", "caroline", "likes", [turns[0].record_id],
            datetime(2024, 1, 1, tzinfo=UTC), "s1", kind="state",
        )  # fmt: skip
        cousin = await eng._deposit_mined_fact(
            "a", "Cousin likes: rock", "caroline", "likes", [turns[1].record_id],
            datetime(2024, 2, 1, tzinfo=UTC), "s1", kind="state",
        )  # fmt: skip
        assert "sub:caroline" in mine.tags and "scope:standing" in mine.tags
        assert "sub:cousin@caroline" in cousin.tags
        live = [
            r
            for r in await eng._records("a", "semantic")
            if r.status is RecordStatus.ACTIVATED and r.attribute == "likes"
        ]
        assert len(live) == 2  # another subject: both hold, nothing superseded
    finally:
        await eng.stop()


async def test_i55_graph_edge_inherits_from_its_source_turn() -> None:
    async def extract(text: str, _ctx: object) -> list[ExtractedEdge]:
        return [
            ExtractedEdge(
                src_entity="caroline", rel="likes", dst_entity="jazz", fact=text, kind="state"
            )
        ]

    written: list[MemoryRecord] = []

    async def write_fact(fact: MemoryRecord) -> MemoryRecord:
        written.append(fact)
        return fact

    src = _rec("I like jazz.", ["sub:caroline", "scope:standing", "pol:pos"])
    await GraphWritePipeline(extract).run(src, write_fact)  # type: ignore[arg-type]
    assert {"sub:caroline", "scope:standing", "pol:pos"} <= set(written[0].tags)
    written.clear()
    await GraphWritePipeline(extract).run(_rec("I like jazz.", []), write_fact)  # type: ignore[arg-type]
    assert not any(t.startswith(("sub:", "scope:", "pol:")) for t in written[0].tags)


# ------------------------------------------------------------------ I42 as-of by subject


def test_i42_other_subjects_history_is_out_of_a_subject_as_of_read() -> None:
    q = resolve_question("Where does Caroline live?", asker="melanie", known=TWO)
    mine = record_view(_rec("x", ["spk:caroline", "sub:caroline"]))
    other = record_view(_rec("x", ["spk:melanie", "sub:melanie"]))
    plain = record_view(_rec("x", []))
    assert as_of_subject_ok(q, mine)
    assert not as_of_subject_ok(q, other)
    assert as_of_subject_ok(q, plain)  # untagged: no information, kept
    assert as_of_subject_ok(resolve_question("hello", known=TWO), other)  # unresolved


async def test_i42_apply_perspective_filters_history_only_under_as_of() -> None:
    eng = _engine(perspective_mode="subject_weight", perspective_as_of_subject=True)
    await eng.start()
    try:
        qp = resolve_question("Where does Caroline live?", asker="melanie", known=TWO)
        live_other = _rec("Melanie: I live in Oslo", ["spk:melanie", "sub:melanie"])
        old_other = _rec("Melanie: I lived in Rome", ["spk:melanie", "sub:melanie"]).model_copy(
            update={"status": RecordStatus.ARCHIVED}
        )
        old_mine = _rec("Caroline: I lived in Rome", ["spk:caroline", "sub:caroline"]).model_copy(
            update={"status": RecordStatus.ARCHIVED}
        )
        cands = [(live_other, 1.0), (old_other, 1.0), (old_mine, 1.0)]
        no_as_of = {r.record_id for r, _ in eng._apply_perspective(cands, qp)}
        assert no_as_of == {r.record_id for r, _ in cands}
        with as_of_scope(datetime(2024, 1, 1, tzinfo=UTC)):
            kept = {r.record_id for r, _ in eng._apply_perspective(cands, qp)}
        assert kept == {live_other.record_id, old_mine.record_id}  # live ones are never dropped
    finally:
        await eng.stop()
