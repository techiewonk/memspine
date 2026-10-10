"""Structured LLM output (D-31): typed responses with an always-on safety net.

Core path: chat → YAML/JSON parse with json-repair fallback → pydantic
validation, honoring the prompt's E9 format (YAML answers cost roughly half
the tokens of JSON). No third-party structured-output library is used.

I69: every call is counted per prompt version (:func:`structured_stats`): clean parses,
repaired parses (strict parse failed, the repair net rescued it), validation failures and
LLM errors. Opt-in (``llm.structured``, both default off): ``retry_on_error`` re-prompts
ONCE with the validation error appended; ``constrained_retry`` additionally asks the
backend for JSON-schema constrained decoding (``response_format`` json_schema; LiteLLM
maps it to Ollama ``format`` and OpenAI-compatible servers) on that retry only, so the
YAML token saving of the happy path is kept.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import BaseModel

from memspine.exceptions import LLMError
from memspine.observability.logging import get_logger
from memspine.prompts.base import Prompt, PromptFormat
from memspine.services.llm.base import LLMService, lenient_json

__all__ = [
    "StructuredOptions",
    "configure",
    "reset_structured_stats",
    "structured_call",
    "structured_stats",
]

_log = get_logger(__name__)


@dataclass(frozen=True)
class StructuredOptions:
    """Process-wide switches (set from ``llm.structured`` at engine start)."""

    retry_on_error: bool = False
    constrained_retry: bool = False


_OPTIONS = StructuredOptions()
_COUNTERS = (
    "calls",
    "clean",
    "repaired",
    "validation_failed",
    "llm_errors",
    "retried",
    "retry_ok",
    "retry_failed",
    "constrained_retries",
)
_STATS: dict[str, dict[str, int]] = {}
#: Set by ``_parse_payload`` when the strict parse failed and a repair path answered.
_repair_seen = [False]


def configure(options: StructuredOptions | Mapping[str, Any] | None) -> None:
    """Install the opt-in switches (``None`` restores the defaults)."""
    global _OPTIONS
    if options is None:
        _OPTIONS = StructuredOptions()
    elif isinstance(options, StructuredOptions):
        _OPTIONS = options
    else:
        _OPTIONS = StructuredOptions(**dict(options))


def _bump(prompt_version: str, key: str) -> None:
    entry = _STATS.setdefault(prompt_version, dict.fromkeys(_COUNTERS, 0))
    entry[key] += 1


def structured_stats() -> dict[str, dict[str, Any]]:
    """Per prompt version counters since start or the last reset, plus ``repair_rate``
    (repaired / calls) and ``failure_rate`` (calls that ended in an LLMError for a bad
    reply / calls). The I69 go/no-go: under about 1% failure, leave retry off."""
    out: dict[str, dict[str, Any]] = {}
    for version, entry in sorted(_STATS.items()):
        calls = max(entry["calls"], 1)
        unretried = max(entry["validation_failed"] - entry["retried"], 0)
        out[version] = {
            **entry,
            "repair_rate": round(entry["repaired"] / calls, 4),
            "failure_rate": round((entry["retry_failed"] + unretried) / calls, 4),
        }
    return out


def reset_structured_stats() -> None:
    _STATS.clear()


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
            _repair_seen[0] = True
            # Models often leave a value unquoted that contains ": " (a quoted
            # title, a time). Strict YAML rejects the whole reply; the line-based
            # reader keeps every item by splitting each field on its FIRST colon.
            salvaged = _lenient_yaml_items(cleaned)
            if salvaged is not None:
                return salvaged
    try:
        return json.loads(cleaned)
    except ValueError:
        _repair_seen[0] = True
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


def _response_format(output_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": output_model.__name__,
            "schema": output_model.model_json_schema(),
            "strict": False,
        },
    }


def _retry_messages(messages: list[dict[str, str]], raw: str, error: str) -> list[dict[str, str]]:
    """The original messages plus the failed reply and the validation error."""
    from memspine.prompts.base import RenderedMessages

    extra = [
        {"role": "assistant", "content": raw[:2000]},
        {
            "role": "user",
            "content": (
                f"Your reply failed validation: {error[:400]}\n"
                "Reply again with ONLY the corrected output, in the same format as before."
            ),
        },
    ]
    return RenderedMessages(
        [*messages, *extra],
        prompt_id=getattr(messages, "prompt_id", None),
        prompt_version=getattr(messages, "prompt_version", None),
    )


async def structured_call[ModelT: BaseModel](
    llm: LLMService,
    prompt: Prompt,
    context: dict[str, object],
    output_model: type[ModelT],
    **chat_options: Any,
) -> ModelT:
    """Render, chat, parse (format-aware), validate. Raises LLMError with the raw
    response attached when validation fails even after repair (and after the one
    opt-in retry, ``llm.structured.retry_on_error``)."""
    version = prompt.prompt_version
    messages = prompt.render(context)
    _bump(version, "calls")
    try:
        raw = await llm.chat(messages, **chat_options)
    except LLMError:
        _bump(version, "llm_errors")
        raise
    first = _try_validate(raw, prompt.format, output_model, version)
    if isinstance(first, BaseModel):
        return first  # type: ignore[return-value]
    options = _OPTIONS
    if not options.retry_on_error:
        raise _validation_error(prompt, output_model, first, raw)
    _bump(version, "retried")
    retry = _retry_messages(messages, raw, str(first))
    raw2: str | None = None
    if options.constrained_retry and "response_format" not in chat_options:
        try:
            raw2 = await llm.chat(
                retry, **chat_options, response_format=_response_format(output_model)
            )
            _bump(version, "constrained_retries")
        except LLMError as exc:  # a backend without schema support: retry unconstrained
            _log.debug("structured.constrained_unsupported", error=str(exc))
    if raw2 is None:
        try:
            raw2 = await llm.chat(retry, **chat_options)
        except LLMError:
            _bump(version, "llm_errors")
            _bump(version, "retry_failed")
            raise
    second = _try_validate(raw2, prompt.format, output_model, version, count=False)
    if isinstance(second, BaseModel):
        _bump(version, "retry_ok")
        return second  # type: ignore[return-value]
    _bump(version, "retry_failed")
    raise _validation_error(prompt, output_model, second, raw2)


def _try_validate(
    raw: str,
    format_: PromptFormat,
    output_model: type[BaseModel],
    version: str,
    *,
    count: bool = True,
) -> BaseModel | Exception:
    """The validated model, or the error (never raises). Counts clean / repaired /
    validation_failed for the first attempt."""
    _repair_seen[0] = False
    try:
        payload = _parse_payload(raw, format_)
        model = output_model.model_validate(payload)
    except Exception as exc:
        if count:
            _bump(version, "validation_failed")
        return exc
    if count:
        _bump(version, "repaired" if _repair_seen[0] else "clean")
    return model


def _validation_error(
    prompt: Prompt, output_model: type[BaseModel], exc: Exception, raw: str
) -> LLMError:
    err = LLMError(
        f"prompt {prompt.prompt_version}: response failed validation against "
        f"{output_model.__name__}: {exc}; raw (truncated): {raw[:300]}"
    )
    err.__cause__ = exc
    return err
