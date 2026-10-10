"""I39-I46: the perspective / attribution layer (owner, speaker, addressee, subject, asker,
modality, polarity, scope, reported speech, certainty, sensitivity)."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.perspective import (
    PerspectiveOptions,
    asker_scope,
    attribution_marker,
    factor,
    match_subject,
    record_view,
    refine_with_decider,
    resolve_question,
    resolve_write,
    scope_of,
)
from memspine.core.records import MemoryRecord

TWO = {"caroline", "melanie"}


def _rec(content: str, tags: list[str]) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="episodic", content=content, tags=tags)


# ------------------------------------------------------------------ resolver, four shapes


def test_defaults_off() -> None:
    assert ReadConfig().perspective_mode == "off"
    assert ReadConfig().perspective_axes == ["subject"]
    assert ReadConfig().perspective_marker is False
    assert not PerspectiveOptions.parse(None).on
    assert not PerspectiveOptions.parse("off").on
    assert PerspectiveOptions.parse("heuristic").on
    assert PerspectiveOptions.parse({"mode": "decider", "axes": ["sub", "bogus"]}).axes == ("sub",)


def test_two_named_speakers() -> None:
    p = resolve_write("Caroline: My cousin is looking for ways to get involved.", known=TWO)
    assert p.spk == "caroline"
    assert p.addr == "melanie"
    tags = p.tags()
    assert "sub:cousin@caroline" in tags
    assert "sub:caroline" not in tags  # the statement is about the cousin, not Caroline
    q = resolve_write("Melanie: Did you see your cousin Anna last week?", known=TWO)
    assert q.spk == "melanie"
    assert q.addr == "caroline"
    assert {"sub:anna", "sub:cousin@caroline", "sub:caroline"} <= set(q.tags())
    assert "mod:question" in q.tags()
    assert "ask:melanie" in q.tags()


def test_user_assistant_chat() -> None:
    known = {"user", "assistant"}
    u = resolve_write("I love sushi", role="user", known=known)
    assert (u.spk, u.addr) == ("user", "assistant")
    assert "sub:user" in u.tags()
    assert "scope:standing" in u.tags()
    a = resolve_write("You mentioned sushi. Try the new cafe.", role="assistant", known=known)
    assert (a.spk, a.addr) == ("assistant", "user")
    assert "sub:user" in a.tags()  # "you" in an assistant turn is the user
    pre = resolve_write("assistant: I recommend Kyoto", role="user", known=known)
    assert pre.spk == "assistant"  # a text prefix beats a default role


def test_multi_party() -> None:
    known = {"ann", "bob", "cy"}
    p = resolve_write("Bob: Ann said she is moving. Cy and I disagree.", known=known)
    assert p.spk == "bob"
    assert "sub:cy" in p.tags()
    assert "sub:bob" in p.tags()
    assert "rep:ann" in p.tags()
    r = resolve_write("Ann: hi Bob", known=known, previous="bob")
    assert r.addr == "bob"


def test_single_user_notes_without_speaker() -> None:
    p = resolve_write("Paris is the capital of France.", role=None)
    assert p.spk is None
    assert p.subjects == ()  # no first / second person, no implicit subject
    mine = resolve_write("I moved to Berlin last year", role=None)
    assert [s.id for s in mine.subjects] == ["user"]  # the owner
    assert "scope:event" in mine.tags()


# ------------------------------------------------------------------ the other axes


def test_polarity_modality_certainty_scope_reported() -> None:
    neg = resolve_write("I don't like jazz.", role="user").tags()
    assert "pol:neg" in neg
    assert "pol:pos" not in neg
    assert "pol:neg" not in resolve_write("No problem, I like jazz.", role="user").tags()
    plan = resolve_write("I'm thinking of moving to Berlin.", role="user").tags()
    assert "mod:plan" in plan
    assert "mod:fact" not in plan
    assert "mod:wish" in resolve_write("I wish I could visit Japan.", role="user").tags()
    assert "mod:hypo" in resolve_write("If I won the lottery I would travel.", role="user").tags()
    assert "mod:request" in resolve_write("Can you recommend a book?", role="user").tags()
    assert "mod:question" in resolve_write("Where did you grow up?", role="user").tags()
    mixed = resolve_write("I moved to Berlin. Maybe I will move again.", role="user").tags()
    assert "mod:fact" in mixed
    assert "cert:hedged" in mixed
    assert scope_of("I usually run on weekends") == {"habit"}
    assert scope_of("Had sushi yesterday") == {"event"}
    assert "standing" in scope_of("I love sushi")
    rep = resolve_write("My mom said I should see a doctor about my diagnosis", role="user")
    assert "rep:mother@user" in rep.tags()
    assert "sensitive:health" in rep.tags()
    assert not any(t.startswith("rep:") for t in resolve_write("I said hello", role="user").tags())


def test_axes_selection() -> None:
    p = resolve_write("I don't like jazz.", role="user", axes=("pol",))
    assert p.tags(("pol",)) == ["pol:neg"]
    assert resolve_write("x", role="user").tags(()) == []


# ------------------------------------------------------------------ question side


def test_question_perspective_resolution() -> None:
    q = resolve_question("What does my cousin like to do?", asker="caroline", known=TWO)
    assert q.about == "third"
    assert q.targets[0].keys == ("cousin@caroline",)
    assert q.scope == "trait"
    me = resolve_question("What are my hobbies?", asker="caroline", known=TWO)
    assert me.about == "self"
    assert me.targets[0].id == "caroline"
    nobody = resolve_question("What are my hobbies?", known=TWO)  # two names, no asker
    assert not nobody.bound
    named = resolve_question("What did Melanie research?", known=TWO)
    assert named.about == "participant"
    chat = resolve_question("What did you recommend?", known={"user", "assistant"})
    assert chat.about == "assistant"
    assert resolve_question("What do I like?", known={"user", "assistant"}).about == "self"
    assert resolve_question("My boss's dog is sick", known={"user"}).bound
    with asker_scope("melanie"):
        from memspine.core.perspective import active_asker

        assert active_asker() == "melanie"


def test_match_and_factor() -> None:
    q = resolve_question("What does my cousin like?", asker="caroline", known=TWO)
    own = record_view(_rec("Caroline: I love painting", ["spk:caroline", "sub:caroline"]))
    cousin = record_view(
        _rec("Caroline: My cousin loves hiking", ["spk:caroline", "sub:cousin@caroline"])
    )
    other = record_view(_rec("Melanie: I like pottery", ["spk:melanie", "sub:melanie"]))
    plain = record_view(_rec("x", []))
    assert match_subject(q, cousin) == 1.0
    assert match_subject(q, own) == 0.0
    assert match_subject(q, other) == 0.0
    assert match_subject(q, plain) is None
    assert factor(q, plain, 0.4)[0] == 1.0
    assert factor(q, own, 0.4)[0] == pytest.approx(0.6)
    me = resolve_question("What do I like?", asker="caroline", known=TWO)
    assert match_subject(me, own) == 1.0
    assert match_subject(me, cousin) == 0.6  # the asker speaking of someone else


def test_modality_polarity_scope_sensitivity_factors() -> None:
    axes = {"modality", "polarity", "scope", "sensitivity"}
    q = resolve_question("What does Caroline like?", known=TWO)
    neg = record_view(_rec("n", ["pol:neg", "mod:fact"]))
    plan = record_view(_rec("p", ["mod:plan", "pol:pos"]))
    once = record_view(_rec("e", ["scope:event", "mod:fact", "pol:pos"]))
    sens = record_view(_rec("s", ["sensitive:health", "mod:fact", "pol:pos"]))
    assert factor(q, neg, 0.4, axes)[1] == {"polarity": pytest.approx(0.6)}
    assert factor(q, plan, 0.4, axes)[1] == {"modality": pytest.approx(0.6)}
    assert factor(q, once, 0.4, axes)[1] == {"scope": pytest.approx(0.8)}
    assert factor(q, sens, 0.4, axes)[1] == {"sensitivity": pytest.approx(0.6)}
    # kept where the question asks for it
    neg_q = resolve_question("What does Caroline not like?", known=TWO)
    assert factor(neg_q, neg, 0.4, axes)[1] == {}
    ever = resolve_question("Did Caroline ever say she dislikes jazz?", known=TWO)
    assert factor(ever, neg, 0.4, axes)[1] == {}
    plan_q = resolve_question("What is Caroline planning to do?", known=TWO)
    assert factor(plan_q, plan, 0.4, axes)[1] == {}
    health_q = resolve_question("What is Caroline's diagnosis?", known=TWO)
    assert factor(health_q, sens, 0.4, axes)[1] == {}
    when = resolve_question("When did Caroline go hiking?", known=TWO)
    assert factor(when, once, 0.4, axes)[1] == {}


def test_attribution_marker() -> None:
    cousin = _rec("Caroline: My cousin ...", ["spk:caroline", "sub:cousin@caroline"])
    assert attribution_marker(cousin) == "[about: Caroline's cousin]"
    named = _rec("x", ["spk:caroline", "sub:anna", "sub:cousin@caroline"])
    assert attribution_marker(named) == "[about: Anna (Caroline's cousin)]"
    assert attribution_marker(_rec("x", ["spk:caroline", "sub:caroline"])) is None
    assert attribution_marker(_rec("x", [])) is None


async def test_decider_refines_ambiguous_axes() -> None:
    base = resolve_write("Jazz is something I enjoy", role="user")

    async def decide(task: str, _text: str) -> tuple[str, float | None]:
        return {
            "is_fact": ("not_fact", 0.9),
            "is_negated": ("negated", 0.9),
            "is_standing": ("standing", 0.2),  # below the floor: the rule stays
            "is_hedged": ("hedged", None),
        }[task]

    out = await refine_with_decider(base, "Jazz is something I enjoy", decide, 0.5)
    assert "fact" not in out.modality
    assert out.polarity == {"neg"}
    assert out.scope == base.scope
    assert out.hedged == base.hedged

    async def boom(_task: str, _text: str) -> tuple[str, float | None]:
        raise RuntimeError("down")

    assert (await refine_with_decider(base, "x", boom)).modality == base.modality


# ------------------------------------------------------------------ the engine


def _engine(policy: Any = "heuristic", **read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True, "policies": {"perspective": policy}}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def test_write_tags_and_content_unchanged() -> None:
    eng = _engine()
    await eng.start()
    try:
        recs = await eng.write_messages(
            [
                {"role": "user", "content": "Caroline: My cousin is looking for a club.",
                 "speaker": "Caroline"},
                {"role": "user", "content": "Melanie: That is great, do you like clubs?",
                 "speaker": "Melanie"},
            ],
            namespace="a",
        )
        assert recs[0].content == "Caroline: My cousin is looking for a club."
        assert {"spk:caroline", "addr:melanie", "sub:cousin@caroline"} <= set(recs[0].tags)
        assert {"spk:melanie", "addr:caroline", "mod:question"} <= set(recs[1].tags)
    finally:
        await eng.stop()


async def test_explicit_speaker_without_text_prefix() -> None:
    eng = _engine()
    await eng.start()
    try:
        [rec] = await eng.write_messages(
            [{"role": "user", "content": "I adopted a dog", "speaker": "Melanie"}], namespace="a"
        )
        assert "spk:melanie" in rec.tags
        assert "sub:melanie" in rec.tags
        assert rec.content == "I adopted a dog"
    finally:
        await eng.stop()


async def test_off_writes_no_perspective_tags() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False},
    )
    await eng.start()
    try:
        [rec] = await eng.write_messages(
            [{"role": "user", "content": "Caroline: My cousin moved", "speaker": "Caroline"}],
            namespace="a",
        )
        assert rec.tags == []
    finally:
        await eng.stop()


async def _store(eng: Engine) -> None:
    await eng.write_messages(
        [
            {"role": "user", "speaker": "Caroline",
             "content": "Caroline: I love volunteering in my community and getting involved."},
            {"role": "user", "speaker": "Melanie",
             "content": "Melanie: Nice! Do you like to get involved in the community?"},
            {"role": "user", "speaker": "Caroline",
             "content": "Caroline: My cousin is looking for ways to get involved in their "
             "community."},
            {"role": "user", "speaker": "Melanie",
             "content": "Melanie: Tell your cousin about the food bank."},
        ],
        namespace="a",
    )


async def _ids(
    eng: Engine, query: str, asker: str | None = "caroline"
) -> tuple[list[str], dict[str, Any]]:
    from memspine.engine import search_forensics

    with search_forensics() as fx, asker_scope(asker):
        await eng.assemble(query, namespace="a", budget_tokens=2000, top_k=4)
    return [rid for rid, _ in fx.get("final", [])], fx


async def test_subject_confusion_filter_prefers_the_cousin_record() -> None:
    """OP-Bench shape: "my cousin ..." must not retrieve the owner's self-only statement first."""
    query = "My cousin is looking for ways to get involved in their community"
    off = _engine()
    await off.start()
    await _store(off)
    ids_off, _ = await _ids(off, query)
    await off.stop()

    eng = _engine(perspective_mode="subject_filter", perspective_min_keep=1)
    await eng.start()
    try:
        await _store(eng)
        records = {r.record_id: r for r in await eng._records("a")}
        ids, fx = await _ids(eng, query)
        top = records[ids[0]]
        assert "sub:cousin@caroline" in top.tags
        self_only = [
            i for i in ids if "sub:caroline" in records[i].tags and "sub:cousin@caroline" not in records[i].tags
        ]
        assert not self_only
        assert fx["perspective"]["about"] == "third"
        assert fx["perspective"]["dropped"]
        assert ids_off  # the default read is unchanged and still returns evidence
    finally:
        await eng.stop()


