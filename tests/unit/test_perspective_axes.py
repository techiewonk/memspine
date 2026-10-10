"""I47-I55: comparison-driven additions to the perspective layer."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.core.perspective import (
    PerspectiveOptions,
    due_to_for,
    is_past_plan,
    match_subject,
    record_marker,
    record_view,
    resolve_question,
    resolve_write,
)
from memspine.core.policies.conflict import ConflictPolicy, ConflictVerdict
from memspine.core.records import MemoryRecord

TWO = {"caroline", "melanie"}
CHAT = {"user", "assistant"}


def _rec(content: str, tags: list[str]) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="episodic", content=content, tags=tags)


def test_i54_kin_resolves_against_the_speaker() -> None:
    assert "sub:mother@caroline" in resolve_write("Caroline: My mom is visiting.", known=TWO).tags()
    assert "sub:mother@user" in resolve_write("My mom is visiting.", role="user").tags()
    your = resolve_write("Melanie: How is your mom?", known=TWO, previous="caroline").tags()
    assert "sub:mother@caroline" in your  # "your" binds to the addressee
    q = resolve_question("How is my mom?", asker="melanie", known=TWO)
    assert q.targets[0].keys == ("mother@melanie",)
    other = record_view(_rec("x", ["spk:caroline", "sub:mother@caroline"]))
    assert match_subject(q, other) == 0.0


def test_i47_agent_subject_is_not_a_user_fact() -> None:
    a = resolve_write("I recommend the Alps trail.", role="assistant", known=CHAT)
    assert a.subjects[0].kind == "agent"
    assert "sub:assistant" in a.tags()
    rv = record_view(_rec("assistant: I recommend the Alps trail", a.tags()))
    assert match_subject(resolve_question("What do I like?", known=CHAT), rv) == 0.0
    assert match_subject(resolve_question("What did you recommend?", known=CHAT), rv) == 1.0


def test_i49_plans_have_a_forward_window() -> None:
    said = datetime(2023, 5, 1, tzinfo=UTC)
    assert due_to_for("I'm going to Rome next week", said) == "2023-05-15"
    assert due_to_for("I'll call you in 3 days", said) == "2023-05-04"
    assert due_to_for("I'm thinking of moving someday", said) is None  # a soft deadline
    p = resolve_write("I'm going to Rome next week.", role="user", when=said)
    assert "due_to:2023-05-15" in p.tags()
    rec = _rec("I'm going to Rome next week.", p.tags())
    assert not is_past_plan(rec, datetime(2023, 5, 10, tzinfo=UTC))
    assert is_past_plan(rec, datetime(2023, 6, 1, tzinfo=UTC))
    assert record_marker(rec, datetime(2023, 6, 1, tzinfo=UTC)) == "[past plan]"
    assert record_marker(rec, datetime(2023, 5, 10, tzinfo=UTC)) is None


def test_i51_namespace_context() -> None:
    o = PerspectiveOptions.parse(
        {"mode": "heuristic", "context": {"owner": "Caroline", "speakers": ["Caroline", "Melanie"]}}
    )
    assert o.owner == "caroline"
    assert o.speakers == ("caroline", "melanie")


def _fact(content: str, tags: list[str]) -> MemoryRecord:
    rec = MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=content,
        tags=tags,
        entity="caroline",
        attribute="likes_jazz",
    )
    return rec.model_copy(update={"content_fingerprint": content})


async def test_i55_invariant_one_active_state_per_owner_entity_attribute_subject() -> None:
    from memspine import Engine

    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True, "policies": {"conflict": {"perspective_key": True}}}
        },
        read={"hybrid": False, "record_access": False},
    )
    await eng.start()
    try:
        t0 = datetime(2023, 1, 1, tzinfo=UTC)
        for n, (text, sub) in enumerate(
            [
                ("Caroline lives in Paris", "sub:caroline"),
                ("Her cousin lives in Oslo", "sub:cousin@caroline"),
                ("Caroline lives in Rome", "sub:caroline"),
                ("Her cousin lives in Bergen", "sub:cousin@caroline"),
            ]
        ):
            await eng.write(
                text,
                namespace="a",
                memory_type="semantic",
                entity="caroline",
                attribute="home",
                tags=[sub],
                valid_from=datetime(2023, 1 + n, 1, tzinfo=UTC),
            )
        live = [
            r
            for r in await eng._records("a", "semantic")
            if r.valid_to is None and r.superseded_at is None and r.status.value == "activated"
        ]
        groups: dict[tuple[str, str, str], int] = {}
        for r in live:
            key = (
                r.entity or "",
                r.attribute or "",
                ",".join(sorted(t for t in r.tags if t.startswith(("sub:", "scope:")))),
            )
            groups[key] = groups.get(key, 0) + 1
        assert t0 and groups  # something is live
        assert all(n == 1 for n in groups.values()), groups
        assert len(groups) == 2  # two subjects coexist
    finally:
        await eng.stop()


def test_i55_conflict_key_carries_subject_scope_polarity() -> None:
    policy = ConflictPolicy.bind({"perspective_key": True})
    base = _fact("Caroline likes jazz", ["sub:caroline", "pol:pos", "scope:standing"])
    cousin = _fact("Her cousin likes jazz", ["sub:cousin@caroline", "pol:pos"])
    club = _fact("Caroline went to a jazz club", ["sub:caroline", "scope:event"])
    flip = _fact("Caroline does not like jazz", ["sub:caroline", "pol:neg", "scope:standing"])
    same = _fact("Caroline loves jazz", ["sub:caroline", "pol:pos", "scope:standing"])
    assert policy.resolve(cousin, base) is ConflictVerdict.ADD
    assert policy.resolve(club, base) is ConflictVerdict.ADD
    assert policy.resolve(flip, base) is ConflictVerdict.CONTEST
    assert policy.resolve(same, base) is not ConflictVerdict.ADD
    default = ConflictPolicy.bind({})
    assert default.resolve(cousin, base) is not ConflictVerdict.ADD  # unchanged when off
