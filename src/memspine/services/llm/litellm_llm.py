"""Unified LLM provider via LiteLLM (D-07/D-33/D-39): cloud + OpenAI-compatible
local behind one adapter (supersedes the hand-rolled ``OpenAICompatLLM``).

The litellm ``model`` string carries the provider — ``openai/gpt-4o``,
``ollama/llama3`` (local; set ``api_base``), ``bedrock/anthropic.claude-...``
(set ``aws_region`` or rely on the boto3 credential chain), ``vertex_ai/...``,
``azure/...``, or any of litellm's 100+ providers. litellm is a core dep now
(amends D-03/D-33) but imported lazily on first ``chat`` so a default engine
with no LLM role never pays its multi-second import cost.

Qwen3 thinks by default and emits a ``<think>...</think>`` block before its
answer: the block costs output tokens and breaks YAML/JSON parsing of the
structured roles. The adapter appends Qwen's documented ``/no_think`` soft
switch to the last user message (on by default for ``qwen3`` model ids). A
think block that leads a reply is stripped for every model; the repair rules
for unclosed or stray tags apply only to thinking models (see ``strip_think``).
"""

from __future__ import annotations

import re
from typing import Any

from memspine.exceptions import LLMError
from memspine.services._retry import retry_transient

__all__ = ["LiteLLMLLM", "default_no_think", "strip_think"]

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
_LEADING_THINK = re.compile(r"^\s*<think>.*?</think>", re.S)
_NO_THINK = "/no_think"


def default_no_think(model: str) -> bool:
    """Whether ``/no_think`` is on by default for ``model``: Qwen3 ids only."""
    return "qwen3" in model.lower()


def strip_think(text: str, *, lenient: bool = False) -> str:
    """Remove reasoning blocks from a reply.

    Always: a closed ``<think>...</think>`` block that *leads* the reply is
    dropped. A ``<think>`` tag elsewhere is content (an answer may quote the
    tag) and is left alone.

    ``lenient`` (a thinking model: ``no_think`` active or a ``qwen3`` id) adds
    the repair rules: every closed block is dropped, an unclosed ``<think>`` (a
    reply cut off mid-reasoning) drops everything after it, and a stray
    ``</think>`` with no opening tag (templates that open the block themselves)
    keeps only what follows it.
    """
    cleaned = _LEADING_THINK.sub("", text, count=1)
    if lenient:
        cleaned = _THINK_BLOCK.sub("", cleaned)
        if "</think>" in cleaned:
            cleaned = cleaned.rsplit("</think>", 1)[1]
        if "<think>" in cleaned:
            cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip() if cleaned != text else text


def _with_no_think(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """A copy of ``messages`` whose last user message ends with ``/no_think``."""
    out = [dict(message) for message in messages]
    for message in reversed(out):
        if message.get("role") == "user":
            content = str(message.get("content", ""))
            if not content.rstrip().endswith(_NO_THINK):
                message["content"] = f"{content} {_NO_THINK}"
            break
    return out


class LiteLLMLLM:
    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        aws_region: str | None = None,
        timeout_seconds: float = 60.0,
        no_think: bool | None = None,
    ) -> None:
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._aws_region = aws_region
        self._timeout = timeout_seconds
        self.no_think = default_no_think(model) if no_think is None else no_think
        #: Provider-reported tokens over this adapter's lifetime: prompt, completion.
        #: Read by the router's token ledger (``LLMRouter.token_counts``).
        self.usage_totals: list[int] = [0, 0]

    @property
    def provider_id(self) -> str:
        return f"litellm:{self._model}"

    @property
    def model(self) -> str:
        return self._model

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        import litellm

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": _with_no_think(messages) if self.no_think else messages,
            "timeout": self._timeout,
        }
        if self._api_base is not None:
            kwargs["api_base"] = self._api_base
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if self._aws_region is not None:
            kwargs["aws_region_name"] = self._aws_region
        kwargs.update(options)
        try:
            response = await retry_transient(
                lambda: litellm.acompletion(**kwargs), what=f"chat:{self._model}"
            )
            content = response.choices[0].message.content
        except Exception as exc:
            raise LLMError(f"litellm chat failed for model {self._model!r}: {exc}") from exc
        usage = getattr(response, "usage", None)
        self.usage_totals[0] += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.usage_totals[1] += int(getattr(usage, "completion_tokens", 0) or 0)
        if content is None:
            raise LLMError(f"litellm returned empty content for model {self._model!r}")
        lenient = self.no_think or default_no_think(self._model)
        return strip_think(str(content), lenient=lenient)
