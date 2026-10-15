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
        default_factory=lambda: ["read", "write", "job"]  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    )


class OtelConfig(BaseModel):
    """T5-06 (G-20): optional OpenTelemetry export of request -> stage spans.

    Off by default. On, ``Engine.start()`` raises ``MissingServiceError`` naming ``memspine[otel]``
    unless ``opentelemetry`` is importable, switches the wiretap on (sink ``none`` when the tap is
    not configured; its own sink is kept otherwise, content stays off) and adds the exporter as a
    second sink. Spans carry ids, hashes, counts, codes and durations only: a string attribute that
    looks like text is dropped and counted (``ops_report()["otel"]["rejected_attributes"]``).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class ObservabilityConfig(BaseModel):
    """I73 diagnostics, opt-in.

    - ``write_timers``: time each step of the write door (validation, firewall, redaction,
      embedding, projection, dedup, conflict ladder, perspective / sensitivity tagging and
      the inline LLM steps) with the monotonic clock; per step count, total, p50 and p95
      through ``Engine.write_timers()`` and ``describe()``. Off: nothing is wrapped.
    - ``ops_ledger`` (T5-06, G-20): time every read verb, write verb, window-extract job and sleep
      stage (runs, errors, items, dropped, deferred, p50 / p95) in ``memspine.ops.ledger``;
      ``Engine.ops_report()`` merges it with the SLO and write-cost ledgers. Off: nothing is timed.
    - ``otel``: see :class:`OtelConfig`.
    - ``inspect`` (TI-1, Trace Inspector): extra wiretap events that follow one turn or question
      through every stage (per-record projector writes, window membership and extraction I/O,
      envelope lifecycle, ladder decisions, window edges, sleep per-item outputs, headers, rerank
      components, planner I/O, retrieve write-back) plus the ``inspect_scope`` correlation keys the
      harness sets. Needs the wiretap: ``Engine.start`` switches it on (jsonl sink) when
      ``observability.tap.enabled`` is off. Off: no event is built and behaviour is unchanged.
    - ``inspect_content`` (TI-1): also keep text payloads (rendered prompts, raw replies, index
      text, planner I/O) in the inspect events, truncated at 20000 characters and redacted. Off:
      ids, hashes, counts and decisions only.
    """

    model_config = ConfigDict(extra="forbid")

    write_timers: bool = False
    ops_ledger: bool = False
    inspect: bool = False
    inspect_content: bool = False
    otel: OtelConfig = Field(default_factory=OtelConfig)
    tap: TapConfig = Field(default_factory=TapConfig)
