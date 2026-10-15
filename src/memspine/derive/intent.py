"""Derivation intent (W2-01; W-02): what a receipt promises to derive, and a status call per layer.

A write receipt carries an ``intent_id``. The intent is the durable promise that the layers above the
raw turn (facts, graph, summaries, profile) will be derived, or are not configured. ``status`` answers
per layer: ``pending`` / ``done`` / ``failed`` / ``skipped`` (not configured, never waited for).

Pure: no I/O. The intake gateway (``ingest.gateway``) builds it from the receipt readiness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from memspine.schemas.public.receipt import Readiness
from memspine.schemas.steps.shared import IntentStatus

__all__ = ["LAYER_OF_CAPABILITY", "DerivationIntent"]

#: capability (receipt readiness) -> the ladder layers it covers. ``raw_search`` is the raw turn (L0);
#: ``facts`` the mined facts (L1); ``graph`` the entity edges (L2); ``summaries`` the episode
#: fragments and summaries (L3); ``profile`` the profile strata (L4, L5).
LAYER_OF_CAPABILITY: Mapping[str, tuple[str, ...]] = {
    "raw_search": ("L0",),
    "facts": ("L1",),
    "graph": ("L2",),
    "summaries": ("L3",),
    "profile": ("L4", "L5"),
}

_STATE = {"ready": "done", "pending": "pending", "failed": "failed", "unavailable": "skipped"}


@dataclass(frozen=True)
class DerivationIntent:
    """One accepted write's derivation promise."""

    intent_id: str
    source_id: str
    source_version: int
    readiness: Readiness
    applied: bool = True  # False while the raw records are still being written (resumable)

    def status(self) -> IntentStatus:
        layers: dict[str, str] = {}
        for cap, names in LAYER_OF_CAPABILITY.items():
            state = _STATE[self.readiness.get(cap)]
            if cap == "raw_search" and not self.applied:
                state = "pending"
            for layer in names:
                layers[layer] = state
        failed = [c for c in LAYER_OF_CAPABILITY if self.readiness.get(c) == "failed"]
        return IntentStatus(
            intent_id=self.intent_id,
            layers=layers,  # type: ignore[arg-type]
            failure_class="derivation_failed" if failed else None,
        )

    def pending_layers(self) -> list[str]:
        return [k for k, v in self.status().layers.items() if v == "pending"]