async def test_subject_weight_orders_third_party_before_self() -> None:
    eng = _engine(perspective_mode="subject_weight", perspective_weight=0.9)
    await eng.start()
    try:
        await _store(eng)
        records = {r.record_id: r for r in await eng._records("a")}
        ids, fx = await _ids(eng, "What is my cousin looking for in the community?")
        assert "sub:cousin@caroline" in records[ids[0]].tags
        assert fx["perspective"]["factors"]
        names = [n for n, _ in fx["extra_legs"]]
        assert "perspective" in names
    finally:
        await eng.stop()


async def test_two_speaker_question_about_a_participant() -> None:
    """LoCoMo shape: "What did Melanie ...?" favours records about / by Melanie."""
    eng = _engine(perspective_mode="subject_weight", perspective_weight=0.9)
    await eng.start()
    try:
        await _store(eng)
        records = {r.record_id: r for r in await eng._records("a")}
        ids, fx = await _ids(eng, "Does Melanie like to get involved in the community?", None)
        assert fx["perspective"]["about"] == "participant"
        assert "spk:melanie" in records[ids[0]].tags or "sub:melanie" in records[ids[0]].tags
    finally:
        await eng.stop()


async def test_chat_roles_assistant_statements() -> None:
    eng = _engine(perspective_mode="subject_weight", perspective_weight=0.9)
    await eng.start()
    try:
        await eng.write_messages(
            [
                {"role": "user", "content": "I love hiking in the mountains."},
                {"role": "assistant", "content": "I recommend the Alps trail for hiking."},
            ],
            namespace="a",
        )
        records = {r.record_id: r for r in await eng._records("a")}
        ids, fx = await _ids(eng, "What hiking trail did you recommend?", None)
        assert fx["perspective"]["about"] == "assistant"
        assert "spk:assistant" in records[ids[0]].tags
    finally:
        await eng.stop()


