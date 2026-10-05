"""GP-2: the entity layer of the association graph (ADR-015 amendment, 2026-10-05).

With ``memories.associative.policies.entity_nodes`` on, the graph projector adds
one node per named entity and a ``mentions`` edge from each record to every
entity it names. Both are derived from WRITE payloads only (the record's
``entity`` field, and the ``dst:<name>`` tag an edge fact carries), so a rebuild
reproduces exactly what incremental projection built.

- **Canonical name**: NFKC, casefolded, whitespace collapsed. "Melanie",
  " MELANIE " and "melanie" are one entity.
- **Node id**: ``ent:<namespace>:<canonical>``. Graph node ids are global keys
  in every adapter, so the namespace is part of the id: two tenants naming
  "Melanie" get two nodes, and forgetting one tenant's last mention never
  touches the other's node.
- **Rejected names**: empty, one character, digits only, a blocklisted junk
  name (pronouns and day words such as "it", "today", "tomorrow", "luck"), or,
  when an allowed list is configured, any name outside it. An edge fact whose
  destination is its own source (a self-edge) adds no second mention.
- **Edge weight**: the record's trust (capped at 1.0), so a graph path is never
  stronger than the record it runs through (GP-10). A trust-0 record's mention
  is a tombstone, gone for every reader.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from memspine.config.constants import CUE_TAG, ENTITY_NODE_BLOCKLIST
from memspine.core.records import MemoryRecord

__all__ = [
    "ENTITY_LABEL",
    "ENTITY_PREFIX",
    "MENTIONS_REL",
    "EntityPolicy",
    "canonical_entity",
    "entity_node_id",
    "is_entity_node",
    "parse_entity_node",
    "record_entity_names",
]

#: Node-id prefix of an entity node.
ENTITY_PREFIX = "ent:"
#: The node label of an entity node.
ENTITY_LABEL = "entity"
#: The rel of a record -> entity edge. Reserved (system-written only, ADR-015 §2).
MENTIONS_REL = "mentions"
#: The tag an edge fact carries for its destination entity (GP-1).
_DST_TAG = "dst:"

_SPACE = re.compile(r"\s+")


def canonical_entity(name: str) -> str:
    """NFKC, casefolded, whitespace-collapsed ``name``."""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", name).casefold()).strip()


def entity_node_id(namespace: str, canonical: str) -> str:
    """The graph node id of entity ``canonical`` in ``namespace``."""
    return f"{ENTITY_PREFIX}{namespace}:{canonical}"


def is_entity_node(node_id: str) -> bool:
    return node_id.startswith(ENTITY_PREFIX)


def parse_entity_node(node_id: str) -> tuple[str, str] | None:
    """``(namespace, canonical)`` of an entity node id, else None. Namespaces never
    contain ``:`` (``core.namespace``), so the first colon after the prefix splits."""
    if not is_entity_node(node_id):
        return None
    namespace, sep, canonical = node_id[len(ENTITY_PREFIX) :].partition(":")
    return (namespace, canonical) if sep and canonical else None


@dataclass(frozen=True)
class EntityPolicy:
    """``memories.associative.policies.entity_nodes``: ``false`` (default, off),
    ``true`` (on, default blocklist) or a mapping with optional ``blocklist``
    (replaces the default list) and ``allowed`` (only these names become nodes)."""

    blocklist: frozenset[str] = frozenset()
    allowed: frozenset[str] | None = None

    @classmethod
    def from_policy(cls, value: Any) -> EntityPolicy | None:
        """The policy for a raw config value; None when entity nodes are off."""
        if not value:
            return None
        options: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
        raw_block = options.get("blocklist")
        blocklist = ENTITY_NODE_BLOCKLIST if raw_block is None else _names(raw_block)
        raw_allowed = options.get("allowed")
        allowed = None if raw_allowed is None else frozenset(_names(raw_allowed))
        return cls(blocklist=frozenset(_names(blocklist)), allowed=allowed)

    def accepts(self, canonical: str) -> bool:
        if len(canonical) < 2 or canonical.isdigit() or canonical in self.blocklist:
            return False
        return self.allowed is None or canonical in self.allowed


def _names(raw: Iterable[Any]) -> frozenset[str]:
    return frozenset(canonical_entity(str(name)) for name in raw if str(name).strip())


def record_entity_names(record: MemoryRecord, policy: EntityPolicy) -> list[str]:
    """The canonical entity names ``record`` mentions, in first-seen order.

    From the ``entity`` field and the ``dst:<name>`` tags. Cues (retrieval keys)
    and shared bookkeeping mention nothing. A destination equal to the source is a
    self-edge: it adds no second mention.
    """
    if CUE_TAG in record.tags or record.memory_type == "shared":
        return []
    raw: list[str] = []
    if record.entity:
        raw.append(record.entity)
    raw.extend(tag[len(_DST_TAG) :] for tag in record.tags if tag.startswith(_DST_TAG))
    names: list[str] = []
    for name in raw:
        canonical = canonical_entity(name)
        if canonical not in names and policy.accepts(canonical):
            names.append(canonical)
    return names
