"""Qwen3 thinking control: the ``/no_think`` switch, think-block stripping, usage ledger."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.prompts.base import PromptFormat
from memspine.prompts.models import ExtractedFacts
from memspine.services.llm.base import LLMRouter
from memspine.services.llm.litellm_llm import LiteLLMLLM, strip_think
from memspine.services.llm.structured import _parse_payload

QWEN = "bedrock/converse/qwen.qwen3-32b-v1:0"


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Response:
    def __init__(self, content: str, usage: _Usage | None = None) -> None:
        self.choices = [_Choice(content)]
        self.usage = usage


def _capture(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[dict[str, Any]]:
    import litellm

    seen: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> _Response:
        seen.append(kwargs)
        return _Response(reply, _Usage(11, 7))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


async def test_no_think_suffix_added_for_qwen3_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, "ok")
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "second"},
    ]
    await LiteLLMLLM(QWEN).chat(messages)
    await LiteLLMLLM("openai/gpt-4o").chat(messages)
    qwen_sent, gpt_sent = seen[0]["messages"], seen[1]["messages"]
    assert qwen_sent[-1]["content"] == "second /no_think"
    assert qwen_sent[1]["content"] == "first"  # only the LAST user message
    assert qwen_sent[0]["content"] == "be brief"
    assert gpt_sent[-1]["content"] == "second"
    assert messages[-1]["content"] == "second"  # the caller's list is not mutated


async def test_no_think_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, "ok")
    await LiteLLMLLM(QWEN, no_think=False).chat([{"role": "user", "content": "q"}])
    await LiteLLMLLM("ollama/llama3", no_think=True).chat([{"role": "user", "content": "q"}])
    await LiteLLMLLM(QWEN).chat([{"role": "user", "content": "q /no_think"}])
    assert seen[0]["messages"][-1]["content"] == "q"
    assert seen[1]["messages"][-1]["content"] == "q /no_think"
    assert seen[2]["messages"][-1]["content"] == "q /no_think"  # never doubled


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("<think>\nlong reasoning\n</think>\n\nParis", "Paris"),
        ("<think>a</think>x<think>b</think>y", "xy"),
        ("reasoning only</think>answer", "answer"),
        ("answer<think>cut off mid-thought", "answer"),
        ("  plain reply  ", "  plain reply  "),  # untouched without think tags
    ],
)
def test_strip_think_lenient(raw: str, clean: str) -> None:
    assert strip_think(raw, lenient=True) == clean


@pytest.mark.parametrize(
    "raw",
    [
        "use the <think> tag to open a block",
        "close it with </think> at the end",
        "x <think>b</think> y",
    ],
)
def test_strip_think_keeps_quoted_tags(raw: str) -> None:
    """B-3: outside thinking models only a leading block is reasoning."""
    assert strip_think(raw) == raw


def test_strip_think_strips_leading_block_for_any_model() -> None:
    assert strip_think("  <think>a</think>\nanswer") == "answer"


async def test_quoted_think_tag_survives_non_thinking_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = "Write </think> to end a block."
    _capture(monkeypatch, reply)
    assert await LiteLLMLLM("openai/gpt-4o").chat([{"role": "user", "content": "q"}]) == reply


async def test_think_block_stripped_for_every_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, "<think>hmm</think>\nhello")
    assert await LiteLLMLLM("openai/gpt-4o").chat([{"role": "user", "content": "q"}]) == "hello"


async def test_structured_parse_after_stripping(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = (
        "<think>The user wants facts. I will list them.\nfacts: [nonsense</think>\n"
        "facts:\n  - entity: ana\n    attribute: city\n    value: Ana lives in Lyon\n"
    )
    _capture(monkeypatch, raw)
    reply = await LiteLLMLLM(QWEN).chat([{"role": "user", "content": "extract"}])
    parsed = ExtractedFacts.model_validate(_parse_payload(reply, PromptFormat.YAML))
    assert [f.value for f in parsed.facts] == ["Ana lives in Lyon"]


async def test_usage_totals_and_router_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, "ok")
    llm = LiteLLMLLM(QWEN)
    router = LLMRouter({"extract": llm})
    await router.for_role("extract").chat([{"role": "user", "content": "q"}])
    await router.for_role("extract").chat([{"role": "user", "content": "q"}])
    assert llm.usage_totals == [22, 14]
    assert router.token_counts() == {"extract": {"prompt": 22, "completion": 14}}
    assert router.models() == {"extract": QWEN}


class _Plain:
    """A provider without usage reporting: the router estimates at 4 chars/token."""

    provider_id = "stub:plain"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        return "x" * 40


async def test_router_estimates_tokens_without_usage() -> None:
    router = LLMRouter({"judge": _Plain()})
    await router.for_role("judge").chat([{"role": "user", "content": "y" * 80}])
    assert router.token_counts() == {"judge": {"prompt": 20, "completion": 10}}


async def test_engine_binds_no_think_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        llm={
            "roles": {
                "extract": {"model": QWEN},
                "chat": {"model": QWEN, "no_think": False},
                "judge": {"model": "openai/gpt-4o"},
            }
        },
    )
    await eng.start()
    try:
        providers = {r: eng._llm.provider(r) for r in ("extract", "chat", "judge")}  # type: ignore[union-attr]
        assert providers["extract"].no_think is True  # type: ignore[attr-defined]
        assert providers["chat"].no_think is False  # type: ignore[attr-defined]
        assert providers["judge"].no_think is False  # type: ignore[attr-defined]
        _capture(monkeypatch, "<think>x</think>fine")
        assert await eng.llm("extract").chat([{"role": "user", "content": "q"}]) == "fine"
        assert eng.model_usage() == {
            "extract": {"model": QWEN, "calls": 1, "prompt": 11, "completion": 7}
        }
    finally:
        await eng.stop()


def _fake_llama(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[list[dict[str, str]]]:
    import sys
    import types

    seen: list[list[dict[str, str]]] = []

    class Llama:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def create_chat_completion(self, messages: Any, **options: Any) -> dict[str, Any]:
            seen.append(messages)
            return {"choices": [{"message": {"content": reply}}]}

    module = types.ModuleType("llama_cpp")
    module.Llama = Llama  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    return seen


async def test_llama_cpp_strips_think_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-7: the in-process provider strips a think block like the LiteLLM one."""
    from memspine.services.llm.llama_cpp import LlamaCppLLM

    _fake_llama(monkeypatch, "<think>x</think>facts: []")
    reply = await LlamaCppLLM(model_path="m.gguf").chat([{"role": "user", "content": "q"}])
    assert reply == "facts: []"
    parsed = ExtractedFacts.model_validate(_parse_payload(reply, PromptFormat.YAML))
    assert parsed.facts == []