async def test_marker_renders_without_changing_content() -> None:
    eng = _engine(perspective_marker=True)
    await eng.start()
    try:
        await _store(eng)
        ctx = await eng.assemble("cousin community", namespace="a", budget_tokens=2000, top_k=4)
        lines = [r.content for r in ctx.records]
        assert any(c.startswith("[about: Caroline's cousin] Caroline: My cousin") for c in lines)
        stored = [r.content for r in await eng._records("a")]
        assert not any(c.startswith("[about:") for c in stored)
    finally:
        await eng.stop()


async def test_latest_wins_is_per_subject_and_skips_non_facts() -> None:
    from memspine.core.latest_wins import mark_latest

    a = _rec("Caroline: I live in Paris now", ["spk:caroline", "sub:caroline"])
    b = _rec("Caroline: My cousin lives in Rome now", ["spk:caroline", "sub:cousin@caroline"])
    q = _rec("Caroline: Do I live in Paris now?", ["spk:caroline", "mod:question"])
    from datetime import UTC, datetime

    a.valid_from = datetime(2023, 1, 1, tzinfo=UTC)
    b.valid_from = datetime(2023, 2, 1, tzinfo=UTC)
    q.valid_from = datetime(2023, 3, 1, tzinfo=UTC)
    assert mark_latest([a, b, q]) == {}
