"""Flag prerequisites as checks (FU-REQS; audit 2026-10-13, finding 1).

A v2 flag that "needs X" used to say so in a comment only; without X the stage was never
built and the flag was silently inert (a screen arm that forgot X read as "no effect").
:data:`REQUIRES` declares each prerequisite as data: dotted YAML key of the flag -> the
:class:`Need` rows that must hold when the flag is on. One generic validator on the root
config (``MemspineConfig._requirements_hold``) calls :func:`check_requires`, which raises
:class:`~memspine.exceptions.ConfigError` naming the missing key. The flag registry
(:class:`memspine.config.flags.FlagSpec`) exposes the same table as ``requires``.

Defaults stay valid: every flag listed here is off (or ``"off"``) by default, so the check
only changes configs that were silently inert before.

This module imports no schema, so ``root.py`` can use it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memspine.exceptions import ConfigError

__all__ = ["REQUIRES", "Need", "check_requires", "is_active", "missing_needs", "resolve_path"]

_OFF = ("off", "none", "")


@dataclass(frozen=True)
class Need:
    """One prerequisite: the config value at ``path`` must be active (a true flag, a mode other
    than ``off``, a non-None object), or, with ``values``, one of those values.

    ``alt`` lists other paths that satisfy the same need (any one is enough). ``soft`` needs are
    reported by :func:`missing_needs` but not raised by the config validator (a rules-only
    fallback exists); the eval arm validator treats them as hard."""

    path: str
    values: tuple[Any, ...] | None = None
    alt: tuple[str, ...] = ()
    soft: bool = False
    why: str = ""

    def describe(self) -> str:
        paths = " or ".join((self.path, *self.alt))
        if self.values is not None:
            shown = " or ".join(repr(v) if isinstance(v, str) else str(v) for v in self.values)
            return f"{paths}: {shown}"
        return paths


_MISSING = object()


def resolve_path(config: Any, path: str) -> Any:
    """Value at a dotted path of a config object (attributes, or dict keys such as
    ``llm.roles.sufficiency``); ``None`` when any step is absent."""
    node: Any = config
    for part in path.split("."):
        if node is None:
            return None
        node = node.get(part, _MISSING) if isinstance(node, dict) else getattr(node, part, _MISSING)
        if node is _MISSING:
            return None
    return node


def is_active(value: Any) -> bool:
    """A flag is on / a mode is something other than ``off``."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.lower() not in _OFF
    if isinstance(value, (int, float)):
        return value != 0  # FU-RRPOOL: a count key (0 = off) is active when above zero
    return True


def _holds(config: Any, need: Need) -> bool:
    for path in (need.path, *need.alt):
        value = resolve_path(config, path)
        if need.values is None:
            if is_active(value):
                return True
        elif value in need.values:
            return True
    return False


_FACTS = ("facts", "facts_graph", "all")
_ENVELOPE = Need(
    "write.envelope_v2", why="the envelope stages are built only inside the envelope write"
)
_WINDOW = Need("write.window_extract", why="a window mode other than off")
_LEGS = Need("read.legs_v2", why="the leg registry the v2 legs register into")

#: dotted flag key -> prerequisites that must hold while the flag is on.
REQUIRES: dict[str, tuple[Need, ...]] = {
    # --- write: the envelope stack (audit finding 1) ---
    "write.segmentation_v2": (_WINDOW,),
    "write.envelope_v2": (
        Need("write.window_extract", _FACTS, why="a window mode with the facts section"),
    ),
    "write.prompt_v2": (_ENVELOPE,),
    "write.temporal_v2": (_ENVELOPE,),
    "write.integration_v2": (_ENVELOPE,),
    "write.grounding_v2": (_ENVELOPE,),
    "write.coverage_v2": (_ENVELOPE,),
    "write.resolver_v2": (_ENVELOPE,),
    "write.prepass": (_WINDOW,),
    "write.asset_describer": (
        Need("write.intake_v2", why="assets enter through the intake gateway"),
    ),
    # --- read: legs and blocks that read what another switch builds ---
    "read.graph_leg_v2": (
        _LEGS,
        Need("graph.edge_compiler_v2", why="the leg walks compiled edges"),
    ),
    "read.episode_leg_v2": (_LEGS,),
    "read.temporal_precision_aware": (_LEGS,),
    "read.standing_v2": (Need("offline.standing_v2", why="the standing answers it leads with"),),
    "read.external_v2": (
        Need(
            "read.external_evidence",
            ("cache", "web"),
            why="the E03 opt-in; this flag never enables lookups",
        ),
    ),
    "read.rerank_v2_search_pool": (
        Need("read.rerank_v2", why="the v2 reranker the wide pool feeds"),
    ),
    "read.graph_node_search": (Need("graph.entity_embeddings", why="embedded entity names"),),
    "read.deep_v2": (
        Need(
            "llm.roles.sufficiency",
            alt=("llm.roles.plan",),
            soft=True,
            why="unbound, the deep tier is rules-only with zero model calls (agentic_unbound)",
        ),
    ),
    # --- trust ---
    "firewall.gates_v2": (
        Need("firewall.enabled", why="the gates sit on the firewall's flagging"),
    ),
    # --- offline ---
    "offline.profile_card_v2": (Need("offline.session_close_v2", why="the call-2 profile ops"),),
    "offline.sleep_scoped": (Need("offline.sleep_v2", why="the sleep cycle it scopes"),),
    # --- economy ---
    "economy.ledger_persist": (Need("economy.ledger_v2", why="the ledger it persists"),),
    "economy.budget": (
        Need("economy.ledger_v2", why="the spend it limits"),
        Need("workers.outbox", why="the deferred derivation it pauses"),
    ),
    # --- privacy ---
    "privacy.erasure_redact_hidden": (Need("privacy.erasure_v2", why="the erasure plan"),),
    "privacy.erasure_rederive": (Need("privacy.erasure_v2", why="the erasure plan"),),
}


def missing_needs(config: Any, key: str) -> list[Need]:
    """The prerequisites of ``key`` that do not hold in ``config`` (empty when ``key`` is off)."""
    if not is_active(resolve_path(config, key)):
        return []
    return [n for n in REQUIRES.get(key, ()) if not _holds(config, n)]


def check_requires(config: Any, *, strict: bool = False) -> None:
    """Raise :class:`ConfigError` naming the first flag whose prerequisite is missing.
    ``strict`` also enforces the soft needs (used by the eval arm validator)."""
    for key in REQUIRES:
        for need in missing_needs(config, key):
            if need.soft and not strict:
                continue
            raise ConfigError(
                f"{key} needs {need.describe()} ({need.why}); without it the flag is silently inert"
            )