async def test_llama_cpp_honours_no_think(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.services.llm.llama_cpp import LlamaCppLLM

    seen = _fake_llama(monkeypatch, "ok")
    await LlamaCppLLM(model_path="Qwen3-8B-Q4.gguf").chat([{"role": "user", "content": "q"}])
    await LlamaCppLLM(model_path="m.gguf", no_think=True).chat([{"role": "user", "content": "q"}])
    await LlamaCppLLM(model_path="m.gguf").chat([{"role": "user", "content": "q"}])
    assert [s[-1]["content"] for s in seen] == ["q /no_think", "q /no_think", "q"]


def test_lenient_yaml_maps_nulls_to_none() -> None:
    """B-4: ``null``, ``~`` and empty values salvage as None, not strings."""
    from memspine.services.llm.structured import _lenient_yaml_items

    text = (
        "facts:\n"
        "  - entity: ana\n"
        "    attribute: null\n"
        "    value: met at 10: 30\n"
        "  - entity: bo\n"
        "    attribute: ~\n"
        "    when:\n"
        "    note: 'null'\n"
    )
    assert _lenient_yaml_items(text) == {
        "facts": [
            {"entity": "ana", "attribute": None, "value": "met at 10: 30"},
            {"entity": "bo", "attribute": None, "when": None, "note": "null"},
        ]
    }


def test_lenient_yaml_refuses_nested_lists() -> None:
    """B-4: structure under a field is not folded into text; salvage gives up."""
    from memspine.services.llm.structured import _lenient_yaml_items

    text = "facts:\n  - entity: ana\n    aliases:\n      - Annie\n      - A\n"
    assert _lenient_yaml_items(text) is None
    mapping = "facts:\n  - entity: ana\n    meta:\n      source: chat\n"
    assert _lenient_yaml_items(mapping) is None
