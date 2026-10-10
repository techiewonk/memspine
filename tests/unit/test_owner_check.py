"""I59 / I60 / I63 / I64: read-side owner check, entity existence, user header, re-injection."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.owner_check import (
    InjectionLog,
    entity_note,
    judge,
    missing_names,
    owner_marker,
    store_vocab,
    user_header,
)
from memspine.core.perspective import asker_scope, record_view, resolve_question
from memspine.core.records import MemoryRecord

TWO = {"caroline", "melanie"}
CHAT = {"user", "assistant"}


def _rv(tags: list[str]):
    return record_view(MemoryRecord(namespace="a", memory_type="episodic", content="x", tags=tags))


def test_defaults_off() -> None:
    r = ReadConfig()
    assert r.owner_check == "off"
    assert r.entity_check == "off"
    assert r.user_header == "off"
    assert r.reinjection_penalty == 0.0


# ------------------------------------------------------------------------ pure judge


def test_owner_swap_gets_a_note_and_markers() -> None:
    qp = resolve_question("What did Caroline paint?", known=TWO)
    views = [_rv(["spk:melanie", "sub:melanie"]), _rv(["spk:melanie", "sub:melanie"])]
    v = judge(qp, views)
    assert v.labels == ["other", "other"]
    assert v.note and "Caroline" in v.note and "Melanie" in v.note
    assert owner_marker(views[0]) == "[Melanie, about Melanie]"


def test_two_person_comparison_keeps_both_and_no_note() -> None:
    qp = resolve_question("Do Caroline and Melanie both like painting?", known=TWO)
    views = [_rv(["spk:melanie", "sub:melanie"]), _rv(["spk:caroline", "sub:caroline"])]
    v = judge(qp, views)
    assert v.labels == ["target", "target"]
    assert v.note is None


def test_one_line_about_the_target_means_no_note() -> None:
    qp = resolve_question("What did Caroline paint?", known=TWO)
    views = [_rv(["spk:melanie", "sub:melanie"]), _rv(["spk:caroline", "sub:caroline"])]
    v = judge(qp, views)
    assert v.labels == ["other", "target"]
    assert v.note is None


def test_unknown_lines_never_trigger_a_note() -> None:
    qp = resolve_question("What did Caroline paint?", known=TWO)
    v = judge(qp, [_rv(["spk:melanie", "sub:melanie"]), None])
    assert v.note is None
    assert v.labels == ["other", "unknown"]
    # the decider's answer for the unknown line
    again = judge(qp, [_rv(["spk:melanie", "sub:melanie"]), None], overrides={1: False})
    assert again.note is not None
    kept = judge(qp, [_rv(["spk:melanie", "sub:melanie"]), None], overrides={1: True})
    assert kept.note is None


def test_chat_user_vs_assistant() -> None:
    qp = resolve_question("What do I like to eat?", known=CHAT)
    views = [_rv(["spk:assistant", "sub:assistant"])]
    v = judge(qp, views)
    assert v.note and "the user" in v.note and "the assistant" in v.note
    qp2 = resolve_question("What did you recommend?", known=CHAT)
    assert judge(qp2, views).note is None


def test_unresolved_question_is_neutral() -> None:
    qp = resolve_question("What is the weather like?", known=TWO)
    v = judge(qp, [_rv(["spk:melanie", "sub:melanie"])])
    assert v.note is None


# ------------------------------------------------------------------------ entity check


def test_entity_names_exact_not_fuzzy() -> None:
    vocab = store_vocab(["Oliver: my dog Oliver loves bones", "Melanie: hi Caroline's pal"], TWO)
    assert missing_names("What did Oscar do with the bone?", vocab) == ["Oscar"]
    assert missing_names("What did Oliver do with the bone?", vocab) == []
    assert missing_names("What did Mel paint?", vocab) == []  # nickname = prefix of a stored name
    assert missing_names("What did Caroline's friend say?", vocab) == []
    assert missing_names("When did Caroline go in June?", vocab) == []  # month is not a name
    assert missing_names("Oscar went where?", vocab) == []  # first word is never a name
    assert entity_note(["Oscar"]) == "Note: Oscar is not mentioned in the memories."


def test_user_header_text() -> None:
    assert user_header(None) is None
    assert user_header("user") is None
    h = user_header("caroline")
    assert h is not None and h.startswith("You are the assistant. The user is Caroline.")


def test_injection_log_window_and_session() -> None:
    log = InjectionLog()
    log.record("a", None, ["r1"], 3)  # no session: nothing tracked
    assert log.factor("a", None, "r1", 0.5) == 1.0
    log.record("a", "s", ["r1", "r2"], 2)
    log.record("a", "s", ["r1"], 2)
    assert log.factor("a", "s", "r1", 0.5) == 0.5
    assert log.factor("a", "s", "r2", 0.5) == 0.75
    assert log.factor("a", "s", "r3", 0.5) == 1.0
    log.record("a", "s", ["r3"], 2)  # r1 slides out of... still in the last 2
    log.record("a", "s", ["r4"], 2)
    assert log.factor("a", "s", "r1", 0.5) == 1.0
    assert log.factor("a", "other", "r3", 0.5) == 1.0


# ------------------------------------------------------------------------ engine


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True, "policies": {"perspective": "heuristic"}}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _store(eng: Engine) -> None:
    await eng.write_messages(
        [
            {
                "role": "user",
                "speaker": "Caroline",
                "content": "Caroline: I went to a pride parade.",
            },
            {"role": "user", "speaker": "Melanie", "content": "Melanie: I painted a sunrise lake."},
            {"role": "user", "speaker": "Melanie", "content": "Melanie: I love pottery class."},
        ],
        namespace="a",
    )


async def _ctx(eng: Engine, query: str, asker: str | None = None, **kw: Any) -> list[str]:
    with asker_scope(asker):
        ctx = await eng.assemble(query, namespace="a", budget_tokens=2000, top_k=6, **kw)
    return [r.content for r in ctx.records]


async def test_engine_owner_swap_note_and_marks() -> None:
    eng = _engine(owner_check="both")
    await eng.start()
    try:
        await eng.write_messages(
            [
                {"role": "user", "speaker": "Caroline", "content": "Caroline: Hi Melanie, hello."},
                {
                    "role": "user",
                    "speaker": "Melanie",
                    "content": "Melanie: I painted a sunrise lake.",
                },
                {"role": "user", "speaker": "Melanie", "content": "Melanie: I painted a forest."},
            ],
            namespace="a",
        )
        lines = await _ctx(eng, "What did Caroline paint?")
        assert any(c.startswith("[Melanie, about Melanie] Melanie: I painted") for c in lines)
        notes = [c for c in lines if c.startswith("Note:")]
        assert len(notes) == 1 and "Caroline" in notes[0] and "Melanie" in notes[0]
        # nothing dropped: mark-only returns the same evidence as the default read
        base = _engine()
        await base.start()
        await base.write_messages(
            [
                {"role": "user", "speaker": "Caroline", "content": "Caroline: Hi Melanie, hello."},
                {
                    "role": "user",
                    "speaker": "Melanie",
                    "content": "Melanie: I painted a sunrise lake.",
                },
                {"role": "user", "speaker": "Melanie", "content": "Melanie: I painted a forest."},
            ],
            namespace="a",
        )
        plain = await _ctx(base, "What did Caroline paint?")
        await base.stop()
        kept = [c.split("] ", 1)[1] for c in lines if c.startswith("[")]
        assert sorted(kept) == sorted(plain)
    finally:
        await eng.stop()


async def test_engine_two_people_keeps_both() -> None:
    eng = _engine(owner_check="both")
    await eng.start()
    try:
        await _store(eng)
        lines = await _ctx(eng, "Do Caroline and Melanie go to pride parade and pottery class?")
        assert not any(c.startswith("Note:") for c in lines)
        assert any("Caroline:" in c for c in lines) and any("Melanie:" in c for c in lines)
    finally:
        await eng.stop()


async def test_engine_chat_user_assistant() -> None:
    eng = _engine(owner_check="both", user_header="on")
    await eng.start()
    try:
        await eng.write_messages(
            [
                {"role": "user", "content": "I love hiking in the mountains."},
                {"role": "assistant", "content": "I recommend the Alps trail for hiking."},
            ],
            namespace="a",
        )
        lines = await _ctx(eng, "What hiking trail did you recommend?", asker="sam")
        assert lines[0].startswith("You are the assistant. The user is Sam.")
        assert any(c.startswith("[the assistant, about the assistant]") for c in lines)
        assert not any(c.startswith("Note:") for c in lines)
    finally:
        await eng.stop()


async def test_engine_entity_check_note() -> None:
    eng = _engine(entity_check="note")
    await eng.start()
    try:
        await _store(eng)
        lines = await _ctx(eng, "What did Oscar paint?")
        assert "Note: Oscar is not mentioned in the memories." in lines
        lines = await _ctx(eng, "What did Mel paint?")
        assert not any(c.startswith("Note:") for c in lines)
    finally:
        await eng.stop()


async def test_engine_user_header_needs_a_known_asker() -> None:
    eng = _engine(user_header="on")
    await eng.start()
    try:
        await _store(eng)
        none = await _ctx(eng, "What did Melanie paint?")
        assert not any(c.startswith("You are the assistant") for c in none)
        some = await _ctx(eng, "What did Melanie paint?", asker="caroline")
        assert some[0] == (
            "You are the assistant. The user is Caroline. "
            "Memory lines are labelled with their speaker."
        )
    finally:
        await eng.stop()


async def test_engine_reinjection_penalty_only_with_session() -> None:
    eng = _engine(reinjection_penalty=1.0, reinjection_window=3)
    await eng.start()
    try:
        await _store(eng)
        q = "pottery class"
        first = await _ctx(eng, q, session_id="s1")
        used = eng._injections.uses("a", "s1")
        assert len(used) == len(first)  # every injected record is tracked for the session
        second = await _ctx(eng, q, session_id="s1")
        assert sorted(second) == sorted(first)  # penalised, never dropped
        # independent QA (no session id): identical results every time, nothing tracked
        a = await _ctx(eng, q)
        b = await _ctx(eng, q)
        assert a == b == first
        assert not eng._injections.uses("a", None)
    finally:
        await eng.stop()
