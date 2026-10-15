"""Which memory types, layers, legs and producers an eval arm effectively has on (MF-16).

A result row is only interpretable if it says what the arm actually ran. A flag set in an arm file
can still be inert (a producer that needs another switch, a leg whose layer is never populated), so
this helper resolves the arm through the real config loader and the real layer registry, then
reports four things:

* ``memory_types``: the enabled types (``config.enabled_memories()``) and the layer each writes to;
* ``layers``: the layers the template enables (``LayerTemplate.from_config``) and the effective set
  (template plus the layers of active producers), L0..L5;
* ``legs``: the retrieval legs the arm switches on, with the layer each one reads;
* ``producers``: the derived-record producers the arm switches on, with the layer each should write
  to, and any unmet prerequisite from ``memspine.config.requires``.

It reads configuration only. It never starts an engine, never calls a model, and a layer being
*enabled* here does not mean it is *populated*: record the post-run ``layer_counts`` beside it.

Usage::

    python -m memspine_evals.memory_type_matrix evals/arms/v2-PICK3-j.json [--template core] [--json]

``effective_flags(arm_dict)`` returns the same data as a dict so a runner can embed it in each
result row.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from memspine.config.loader import load_config
from memspine.config.requires import REQUIRES, missing_needs, resolve_path
from memspine.store.layer_registry import LayerTemplate
from memspine.store.layers import LAYERS, derive_layer

__all__ = ["LEGS", "MEMORY_TYPES", "PRODUCERS", "effective_flags", "main", "render"]

MEMORY_TYPES: tuple[str, ...] = (
    "working",
    "episodic",
    "semantic",
    "associative",
    "reflective",
    "procedural",
    "prospective",
    "shared",
    "resource",
)

#: leg name -> (config path that switches it on, layer it reads or "any")
LEGS: dict[str, tuple[str, str]] = {
    "vector": ("", "any"),  # always on
    "lexical": ("", "any"),  # always on
    "temporal": ("read.temporal_leg", "any"),
    "entity": ("read.entity_leg_v2", "L1"),
    "graph": ("read.graph_leg_v2", "L3"),
    "episode": ("read.episode_leg_v2", "L2"),
    "recency": ("read.recency_leg", "any"),
    "session": ("read.session_leg", "any"),
    "perspective": ("read.perspective_leg", "any"),
}

#: producer -> (config path that switches it on, layer it should write to, memory type, extra needs)
PRODUCERS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "window_facts": ("write.window_extract", "L1", "semantic", ()),
    "window_episodes": ("write.window_extract", "L2", "semantic", ()),
    "session_close": ("offline.session_close_v2", "L2", "semantic", ()),
    "consolidation": ("memories.episodic.policies.consolidation", "L2", "semantic", ()),
    "extract_graph": ("memories.semantic.policies.extract_graph", "L3", "associative", ()),
    "entity_summary": ("memories.associative.policies.entity_summaries", "L3", "associative", ()),
    "communities": ("offline.communities_v2", "L3", "associative", ()),
    "observation": ("offline.patterns_v2", "L4", "reflective", ()),
    "standing": ("offline.standing_v2", "L5", "procedural", ()),
    "profile_card": ("offline.profile_card_v2", "L5", "procedural", ()),
    "reconcile": ("offline.reconcile_v2", "L1", "semantic", ()),
    "histories": ("offline.histories_v2", "L1", "semantic", ()),
}

#: producer -> the value of its switch that means "off" even though the key is present
_OFF_VALUES = (None, False, "off", "none", "", 0, {}, [])


def _on(config: Any, path: str) -> bool:
    if not path:
        return True
    value = resolve_path(config, path)
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, str):
        return value.lower() not in ("off", "none", "")
    return value not in _OFF_VALUES


def _arm_overrides(arm: dict[str, Any]) -> dict[str, Any]:
    """An arm file is the nested override tree; some wrap it as ``{"system": {"overrides": ...}}``."""
    system = arm.get("system")
    if isinstance(system, dict) and isinstance(system.get("overrides"), dict):
        return system["overrides"]
    return arm


def effective_flags(arm: dict[str, Any], *, template: str = "core") -> dict[str, Any]:
    """The effective memory-type / layer / leg / producer set of one arm, as plain data."""
    resolved = load_config(template=template, overrides=_arm_overrides(arm))
    cfg = getattr(resolved, "config", resolved)
    roles = resolve_path(cfg, "llm.roles") or {}
    worker = bool(roles)
    graph_store = _on(cfg, "graph.edge_compiler_v2") or _on(cfg, "memories.associative.enabled")
    enabled_layers = LayerTemplate.from_config(cfg, worker_model=worker, graph_store=graph_store)

    enabled_types = sorted(cfg.enabled_memories())
    types = {
        name: {
            "enabled": name in enabled_types,
            "layer": derive_layer(name),
        }
        for name in MEMORY_TYPES
    }
    producers: dict[str, Any] = {}
    for name, (path, layer, mtype, _needs) in PRODUCERS.items():
        on = _on(cfg, path)
        if name == "window_facts":
            on = str(resolve_path(cfg, path)) in ("facts", "facts_graph", "all")
        elif name == "window_episodes":
            on = str(resolve_path(cfg, path)) == "all"
        unmet = [n.describe() for n in missing_needs(cfg, path)] if path in REQUIRES else []
        producers[name] = {
            "on": on,
            "layer": layer,
            "memory_type": mtype,
            "unmet_prerequisites": unmet if on else [],
            "inert": bool(on and unmet),
        }
    produced = {row["layer"] for row in producers.values() if row["on"] and not row["inert"]}
    effective = set(enabled_layers.enabled) | produced
    legs = {
        name: {
            "on": _on(cfg, path),
            "reads": reads,
            "layer_enabled": reads == "any" or reads in effective,
        }
        for name, (path, reads) in LEGS.items()
    }
    return {
        "template": template,
        "lineage_table": bool(resolve_path(cfg, "write.lineage_table")),
        "worker_model_bound": worker,
        "memory_types": types,
        "layers_enabled": [layer for layer in LAYERS if layer in enabled_layers.enabled],
        "layers_effective": [layer for layer in LAYERS if layer in effective],
        "legs": legs,
        "producers": producers,
    }


def render(flags: dict[str, Any]) -> str:
    lines = [
        f"template={flags['template']} lineage_table={flags['lineage_table']} "
        f"worker_model={flags['worker_model_bound']}",
        "layers enabled (template): " + ", ".join(flags["layers_enabled"]),
        "layers effective (template + active producers): " + ", ".join(flags["layers_effective"]),
        "",
        "memory types:",
    ]
    for name, row in flags["memory_types"].items():
        lines.append(f"  {name:<12} {'ON ' if row['enabled'] else 'off'}  writes {row['layer']}")
    lines += ["", "legs:"]
    for name, row in flags["legs"].items():
        note = "" if row["layer_enabled"] or not row["on"] else "  (layer not enabled: inert)"
        lines.append(f"  {name:<12} {'ON ' if row['on'] else 'off'}  reads {row['reads']}{note}")
    lines += ["", "producers:"]
    for name, row in flags["producers"].items():
        note = f"  UNMET: {'; '.join(row['unmet_prerequisites'])}" if row["inert"] else ""
        lines.append(
            f"  {name:<14} {'ON ' if row['on'] else 'off'}  {row['memory_type']} -> {row['layer']}{note}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("arm", type=Path, help="arm config JSON (nested override tree)")
    parser.add_argument("--template", default="core")
    parser.add_argument("--json", action="store_true", help="print the data, not the table")
    args = parser.parse_args(argv)
    flags = effective_flags(
        json.loads(args.arm.read_text(encoding="utf-8")), template=args.template
    )
    print(json.dumps(flags, indent=2) if args.json else render(flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
