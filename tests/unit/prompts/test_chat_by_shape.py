"""C2: ``prompts.selection.chat_by_shape`` routes the chat prompt by question shape.

Unset, every chat render is exactly today's (golden guard below); set, temporal and
inference questions take the mapped ``chat`` condition and every other question keeps
the role's default selection.
"""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.core.query_shape import is_inference, is_temporal
from memspine.exceptions import ConfigError
from memspine.prompts.registry import PromptRegistry, question_shape

TEMPORAL = "When did Ana go hiking?"
INFERENCE = "Would Ana enjoy a hiking holiday?"
PLAIN = "What pet does Ana have?"
_ROUTED = {"temporal": "temporal", "inference": "inference"}


@pytest.mark.parametrize(
    ("question", "shape"),
    [
        (TEMPORAL, "temporal"),
        ("How long ago did Ana adopt the dog?", "temporal"),
        ("When would Ana likely visit Paris?", "temporal"),  # a date question wins
        (INFERENCE, "inference"),
        ("Is it likely that Nate has friends besides Joanna?", "inference"),
        ("What might John's financial status be?", "inference"),
        ("Could Melanie be a member of the LGBTQ community?", "inference"),
        (PLAIN, None),
        ("What did Ana say about the hike", None),  # no question mark: not inference
        ("Ana couldn't come", None),
    ],
)
def test_question_shape(question: str, shape: str | None) -> None:
    assert question_shape(question) == shape


def test_is_inference_is_word_bounded() -> None:
    assert is_inference("Would she go?") and is_inference("is it LIKELY she goes?")
    assert not is_inference("Who is unlikely to say?")  # "unlikely" is not "likely"
    assert not is_inference("Would she go")
    assert not is_temporal(INFERENCE)


@pytest.mark.parametrize("question", [TEMPORAL, INFERENCE, PLAIN])
@pytest.mark.parametrize("selection", [{}, {"chat": {"condition": "dated"}}])
def test_unset_is_exactly_select(question: str, selection: dict[str, Any]) -> None:
    registry = PromptRegistry(selection=selection)
    assert registry.select_for_question("chat", question) is registry.select("chat")


def test_mapping_routes_by_shape_and_keeps_the_default_otherwise() -> None:
    registry = PromptRegistry(selection={"chat": {"condition": "dated"}, "chat_by_shape": _ROUTED})
    assert registry.select_for_question("chat", TEMPORAL).id == "chat@temporal"
    assert registry.select_for_question("chat", INFERENCE).id == "chat@inference"
    assert registry.select_for_question("chat", PLAIN).id == "chat@dated"
    # An explicit per-call condition wins over the shape.
    assert registry.select_for_question("chat", TEMPORAL, condition="").id == "chat"
    # Only the mapped shapes route; the role's plain select() is untouched.
    partial = PromptRegistry(selection={"chat_by_shape": {"temporal": "infer"}})
    assert partial.select_for_question("chat", INFERENCE).id == "chat"
    assert partial.select("chat").id == "chat"


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ({"chat_by_shape": {"aggregation": "infer"}}, "unknown question shape"),
        ({"chat_by_shape": {"temporal": "infre"}}, "no 'chat' prompt with condition"),
    ],
)
def test_bad_mapping_is_a_config_error(selection: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        PromptRegistry(selection=selection)


def test_base_condition_is_allowed() -> None:
    registry = PromptRegistry(
        selection={"chat": {"condition": "dated"}, "chat_by_shape": {"inference": ""}}
    )
    assert registry.select_for_question("chat", INFERENCE).id == "chat"


def test_routed_chat_prompts_carry_the_h11_wording() -> None:
    """Full-sentence answers kept; no terse-answer instruction; no refusal on inference."""
    registry = PromptRegistry()
    temporal = registry.get("chat@temporal").render({"context": "", "message": "q"})
    inference = registry.get("chat@inference").render({"context": "", "message": "q"})
    t, i = temporal[0]["content"], inference[0]["content"]
    assert "DD Month YYYY" in t and "relative phrase" in t
    assert "say 'likely'" in i and "do not know" not in i
    for text in (t, i):
        assert "very concise" not in text and "short answer only" not in text
        assert "Not mentioned" not in text


def _engine(**prompts: Any) -> Engine:
    return Engine(
        template="assistant",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        **({"prompts": prompts} if prompts else {}),
    )


async def test_chat_messages_unchanged_when_unset() -> None:
    """Golden: unset, ``chat_messages`` renders what ``select('chat')`` renders today."""
    eng = _engine()
    await eng.start()
    try:
        assert eng._prompts is not None
        for question in (TEMPORAL, INFERENCE, PLAIN):
            expected = eng._prompts.select("chat").render({"context": "c", "message": question})
            assert eng.chat_messages(question, "c") == expected
            assert "computed from the line's own date" in expected[0]["content"]
    finally:
        await eng.stop()


async def test_chat_messages_routes_when_set() -> None:
    eng = _engine(selection={"chat_by_shape": _ROUTED})
    await eng.start()
    try:
        assert "DD Month YYYY" in eng.chat_messages(TEMPORAL, "c")[0]["content"]
        assert "say 'likely'" in eng.chat_messages(INFERENCE, "c")[0]["content"]
        plain = eng.chat_messages(PLAIN, "c")[0]["content"]
        assert "computed from the line's own date" in plain  # still chat@dated
        assert "DD Month YYYY" not in plain and "'likely'" not in plain
        base = eng.chat_messages(TEMPORAL, "c", condition="")  # per-call override
        assert "computed from" not in base[0]["content"]
    finally:
        await eng.stop()
