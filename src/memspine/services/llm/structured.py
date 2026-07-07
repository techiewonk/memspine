"""Structured LLM output (D-31): typed responses with an always-on safety net.

Core path: chat → YAML/JSON parse with json-repair fallback → pydantic
validation, honoring the prompt's E9 format (YAML answers cost roughly half
the tokens of JSON). The ``[structured]`` instructor adapter can replace the
parse step for OpenAI-compatible endpoints; the core path never requires it.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel

from memspine.exceptions import LLMError
from memspine.prompts.base import Prompt, PromptFormat
from memspine.services.llm.base import LLMService, lenient_json

__all__ = ["structured_call"]


def _parse_payload(text: str, format_: PromptFormat) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip a markdown fence if the model added one despite instructions.
        cleaned = cleaned.strip("`")
        first_newline = cleaned.find("\n")
        if first_newline != -1 and cleaned[:first_newline].strip() in {"yaml", "json", ""}:
            cleaned = cleaned[first_newline + 1 :]
    if format_ in {PromptFormat.YAML, PromptFormat.COD}:
        try:
            return yaml.safe_load(cleaned)
        except yaml.YAMLError:
            pass  # fall through to the JSON-repair net
    return lenient_json(cleaned)


async def structured_call[ModelT: BaseModel](
    llm: LLMService,
    prompt: Prompt,
    context: dict[str, object],
    output_model: type[ModelT],
    **chat_options: Any,
) -> ModelT:
    """Render → chat → parse (format-aware) → validate. Raises LLMError with
    the raw response attached when validation fails even after repair."""
    raw = await llm.chat(prompt.render(context), **chat_options)
    payload = _parse_payload(raw, prompt.format)
    try:
        return output_model.model_validate(payload)
    except Exception as exc:
        raise LLMError(
            f"prompt {prompt.prompt_version}: response failed validation against "
            f"{output_model.__name__}: {exc}; raw (truncated): {raw[:300]}"
        ) from exc
