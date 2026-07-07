"""Internal registry (D-15): memory types, their dependency graph, auto-enable.

Owns the §3 dependency table. ``config validate`` walks it; C1(b) auto-enables
missing dependencies with a logged INFO notice (D-13).
"""

from __future__ import annotations

import structlog

from memspine.exceptions import ConfigError

__all__ = ["MEMORY_TYPES", "dependency_closure", "validate_types"]

_log = structlog.get_logger(__name__)

#: The nine v0.1 memory types (structure plan §1).
MEMORY_TYPES: frozenset[str] = frozenset(
    {
        "working",
        "episodic",
        "semantic",
        "procedural",
        "associative",
        "reflective",
        "prospective",
        "shared",
        "resource",
    }
)

#: Hard dependencies (§3): enabling the key requires every type in the value.
_REQUIRES: dict[str, frozenset[str]] = {
    "reflective": frozenset({"episodic"}),
    "prospective": frozenset({"semantic"}),
    "associative": frozenset({"semantic"}),
}

#: any-of dependencies (§3): the key needs at least one of the value; when none
#: is enabled we auto-enable the first (deterministic default).
_REQUIRES_ANY: dict[str, tuple[str, ...]] = {
    "shared": ("semantic", "episodic"),
}


def validate_types(names: set[str]) -> None:
    unknown = names - MEMORY_TYPES
    if unknown:
        raise ConfigError(
            f"unknown memory type(s): {sorted(unknown)} — valid: {sorted(MEMORY_TYPES)}"
        )


def dependency_closure(enabled: set[str]) -> tuple[set[str], list[str]]:
    """C1(b): expand ``enabled`` with every hard/any-of dependency (D-13).

    Returns ``(closure, auto_enabled)``; every auto-enable is logged at INFO so
    the effective world is never a surprise.
    """
    validate_types(enabled)
    closure = set(enabled)
    auto: list[str] = []
    changed = True
    while changed:
        changed = False
        for mem_type in sorted(closure):
            for dep in sorted(_REQUIRES.get(mem_type, frozenset())):
                if dep not in closure:
                    closure.add(dep)
                    auto.append(dep)
                    changed = True
                    _log.info(
                        "memory.dependency_auto_enabled",
                        memory_type=dep,
                        required_by=mem_type,
                    )
            any_of = _REQUIRES_ANY.get(mem_type)
            if any_of and not any(option in closure for option in any_of):
                default = any_of[0]
                closure.add(default)
                auto.append(default)
                changed = True
                _log.info(
                    "memory.dependency_auto_enabled",
                    memory_type=default,
                    required_by=mem_type,
                    reason=f"needs at least one of {list(any_of)}",
                )
    return closure, auto
