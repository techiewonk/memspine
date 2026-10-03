"""An offline stand-in for LiteLLM's transport, for rehearsals and tests (G5).

``install_stub_litellm()`` replaces ``litellm.acompletion`` and ``litellm.aembedding``
with deterministic local functions. Everything above the transport runs for real:
the reader and judge, the call and dollar budgets, the engine's LLM roles with their
``/no_think`` switch and think stripping, structured parsing, and the token ledger.
No request leaves the process.

The completion stub recognises who is calling from the prompt itself:

* an engine role, by its shipped system prompt (``facts`` / ``cues`` / ``insights``
  / ``labels`` / ``edges`` lists, conflict verdicts, summaries, rewrites);
* a harness judge, by its reply format (a JSON ``label`` or "yes or no");
* otherwise the reader, which gets a short plausible answer.

Like Qwen3 in thinking mode, it opens every reply with a ``<think>`` block unless
the last user message carries ``/no_think``, so a missing switch is visible in the
token counts and a missing stripper breaks parsing, as it would on the real model.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

__all__ = ["StubLiteLLM", "install_stub_litellm"]

_LINE_DATE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
_NUMBERED = re.compile(r"^\s*\[(\d+)\]", re.M)
_WORD = re.compile(r"[a-z0-9]+")
_THINK = "<think>\nLet me work through this step by step before answering.\n</think>\n\n"


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _content_lines(text: str, marker: str) -> list[str]:
    """Transcript lines after ``marker`` (e.g. ``transcript: |``) in a rendered prompt."""
    _, _, tail = text.partition(marker)
    lines = [line.strip() for line in tail.splitlines()]
    return [line for line in lines if line and not line.endswith(":")]


def _quote(text: str) -> str:
    return '"' + text.replace("\\", " ").replace('"', "'")[:160] + '"'


class StubLiteLLM:
    """The stub transport. ``calls`` counts completions per detected caller kind."""

    def __init__(self, think_unless_no_think: bool = True) -> None:
        self.think_unless_no_think = think_unless_no_think
        self.calls: Counter[str] = Counter()
        self.embeddings = 0
        #: completions whose last user message lacked /no_think (Qwen3 would think)
        self.thinking_calls = 0

    # -- completion ------------------------------------------------------------

    def classify(self, messages: list[dict[str, Any]]) -> str:
        system = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        user = str(messages[-1].get("content", "")) if messages else ""
        text = f"{system}\n{user}"
        markers = (
            ("under the key `cues`", "anticipate"),
            ("Extract the atomic facts from this session transcript", "extract_session"),
            ("under the key `facts`", "extract"),
            ("under the key `edges`", "extract_edges"),
            ("under the key `labels`", "relevance"),
            ("under `insights`", "reflect"),
            ("verdict (one of", "judge_conflict"),
            ("instruction_shaped", "firewall_flag"),
            ("compact summaries", "summarize"),
            ("alternative search queries", "query_rewrite"),
        )
        for marker, kind in markers:
            if marker in text:
                return kind
        if '"label"' in text:
            return "harness_judge"
        if "Answer yes or no only" in text:
            return "harness_judge_yes_no"
        if system and "memory" in system.lower():
            return "engine_other"
        return "reader"

    def reply(self, kind: str, messages: list[dict[str, Any]]) -> str:
        user = str(messages[-1].get("content", "")) if messages else ""
        if kind == "extract_session":
            facts = []
            for line in _content_lines(user, "transcript: |")[:3]:
                date = _LINE_DATE.search(line)
                body = _LINE_DATE.sub("", line).strip()
                entity = body.split(":", 1)[0].strip().lower() or "speaker"
                facts.append(
                    f"  - entity: {_quote(entity)}\n    attribute: event\n"
                    f"    value: {_quote(body)}\n    date: {date.group(1) if date else '~'}\n"
                    "    confidence: 0.8"
                )
            return "facts:\n" + "\n".join(facts) if facts else "facts: []"
        if kind == "extract":
            lines = _content_lines(user, "content: |")[:1]
            if not lines:
                return "facts: []"
            return (
                f"facts:\n  - entity: speaker\n    attribute: statement\n"
                f"    value: {_quote(lines[0])}\n    confidence: 0.7"
            )
        if kind == "anticipate":
            numbers = [int(n) for n in _NUMBERED.findall(user)][:2]
            cues = [
                f"  - line: {n}\n    cue: What did they say about this later on?" for n in numbers
            ]
            return "cues:\n" + "\n".join(cues) if cues else "cues: []"
        if kind == "relevance":
            numbers = [int(n) for n in _NUMBERED.findall(user)]
            labels = [
                f"  - index: {n}\n    label: {'irrelevant' if i % 3 == 2 else 'relevant'}"
                for i, n in enumerate(numbers)
            ]
            return "labels:\n" + "\n".join(labels) if labels else "labels: []"
        if kind == "reflect":
            return (
                "insights:\n  - insight: The person values their close friends.\n    evidence: [0]"
            )
        if kind == "extract_edges":
            return "edges: []"
        if kind == "judge_conflict":
            return "verdict: add\nreason: the two memories are independent."
        if kind == "firewall_flag":
            return "instruction_shaped: false\nreason: ordinary content."
        if kind == "summarize":
            body = " ".join(user.split())[-300:]
            return f"Summary: {body[:200]}"
        if kind == "query_rewrite":
            question = user.rsplit("question:", 1)[-1].strip()[:80]
            return f"{question} details\nwhen did {question.lower()}"
        if kind == "harness_judge":
            return '{"label": "CORRECT"}'
        if kind == "harness_judge_yes_no":
            return "yes"
        if kind == "engine_other":
            return "ok"
        return "They went to the support group in May 2023."

    async def acompletion(self, **kwargs: Any) -> Any:
        messages = list(kwargs.get("messages") or [])
        kind = self.classify(messages)
        self.calls[kind] += 1
        text = self.reply(kind, messages)
        last_user = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), ""
        )
        if not last_user.rstrip().endswith("/no_think"):
            self.thinking_calls += 1
            if self.think_unless_no_think:
                text = _THINK + text
        prompt_tokens = sum(_tokens(str(m.get("content", ""))) for m in messages)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text), finish_reason="stop", index=0
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=_tokens(text),
                total_tokens=prompt_tokens + _tokens(text),
                prompt_tokens_details=None,
                cache_read_input_tokens=None,
            ),
            model=kwargs.get("model"),
        )

    # -- embedding -------------------------------------------------------------

    @staticmethod
    def vector(text: str, dim: int) -> list[float]:
        """A deterministic bag-of-words hash vector, L2-normalised (lexical similarity)."""
        vec = [0.0] * dim
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % dim
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def aembedding(self, **kwargs: Any) -> Any:
        inputs = kwargs.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        dim = int(kwargs.get("dimensions") or 1024)
        self.embeddings += len(inputs)
        data = [{"embedding": self.vector(str(t), dim), "index": i} for i, t in enumerate(inputs)]
        return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=0, total_tokens=0))


@contextmanager
def install_stub_litellm(stub: StubLiteLLM | None = None) -> Iterator[StubLiteLLM]:
    """Swap LiteLLM's network entry points for ``stub`` for the duration of the block."""
    import litellm

    stub = stub or StubLiteLLM()
    saved = (litellm.acompletion, litellm.aembedding)
    litellm.acompletion = stub.acompletion
    litellm.aembedding = stub.aembedding
    try:
        yield stub
    finally:
        litellm.acompletion, litellm.aembedding = saved
