"""I4 intent list trigger and I5 subject-based speaker vote. Opt-in; defaults unchanged."""

from __future__ import annotations

import pytest

from memspine.config.schema import ReadConfig
from memspine.core.query_shape import is_intent_list
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.core.temporal_query import (
    LegHit,
    question_subject,
    speaker_vector_leg,
    subject_vector_leg,
)

# -- I4 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "How many tops have I bought from H&M so far?",
        "What is the total number of episodes I have watched?",
        "How many bikes do I own?",
        "How much have I spent on groceries?",
        "List all the books she read.",
        "What do Jon and Gina have in common?",
        "What gifts did Caroline buy?",
        "What kind of books does Melanie read?",
        "I read a lot lately. How many books have I finished?",
        "What is the order of the three trips I took?",
    ],
)
def test_intent_fires(question: str) -> None:
    assert is_intent_list(question)


@pytest.mark.parametrize(
    "question",
    [
        "What type of camera lens did I purchase most recently?",
        "What kind of car did I buy last?",
        "When did Melanie sign up for a pottery class?",
        "Why did Jon shut down his bank account?",
        "How many days ago did I harvest my first herbs?",
        "What does Joanna love most about having turtles?",
        "What is her favourite dish?",
        "Is Oscar Melanie's pet?",
        "Can you remind me of the name of the last venue you recommended in the list of venues?",
    ],
)
def test_intent_quiet(question: str) -> None:
    assert not is_intent_list(question)


def test_intent_is_a_trigger_value_and_default_unchanged() -> None:
    assert ReadConfig().list_trigger == "set_question"
    assert ReadConfig(list_trigger="intent").list_trigger == "intent"


# -- I5 -----------------------------------------------------------------------------------


def _rec(text: str, role: str = "user", tags: list[str] | None = None) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=text,
        source=SourceInfo(role=role),
        tags=tags or [],
    )


def _ids(hits: list[LegHit]) -> list[str]:
    return [h.record_id for h in hits]


def test_default_mode_is_name() -> None:
    assert ReadConfig().speaker_vote_mode == "name"


def test_question_subject() -> None:
    assert question_subject("What pets do I have?") == "user"
    assert question_subject("What did you recommend for my trip?") is None  # mixed
    assert question_subject("What did you recommend?") == "assistant"
    assert question_subject("What pets does she have?") is None
    assert question_subject("What pets does Caroline have?") is None


def test_two_named_speakers_vote_by_name() -> None:
    recs = [
        _rec("Caroline: I adopted a cat."),
        _rec("Melanie: I painted a lake."),
        _rec("Caroline: My dog is old."),
    ]
    hits = [LegHit(r.record_id, 1.0 - i / 10) for i, r in enumerate(recs)]
    expected = [recs[0].record_id, recs[2].record_id]
    assert _ids(subject_vector_leg("What pets does Caroline have?", recs, hits)) == expected
    # same as the name mode where a speaker is named
    assert subject_vector_leg("What pets does Caroline have?", recs, hits) == speaker_vector_leg(
        "What pets does Caroline have?", recs, hits
    )
    # a pronoun is unresolved: no vote; "I" in a two-named-speaker chat (every role=user): none
    assert subject_vector_leg("What pets does she have?", recs, hits) == []
    assert subject_vector_leg("What pets do I have?", recs, hits) == []
    # a named non-speaker casts no vote
    assert subject_vector_leg("What pets does Zed have?", recs, hits) == []


def test_user_assistant_chat_vote_by_role_metadata() -> None:
    recs = [
        _rec("I adopted a cat last week.", "user"),
        _rec("Cats need a vet visit; here are tips.", "assistant"),
        _rec("My dog is old.", "user"),
        _rec("Dogs can have joint supplements.", "assistant"),
    ]
    hits = [LegHit(r.record_id, 1.0 - i / 10) for i, r in enumerate(recs)]
    assert _ids(subject_vector_leg("What pets do I have?", recs, hits)) == [
        recs[0].record_id,
        recs[2].record_id,
    ]
    assert _ids(subject_vector_leg("Which supplements did you suggest?", recs, hits)) == [
        recs[1].record_id,
        recs[3].record_id,
    ]
    assert subject_vector_leg("What pets does she have?", recs, hits) == []
    assert subject_vector_leg("What did you say about my dog?", recs, hits) == []  # mixed
    # name mode (speaker_vector_leg) stays inert on this shape
    assert speaker_vector_leg("What pets do I have?", recs, hits) == []


def test_user_assistant_chat_vote_by_text_prefix() -> None:
    # loaders that fold the role into the text; every record has role=user
    recs = [
        _rec("user: I adopted a cat.", "user"),
        _rec("assistant: Cats need a vet.", "user"),
        _rec("User: My dog is old.", "user"),
    ]
    hits = [LegHit(r.record_id, 1.0 - i / 10) for i, r in enumerate(recs)]
    assert _ids(subject_vector_leg("What pets do I have?", recs, hits)) == [
        recs[0].record_id,
        recs[2].record_id,
    ]


def test_single_role_store_casts_no_role_vote() -> None:
    recs = [_rec("I adopted a cat.", "user"), _rec("My dog is old.", "user")]
    hits = [LegHit(r.record_id, 1.0) for r in recs]
    assert subject_vector_leg("What pets do I have?", recs, hits) == []
