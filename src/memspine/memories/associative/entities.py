"""GP-2: the entity layer of the association graph (ADR-015 amendment, 2026-10-05).

With ``memories.associative.policies.entity_nodes`` on, the graph projector adds
one node per named entity and a ``mentions`` edge from each record to every
entity it names. Both are derived from WRITE payloads only (the record's
``entity`` field, the ``dst:<name>`` tag an edge fact carries, and the entity role
fillers of the canonical claim it stores, RM-157), so a rebuild reproduces exactly
what incremental projection built.

- **Canonical name**: NFKC, casefolded, whitespace collapsed. "Melanie",
  " MELANIE " and "melanie" are one entity.
- **Node id** (RM-130, ADR-089): ``ent:<namespace>:<uuid7>``, opaque. The id is
  minted once at write time and recorded in the WRITE event (``entity_links``), so a
  replay never mints one; the name lives in the entity name index
  (:mod:`~memspine.memories.associative.entity_index`), never in the id. Two
  same-named people are two ids. Graph node ids are global keys in every adapter,
  so the namespace is part of the id. Mention surface nodes are
  ``surface:<namespace>:<label>:<surface>`` and never parse as an entity.
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

import fastuuid

from memspine.config.constants import CUE_TAG, ENTITY_NODE_BLOCKLIST
from memspine.store.events import fingerprint_payload
from memspine.store.records import MemoryRecord

__all__ = [
    "DST_TYPE_TAG",
    "ENTITY_LABEL",
    "ENTITY_PREFIX",
    "ENTITY_REF_TAG",
    "ENTITY_SURFACE_LABEL",
    "ENTITY_TYPE_TAG",
    "HAS_MENTION_REL",
    "MENTIONS_REL",
    "MENTION_OCCURRENCE_LABEL",
    "REFERS_TO_REL",
    "SURFACE_PREFIX",
    "EntityPolicy",
    "canonical_entity",
    "is_entity_node",
    "match_key",
    "name_fingerprint",
    "new_entity_id",
    "parse_entity_node",
    "record_entity_names",
    "record_name_types",
    "role_fillers",
    "surface_node_id",
    "types_compatible",
]

#: Node-id prefix of a canonical entity node (``ent:<ns>:<uuid7>``).
ENTITY_PREFIX = "ent:"
#: Node-id prefix of a mention surface node (``surface:<ns>:<label>:<surface>``, RM-130).
SURFACE_PREFIX = "surface:"
#: RM-130: ``entity_type:<type>`` types a record's subject entity, ``dst_type:<type>`` its object.
ENTITY_TYPE_TAG = "entity_type:"
DST_TYPE_TAG = "dst_type:"
#: RM-130: ``entity_ref:<entity id>`` pins a record's subject to that entity (explicit link);
#: ``entity_ref:new`` makes it a new entity even when the name is known.
ENTITY_REF_TAG = "entity_ref:"
#: The node label of an entity node.
ENTITY_LABEL = "entity"
#: The rel of a record -> entity edge. Reserved (system-written only, ADR-015 §2).
MENTIONS_REL = "mentions"
#: BI-08: exact source-bound annotations use an occurrence node so repeated
#: surface forms in one source cannot collapse into one graph edge.
HAS_MENTION_REL = "has_mention"
REFERS_TO_REL = "refers_to"
MENTION_OCCURRENCE_LABEL = "mention_occurrence"
ENTITY_SURFACE_LABEL = "entity_surface"
#: The tag an edge fact carries for its destination entity (GP-1).
_DST_TAG = "dst:"

_SPACE = re.compile(r"\s+")


def canonical_entity(name: str) -> str:
    """NFKC, casefolded, whitespace-collapsed ``name``."""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", name).casefold()).strip()


_WORD = re.compile(r"\w+")


def match_key(text: str) -> str:
    """The form entity names and query n-grams are compared in: the canonical
    name's words, punctuation dropped ("Charlotte's Web" -> "charlotte s web")."""
    return " ".join(_WORD.findall(canonical_entity(text)))


def new_entity_id(namespace: str) -> str:
    """A fresh opaque entity id in ``namespace``. Minted only on the write path; the id is
    recorded in the event log and every projection reads it from there."""
    return f"{ENTITY_PREFIX}{namespace}:{fastuuid.uuid7()}"


def surface_node_id(namespace: str, label: str, surface: str) -> str:
    """The graph node id of a mention surface (BI-08): never an entity id."""
    return f"{SURFACE_PREFIX}{namespace}:{label}:{surface}"


def is_entity_node(node_id: str) -> bool:
    return parse_entity_node(node_id) is not None


def parse_entity_node(node_id: str) -> tuple[str, str] | None:
    """``(namespace, local id)`` of a canonical entity node id, else None. Namespaces never
    contain ``:`` (``core.namespace``) and neither does the local id, so a surface id or any
    other colon-bearing id never parses. The local id is opaque: a name comes from the index."""
    if not node_id.startswith(ENTITY_PREFIX):
        return None
    namespace, sep, local = node_id[len(ENTITY_PREFIX) :].partition(":")
    return (namespace, local) if sep and namespace and local and ":" not in local else None


def name_fingerprint(canonical: str) -> str:
    """The key a WRITE event's entity link names its mention by (the name itself stays out of
    the link, so a redacted record leaves no name behind in its links)."""
    return fingerprint_payload({"entity_name": canonical})


