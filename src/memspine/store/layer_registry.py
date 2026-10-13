# ruff: noqa: E501
"""LayerRegistry (F-02b; M-02): which layers the template enables, and which are populated per namespace.

Two separate facts, kept apart because the router (R-Q1) needs both:

* **enabled**: what the template switches on. L0 (raw) is always on. L1 (facts) and L3 (graph) are on
  when a worker model is configured (I-7) or, for L3, a graph store is. L2 (episodes, summaries), L4
  (reflections) and L5 (standing answers, procedures) are opt-in: ``write.window_extract: all`` for L2, the
  ``reflective`` memory type for L4, ``procedural`` / ``prospective`` for L5.
* **populated**: how many live records each layer holds in a namespace, counted by the ``layer`` column
  (F-02a). A layer that holds records is treated as enabled whatever the template says: the records exist.

The registry is a snapshot value: build it with :meth:`LayerRegistry.load` (reads one grouped count) or
:meth:`LayerRegistry.of` (from counts you already have). It imports no engine code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from memspine.store.layers import LAYERS

__all__ = ["LayerRegistry", "LayerTemplate"]

#: Statuses that count as live in a layer count (``RecordStatus`` values).
_LIVE_STATUSES = ("activated", "resolving")


@dataclass(frozen=True)
class LayerTemplate:
    """The layers a template enables. ``L0`` is always in the set."""

    enabled: frozenset[str] = frozenset({"L0"})

    @classmethod
    def from_config(
        cls, config: Any, *, worker_model: bool = False, graph_store: bool = False
    ) -> LayerTemplate:
        """Derive the enabled set from a :class:`MemspineConfig` plus what the engine holds live."""
        on = {"L0"}
        if worker_model:
            on.update({"L1", "L3"})
        if graph_store:
            on.add("L3")
        if getattr(getattr(config, "write", None), "window_extract", "off") == "all":
            on.add("L2")
        memories = getattr(config, "memories", {}) or {}

        def _enabled(name: str) -> bool:
            entry = memories.get(name)
            return bool(entry is not None and entry.enabled)

        if _enabled("reflective"):
            on.add("L4")
        if _enabled("procedural") or _enabled("prospective"):
            on.add("L5")
        return cls(enabled=frozenset(on))


@dataclass(frozen=True)
class LayerRegistry:
    """Per-namespace snapshot: template-enabled layers and live record counts by layer."""

    namespace: str
    template: LayerTemplate = field(default_factory=LayerTemplate)
    counts: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def of(
        cls, namespace: str, template: LayerTemplate, counts: Mapping[str, int] | None = None
    ) -> LayerRegistry:
        known = {k: int(v) for k, v in (counts or {}).items() if k in LAYERS and int(v) > 0}
        return cls(namespace=namespace, template=template, counts=known)

    @classmethod
    async def load(cls, storage: Any, namespace: str, template: LayerTemplate) -> LayerRegistry:
        """Count the namespace's live records by layer (one grouped query) and bind the template."""
        return cls.of(namespace, template, await storage.layer_counts(namespace))

    def count(self, layer: str) -> int:
        return max(0, int(self.counts.get(layer, 0)))

    def is_populated(self, layer: str) -> bool:
        return self.count(layer) > 0

    def is_enabled(self, layer: str) -> bool:
        """On by the template, or evidently on because the layer holds records."""
        return layer == "L0" or layer in self.template.enabled or self.is_populated(layer)

    @property
    def enabled(self) -> frozenset[str]:
        return frozenset(layer for layer in LAYERS if self.is_enabled(layer))

    @property
    def populated(self) -> frozenset[str]:
        return frozenset(layer for layer in LAYERS if self.is_populated(layer))

    def report(self) -> dict[str, dict[str, Any]]:
        """``{layer: {"enabled": bool, "count": int}}`` for every layer, L0..L5."""
        return {
            layer: {"enabled": self.is_enabled(layer), "count": self.count(layer)}
            for layer in LAYERS
        }
