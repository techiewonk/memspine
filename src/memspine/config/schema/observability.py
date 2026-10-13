"""Config area: observability (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TapConfig(BaseModel):
    """WT-01 wiretap: a per-request trace of every pipeline stage, for debugging only.

    Off by default; when off no event is built and no file is touched. Events carry ids, scores,
    counts, decisions and drop reasons; text is kept only when ``content`` is on (truncated to
    ``max_content_chars`` and redacted). See docs/v2/WIRETAP.md.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    sink: Literal["none", "jsonl", "sqlite", "memory", "otel", "log"] = "jsonl"
    dir: str = "runs/_tap"
    content: bool = False
    max_content_chars: int = Field(default=200, ge=1, le=100_000)
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    kinds: list[Literal["read", "write", "job"]] = Field(
        default_factory=lambda: ["read", "write", "job"]
    )


class ObservabilityConfig(BaseModel):
    """I73 diagnostics, opt-in.

    - ``write_timers``: time each step of the write door (validation, firewall, redaction,
      embedding, projection, dedup, conflict ladder, perspective / sensitivity tagging and
      the inline LLM steps) with the monotonic clock; per step count, total, p50 and p95
      through ``Engine.write_timers()`` and ``describe()``. Off: nothing is wrapped.
    """

    model_config = ConfigDict(extra="forbid")

    write_timers: bool = False
    tap: TapConfig = Field(default_factory=TapConfig)