def _type(raw: object) -> str | None:
    value = canonical_entity(str(raw)) if raw is not None else ""
    return None if value in ("", "unknown", "none") else value


def types_compatible(a: str | None, b: str | None) -> bool:
    """RM-130: the same entity type, or either side untyped / unknown."""
    return a is None or b is None or a == b


def role_fillers(record: MemoryRecord) -> list[tuple[str, str, str | None]]:
    """RM-157: ``(role, surface, entity type)`` of each entity-valued role binding of the
    canonical claim ``record`` stores (``fingerprint.envelope``), in binding order. Text,
    number and temporal fillers name no entity; a redacted record (its envelope gone)
    names none."""
    raw = record.fingerprint.envelope
    bindings = raw.get("role_bindings") if isinstance(raw, Mapping) else None
    if not isinstance(bindings, list):
        return []
    out: list[tuple[str, str, str | None]] = []
    for item in bindings:
        if not isinstance(item, Mapping) or item.get("value_kind") != "entity":
            continue
        role, value = item.get("role_name"), item.get("value")
        if isinstance(role, str) and isinstance(value, str) and value.strip():
            out.append((role, value, _type(item.get("entity_type"))))
    return out


def record_name_types(record: MemoryRecord, names: Iterable[str]) -> dict[str, str | None]:
    """The entity type of each canonical name ``record`` mentions: the subject's from an
    ``entity_type:`` tag, the object's from ``dst_type:``, a role filler's from its binding
    (RM-157), else the label of a bound mention whose surface is that name (None: untyped)."""
    subject = canonical_entity(record.entity or "")
    dsts = {canonical_entity(t[len(_DST_TAG) :]) for t in record.tags if t.startswith(_DST_TAG)}
    tagged_subject = next(
        (_type(t[len(ENTITY_TYPE_TAG) :]) for t in record.tags if t.startswith(ENTITY_TYPE_TAG)),
        None,
    )
    tagged_dst = next(
        (_type(t[len(DST_TYPE_TAG) :]) for t in record.tags if t.startswith(DST_TYPE_TAG)), None
    )
    labels: dict[str, str | None] = {}
    for _, surface, entity_type in role_fillers(record):
        if entity_type is not None:
            labels.setdefault(canonical_entity(surface), entity_type)
    for mention in record.fingerprint.entity_mentions:
        labels.setdefault(canonical_entity(mention.text), _type(mention.label))
    out: dict[str, str | None] = {}
    for name in names:
        if name == subject and tagged_subject is not None:
            out[name] = tagged_subject
        elif name in dsts and tagged_dst is not None:
            out[name] = tagged_dst
        else:
            out[name] = labels.get(name)
    return out


@dataclass(frozen=True)
class EntityPolicy:
    """``memories.associative.policies.entity_nodes``: ``false`` (default, off),
    ``true`` (on, default blocklist) or a mapping with optional ``blocklist``
    (replaces the default list) and ``allowed`` (only these names become nodes)."""

    blocklist: frozenset[str] = frozenset()
    allowed: frozenset[str] | None = None
    #: G-7 (Graphiti MENTIONS for every episode): ``turn_mentions: true`` also takes
    #: the proper nouns and years a raw episodic turn names (rules, no model), minus
    #: the turn's speaker, so the graph reaches turns no fact was mined from.
    turn_mentions: bool = False

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
        return cls(
            blocklist=frozenset(_names(blocklist)),
            allowed=allowed,
            turn_mentions=bool(options.get("turn_mentions", False)),
        )

    def accepts(self, canonical: str) -> bool:
        if len(canonical) < 2 or canonical.isdigit() or canonical in self.blocklist:
            return False
        return self.allowed is None or canonical in self.allowed


def _names(raw: Iterable[Any]) -> frozenset[str]:
    return frozenset(canonical_entity(str(name)) for name in raw if str(name).strip())


def record_entity_names(record: MemoryRecord, policy: EntityPolicy) -> list[str]:
    """The canonical entity names ``record`` mentions, in first-seen order.

    From the ``entity`` field, the ``dst:<name>`` tags and the entity role fillers of the
    stored canonical claim (:func:`role_fillers`, RM-157). Cues (retrieval keys)
    and shared bookkeeping mention nothing. A destination equal to the source is a
    self-edge: it adds no second mention.
    """
    if CUE_TAG in record.tags or record.memory_type == "shared":
        return []
    raw: list[str] = []
    if record.entity:
        raw.append(record.entity)
    raw.extend(tag[len(_DST_TAG) :] for tag in record.tags if tag.startswith(_DST_TAG))
    raw.extend(surface for _, surface, _ in role_fillers(record))
    if policy.turn_mentions and record.memory_type == "episodic" and not record.entity:
        # G-7: a raw turn's own proper nouns and years (derived from the content, so a
        # rebuild projects the same mentions); the speaker is a hub, not a mention.
        from memspine.models.encoders.query.temporal_query import named_terms, speaker_of

        speaker = speaker_of(record.content)
        raw.extend(
            named_terms(
                record.content,
                exclude=[speaker] if speaker else [],
                skip_sentence_start=True,
            )
        )
    names: list[str] = []
    for name in raw:
        canonical = canonical_entity(name)
        if canonical not in names and policy.accepts(canonical):
            names.append(canonical)
    return names
