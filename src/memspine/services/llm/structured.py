"""Structured LLM output (D-31): typed responses with an always-on safety net.

Core path: chat → YAML/JSON parse with json-repair fallback → pydantic
validation, honoring the prompt's E9 format (YAML answers cost roughly half
the tokens of JSON). The ``[structured]`` instructor adapter can replace the
parse step for OpenAI-compatible endpoints; the core path never requires it.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel

from memspine.exceptions import LLMError
from memspine.observability.logging import get_logger
from memspine.prompts.base import Prompt, PromptFormat
from memspine.services.llm.base import LLMService, lenient_json

__all__ = ["structured_call"]

_log = get_logger(__name__)


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
        except yaml.YAMLError as exc:
            # Keep the exact parse error visible before the repair net hides it
            # — it pinpoints the malformed line when a format regression hits.
            _log.debug("structured.yaml_parse_failed", error=str(exc))
            # Models often leave a value unquoted that contains ": " (a quoted
            # title, a time). Strict YAML rejects the whole reply; the line-based
            # reader keeps every item by splitting each field on its FIRST colon.
            salvaged = _lenient_yaml_items(cleaned)
            if salvaged is not None:
                return salvaged
    return lenient_json(cleaned)


_TOP = re.compile(r"^([A-Za-z_][\w-]*):\s*(\[\])?\s*$")
_ITEM = re.compile(r"^\s*-\s+([A-Za-z_][\w-]*):\s?(.*)$")
_FIELD = re.compile(r"^\s+([A-Za-z_][\w-]*):\s?(.*)$")


#: Plain YAML scalars that mean null (an unquoted empty value included).
_NULLS = frozenset({"", "~", "null", "Null", "NULL"})


def _scalar(value: str) -> str | None:
    """A field value: quotes removed, YAML's null spellings mapped to ``None``
    (a quoted ``"null"`` stays the string)."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return None if value in _NULLS else value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _lenient_yaml_items(text: str) -> dict[str, list[dict[str, str | None]]] | None:
    """Salvage the ``key:`` + list-of-flat-mappings shape every structured prompt
    answers in (``facts:``, ``cues:``, ``labels:`` ...). Values stay strings (or
    ``None`` for YAML nulls) and pydantic coerces them.

    Returns None for any other shape, including a nested list or mapping under
    a field (a line deeper than the item's fields that opens a ``- `` entry or a
    ``key:``), so the caller falls through to json-repair instead of folding
    the structure into text."""
    out: dict[str, list[dict[str, str | None]]] = {}
    key: str | None = None
    item: dict[str, str | None] | None = None
    field_indent = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        depth = _indent(line)
        nested = item is not None and depth > field_indent
        if m := _TOP.match(line):
            key = m.group(1)
            out[key] = []
            item = None
        elif nested and (line.lstrip().startswith("- ") or _FIELD.match(line)):
            return None  # B-4: structure under a field, not a flat mapping
        elif (m := _ITEM.match(line)) and key is not None:
            item = {m.group(1): _scalar(m.group(2))}
            field_indent = m.start(1)
            out[key].append(item)
        elif (m := _FIELD.match(line)) and item is not None:
            item[m.group(1)] = _scalar(m.group(2))
        elif item is not None and line.startswith((" ", "\t")):
            last = next(reversed(item))  # continuation of a wrapped value
            if item[last] is None:
                return None  # a block value under an empty field
            item[last] = f"{item[last]} {line.strip()}".strip()
        else:
            return None
    return out or None


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
