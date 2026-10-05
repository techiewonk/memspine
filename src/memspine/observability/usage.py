"""Per-prompt token ledger (#33): what each named prompt cost.

Every internal LLM call renders a named, versioned prompt (D-43). The router
records each call's input and output tokens here under the prompt's
``prompt_version`` (``<id>@<version>``), so a caller can attribute cost per
loop stage (the CPC metric) instead of only per role.

Token counts come from the provider's own usage report when it gives one
(LiteLLM's ``usage``); otherwise they are estimated at
:data:`~memspine.config.constants.TOKEN_ESTIMATE_CHARS_PER_TOKEN` characters per
token, and the call is counted as estimated. The ledger is in-process only: it
is not persisted and starts empty with every router.
"""

from __future__ import annotations

from typing import Any

__all__ = ["UNNAMED_PROMPT", "UsageLedger"]

#: Ledger key for a call whose messages were not rendered from a named prompt
#: (a caller's own messages sent through ``Engine.llm(role)``).
UNNAMED_PROMPT = "<unnamed>"


class UsageLedger:
    """In-process counters of calls and tokens per prompt version."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def record(
        self,
        *,
        prompt_id: str | None,
        prompt_version: str | None,
        role: str,
        input_tokens: int,
        output_tokens: int,
        estimated: bool,
    ) -> None:
        """Add one call to the entry of ``prompt_version`` (or :data:`UNNAMED_PROMPT`)."""
        key = prompt_version or UNNAMED_PROMPT
        entry = self._entries.setdefault(
            key,
            {
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "roles": [],
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_calls": 0,
            },
        )
        if role not in entry["roles"]:
            entry["roles"] = sorted([*entry["roles"], role])
        entry["calls"] += 1
        entry["input_tokens"] += max(0, int(input_tokens))
        entry["output_tokens"] += max(0, int(output_tokens))
        if estimated:
            entry["estimated_calls"] += 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """A copy of every entry, keyed by prompt version, each with an ``estimated``
        flag that is true when any of its calls was estimated."""
        return {
            key: {**entry, "roles": list(entry["roles"]), "estimated": entry["estimated_calls"] > 0}
            for key, entry in sorted(self._entries.items())
        }

    def reset(self) -> None:
        self._entries.clear()
