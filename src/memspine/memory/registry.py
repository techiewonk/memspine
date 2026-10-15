"""Typed registry of memory types (MOD-02). The nine built-ins map to the module of the mechanism
that implements them; an enabled plug-in adds a type under its own name.

``memspine.core.registry`` still owns the dependency table for the built-ins; a plug-in type has
no built-in dependencies.
"""

from __future__ import annotations

from memspine.plugins import Registry

__all__ = ["MEMORY_TYPES"]

MEMORY_TYPES: Registry[str] = Registry("memory_types")

for _name in (
    "working",
    "episodic",
    "semantic",
    "procedural",
    "associative",
    "reflective",
    "prospective",
    "shared",
    "resource",
):
    MEMORY_TYPES.register(_name, f"memspine.memories.{_name}")
