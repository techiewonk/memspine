"""Choose constrained decoding from explicit service capabilities."""

from __future__ import annotations

from typing import Any

__all__ = ["schema_options"]


def schema_options(llm: object, schema: dict[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    """``(mode, chat options)``; mode is ``json_schema``, ``grammar`` or ``none``."""
    if getattr(llm, "supports_grammar", False):
        return "grammar", {"response_format": {"type": "json_object", "schema": schema}}
    declared = getattr(llm, "supports_json_schema", None)
    grammar = getattr(llm, "supports_grammar", False)
    if declared is not True and grammar is True:
        return "grammar", {"response_format": {"type": "json_object", "schema": schema}}
    if declared is False:
        return "none", {}
    pid = str(getattr(llm, "provider_id", "") or "")
    if declared is not True:
        if pid.startswith("llama_cpp:"):
            return "grammar", {"response_format": {"type": "json_object", "schema": schema}}
        return "none", {}
    elif pid.startswith("llama_cpp:"):
        return "grammar", {"response_format": {"type": "json_object", "schema": schema}}
    return "json_schema", {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema, "strict": False},
        }
    }
