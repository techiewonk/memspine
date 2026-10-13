"""Ordered post-compile stages over a window's assertion envelopes (W2-06c stage hook list).

``process_items`` (W2-06a, ``write.envelope_v2``) validates, repairs and compiles the model's items
into :class:`AssertionEnvelope`s. Before they are deposited, the window pipeline runs them through
this ordered list of stages. Each Phase 2 write package plugs in here with its own module and one
builder line in ``ingest/windows.py:_envelope_stages``, so the packages stay independent:

    order  package  stage
    100    W2-14    temporal anchoring (event time, basis, precision, relations)
    200    W2-08    grounding (spans per assertion, rule checks, assent)
    300    W2-09    online entity resolution (pending_merge)
    400    W2-10    integration ladder + cardinality registry (append/supersede/merge/coexist/contest)
    500    W2-13    pre-pass hints (GLiNER2) — consumes hints, never adds facts on its own

A stage returns the envelopes to keep (it may annotate, rewrite or drop them; a drop must be counted
in ``ctx.notes`` with a reason code). With no stage configured the list is empty and the window path
is unchanged. Stages run outside the namespace lock and must not make more than their own documented
model calls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from memspine.observability import tap
from memspine.schemas.durable.assertion import AssertionEnvelope

__all__ = ["EnvelopeStage", "EnvelopeStageContext", "run_envelope_stages"]


@dataclass
class EnvelopeStageContext:
    """What a stage may read about the window it runs on (ids and handles, never a dataset)."""

    namespace: str
    window_id: str
    #: line number -> the cited source line (as built for the compile context)
    lines: Mapping[int, Any]
    #: the window's raw turn records, in order
    records: Sequence[Any]
    #: the W2-06a compile context (scope, session, source time)
    compile_ctx: Any
    #: engine-side services a stage was built with (storage, resolver, ...), keyed by name
    services: Mapping[str, Any] = field(default_factory=dict)
    #: drop / rewrite counters, reported in the window stats under ``envelope_stages``
    notes: Counter[str] = field(default_factory=Counter)


StageFn = Callable[[list[AssertionEnvelope], EnvelopeStageContext], Awaitable[list[AssertionEnvelope]]]


@dataclass(frozen=True)
class EnvelopeStage:
    name: str
    order: int
    run: StageFn


async def run_envelope_stages(
    stages: Sequence[EnvelopeStage],
    envelopes: list[AssertionEnvelope],
    ctx: EnvelopeStageContext,
) -> list[AssertionEnvelope]:
    """Run ``stages`` in ``order``; a no-op (same list) when there are none."""
    if not stages:
        return envelopes
    out = envelopes
    for stage in sorted(stages, key=lambda s: s.order):
        before = len(out)
        out = await stage.run(out, ctx)
        if tap.enabled():
            tap.event(
                "write.envelope.stage",
                stage=stage.name,
                before=before,
                after=len(out),
                window=ctx.window_id,
            )
    return out
