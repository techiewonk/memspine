"""Config area: economy (W2-12; W-26, P-14, P-22): the write-side cost ledger and its budget."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from memspine.config.schema.base import ConfigModel


class BudgetConfig(ConfigModel):
    """Spend limit per namespace. When it is spent, derivation pauses and the work is marked
    underived in the outbox (never dropped); ``Engine.resume_deferred`` puts it back."""

    model_config = ConfigDict(extra="forbid")

    #: Generative model calls allowed per namespace per period (None = no call limit).
    max_calls: int | None = Field(default=None, ge=1)
    #: Generative tokens (in + out) allowed per namespace per period (None = no token limit).
    max_tokens: int | None = Field(default=None, ge=1)
    #: Length of one period in seconds; spend resets when it rolls. None = one period for the run.
    period_s: float | None = Field(default=None, gt=0)


class EconomyConfig(ConfigModel):
    """Cost accounting. Everything is OFF by default: nothing is recorded and nothing changes.

    - ``ledger_v2`` (W2-12; W-26, P-22 C, LIB-27): count model calls, tokens in/out, latency and
      embed calls per write, per layer, per namespace and per stage (window extract, sleep
      stages). Read it with ``Engine.cost_report(namespace)``. Tap: ``economy.cost``.
    - ``ledger_persist`` (FU-SLEEP; needs ``ledger_v2``): also keep every ledger entry as a compact
      row (ids, hashed namespace, stage, calls, tokens, latency; no content) in a sibling SQLite
      file
      of the storage database (``<path>.cost``), and reload the totals at start, so ``cost_report``
      survives a restart. In-memory and non-sqlite storage: not persisted (a warning is logged).
      Reloaded rows carry the namespace hash; ``cost_report(namespace)`` matches it by hash.
    - ``budget`` (W-26 D, P-22 C; needs ``ledger_v2`` and ``workers.outbox``): optional spend
      limit that pauses derivation. Tap: ``economy.budget``. ``None`` = no limit.
    """

    model_config = ConfigDict(extra="forbid")

    ledger_v2: bool = False
    ledger_persist: bool = False
    budget: BudgetConfig | None = None
