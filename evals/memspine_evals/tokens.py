"""Token accounting and budget truncation.

The budget is part of the protocol, not an implementation detail: two systems
answering the same question from contexts of 4k and 200k tokens have not been
compared. Every context the harness passes to a reader goes through
``truncate_to_budget`` and reports whether the budget bit.

The counter itself is recorded in the manifest. A heuristic counter is honest
and reproducible; a heuristic counter whose identity is lost is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    counter_id: str

    def count(self, text: str) -> int: ...

    def describe(self) -> Mapping[str, Any]: ...


class HeuristicTokenCounter:
    """Deterministic chars-per-token estimate — no model, no network, no drift.

    Good enough to enforce a budget consistently across systems, which is what
    the budget is for. It is *not* good enough to publish as a token cost
    against a provider's billing; runs that report cost must carry a real
    tokenizer, and the manifest says which one ran.
    """

    counter_id = "heuristic-chars4"

    def __init__(self, chars_per_token: float = 4.0) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self.chars_per_token = chars_per_token

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, round(len(text) / self.chars_per_token))

    def describe(self) -> Mapping[str, Any]:
        return {"counter_id": self.counter_id, "chars_per_token": self.chars_per_token}


class TiktokenCounter:
    """Real BPE counts via ``tiktoken``. Optional: hard-fails naming the install."""

    def __init__(self, encoding: str = "cl100k_base") -> None:
        try:
            import tiktoken
        except ImportError as exc:  # pragma: no cover - exercised only with the extra
            raise RuntimeError(
                "TiktokenCounter needs tiktoken — `uv pip install tiktoken`, "
                "or use HeuristicTokenCounter and say so in the manifest"
            ) from exc
        self._enc = tiktoken.get_encoding(encoding)
        self.encoding = encoding
        self.counter_id = f"tiktoken-{encoding}"

    def count(self, text: str) -> int:
        return len(self._enc.encode(text))

    def describe(self) -> Mapping[str, Any]:
        return {"counter_id": self.counter_id, "encoding": self.encoding}


def truncate_to_budget(
    text: str, budget_tokens: int, counter: TokenCounter
) -> tuple[str, int, bool]:
    """Cut ``text`` to fit ``budget_tokens``. Returns ``(text, tokens, truncated)``.

    Truncation is from the tail: the head of an assembled context is where the
    stable, cache-aligned material sits (memspine E2), and dropping the head to
    keep the tail would silently change what is being evaluated.
    """
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    tokens = counter.count(text)
    if tokens <= budget_tokens:
        return text, tokens, False
    # Binary-search the character cut so the result holds for any counter.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter.count(text[:mid]) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    return cut, counter.count(cut), True
