"""Config area: observability (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ObservabilityConfig(BaseModel):
    """I73 diagnostics, opt-in.

    - ``write_timers``: time each step of the write door (validation, firewall, redaction,
      embedding, projection, dedup, conflict ladder, perspective / sensitivity tagging and
      the inline LLM steps) with the monotonic clock; per step count, total, p50 and p95
      through ``Engine.write_timers()`` and ``describe()``. Off: nothing is wrapped.
    """

    model_config = ConfigDict(extra="forbid")

    write_timers: bool = False
