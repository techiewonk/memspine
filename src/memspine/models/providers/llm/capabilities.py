# ruff: noqa: E501
"""Per-provider capability: can this LLM take a JSON schema for constrained decoding? (W2-06b; P-04)

Never a requirement: ``schema_options`` returns ``("none", {})`` for a provider that does not declare or
imply support, and the caller falls back to the validator plus one repair. A provider can say it itself
with a ``supports_json_schema`` attribute (True / False wins over the guess below); otherwise the choice
follows the provider id: LiteLLM models on an OpenAI-compatible or Ollama backend take
``response_format`` json_schema (LiteLLM maps it to Ollama's ``format``), llama.cpp takes
``response_format`` with a ``schema`` (compiled to a grammar).
"""

from __future__ import annotations

from typing import Any

__all__ = ["schema_options"]

#: LiteLLM provider prefixes whose backends accept ``response_format`` json_schema.
_JSON_SCHEMA_PREFIXES = frozenset(
    {"ollama", "ollama_chat", "openai", "hosted_vllm", "vllm", "lm_studio", "azure", "gemini"}
)


def schema_options(llm: object, schema: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    """``(mode, chat options)``; mode is ``json_schema``, ``grammar`` or ``none``."""
    declared = getattr(llm, "supports_json_schema", None)
    if declared is False:
        return "none", {}
    pid = str(getattr(llm, "provider_id", "") or "")
    if declared is not True:
        if pid.startswith("llama_cpp:"):
            return "grammar", {"response_format": {"type": "json_object", "schema": schema}}
        if not pid.startswith("litellm:") or pid[8:].split("/", 1)[0] not in _JSON_SCHEMA_PREFIXES:
            return "none", {}
    elif pid.startswith("llama_cpp:"):
        return "grammar", {"response_format": {"type": "json_object", "schema": schema}}
    return "json_schema", {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema, "strict": False},
        }
    }
