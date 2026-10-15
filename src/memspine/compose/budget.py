"""Exact token budgeting (R-18 step 1, ER-24).

Pure and deterministic: no I/O, no model calls, no tokenizer import. A caller that has the reader's
tokenizer (for example a Hugging Face ``tokenizer.encode``) wraps it in :class:`CallableCounter` and gets
``accounting == "exact"``; without one, :class:`EstimatingCounter` (chars / 4) gives ``"estimated"``,
which is a labelled fallback and never a hard guarantee.

The budget counts the *rendered* text, so headers, separators, citation markers and section titles are
paid for like any other token. Configured reserves (system prompt, output, history) come off the top.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "Accounting",
    "Budget",
    "BudgetExceededError",
    "CallableCounter",
    "EstimatingCounter",
    "TokenCounter",
]

Accounting = Literal["exact", "estimated"]


@runtime_checkable
class TokenCounter(Protocol):
    """Counts tokens of a rendered string. ``accounting`` says whether the count is the reader's own."""

    name: str
    accounting: Accounting

    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class EstimatingCounter:
    """chars / 4, rounded up. Estimated accounting: no hard token guarantee (ER-24)."""

    name: str = "estimate:chars/4"
    accounting: Accounting = "estimated"

    def count(self, text: str) -> int:
        return -(-len(text) // 4)


@dataclass(frozen=True)
class CallableCounter:
    """Adapter for any callable tokenizer; exact accounting.

    ``fn`` may return a token sequence (its length is used) or an integer count. The caller owns
    special-token policy: pass ``lambda t: tok.encode(t, add_special_tokens=False)`` to count raw text.
    """

    fn: Callable[[str], Sized | int]
    name: str
    accounting: Accounting = "exact"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("an exact counter names its tokenizer")

    def count(self, text: str) -> int:
        out = self.fn(text)
        n = out if isinstance(out, int) else len(out)
        if n < 0:
            raise ValueError("a tokenizer returned a negative count")
        return n


class BudgetExceededError(ValueError):
    """``consume`` would take the budget over its available tokens."""


@dataclass
class Budget:
    """Total tokens, reserves, and per-block shares.

    ``available = total - system - output - history`` (floored at 0) is what rendered context may use.
    ``shares`` maps a block kind to a fraction of ``available``; fractions sum to at most 1. A kind with
    no share has no guaranteed capacity: it can only use capacity other blocks left unused.
    """

    total: int
    counter: TokenCounter = field(default_factory=EstimatingCounter)
    system: int = 0
    output: int = 0
    history: int = 0
    shares: Mapping[str, float] = field(default_factory=dict)
    used: int = 0

    def __post_init__(self) -> None:
        if self.total <= 0:
            raise ValueError("total must be positive")
        if min(self.system, self.output, self.history, self.used) < 0:
            raise ValueError("reserves and use are non-negative")
        if any(not 0.0 <= s <= 1.0 for s in self.shares.values()):
            raise ValueError("a share is a fraction in [0, 1]")
        if sum(self.shares.values()) > 1.0 + 1e-9:
            raise ValueError("block shares sum to more than 1")

    @property
    def available(self) -> int:
        return max(0, self.total - self.system - self.output - self.history)

    @property
    def remaining(self) -> int:
        return max(0, self.available - self.used)

    def block_cap(self, kind: str) -> int:
        """Guaranteed tokens for ``kind`` (floor of share * available); 0 when it has no share."""
        return int(self.shares.get(kind, 0.0) * self.available + 1e-9)

    def _cost(self, text_or_tokens: str | int) -> int:
        if isinstance(text_or_tokens, int):
            if text_or_tokens < 0:
                raise ValueError("token counts are non-negative")
            return text_or_tokens
        return self.counter.count(text_or_tokens)

    def fits(self, text_or_tokens: str | int) -> bool:
        return self._cost(text_or_tokens) <= self.remaining

    def consume(self, text_or_tokens: str | int) -> int:
        """Spend tokens (counting a string if given); raises if they do not fit. Returns the cost."""
        n = self._cost(text_or_tokens)
        if n > self.remaining:
            raise BudgetExceededError(f"{n} tokens exceed the {self.remaining} remaining")
        self.used += n
        return n
