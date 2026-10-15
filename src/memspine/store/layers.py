# ruff: noqa: E501
"""Record envelope derivations (F-02a; M-02 layers, M-03 type tags, M-07 provenance class, M-08 axes).

Pure functions over primitives (no ``MemoryRecord`` import), so the record model, the Alembic backfill
and the tests share one mapping.

Layer mapping (M-02 / M-03), by memory type, then by write channel for the ``semantic`` type:

==========================================================  =====
what                                                        layer
==========================================================  =====
episodic turn, resource, working, shared (grants)           L0
semantic, mined / calibrated / user-written fact            L1
semantic, session summary (``consolidation``), window       L2
episode (``window_episode``), ``anticipation``,
``session_close``, ``multires``
semantic, graph-derived (``extract_graph``, ``rule_edges``, L3
``reorganize`` / ``community_summary``, ``entity_summary``)
semantic, ``observation`` channel                           L4
semantic, ``standing`` channel                              L5
reflective (observations, reflections)                      L4
procedural, prospective (standing answers, plans)           L5
==========================================================  =====

Unknown memory types fall back to L0 (the raw layer): a layer is a label, never a gate.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

__all__ = [
    "DERIVED_CHANNELS",
    "LAYERS",
    "ProvenanceClass",
    "derive_axes",
    "derive_layer",
    "derive_provenance_class",
]

Layer = Literal["L0", "L1", "L2", "L3", "L4", "L5"]
LAYERS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")

ProvenanceClass = Literal[
    "user_stated", "assistant_proposed", "user_confirmed", "tool", "document", "external", "derived"
]

#: Memory type to layer (M-03). ``semantic`` is refined by channel in :func:`derive_layer`.
_TYPE_LAYER: dict[str, str] = {
    "working": "L0",
    "episodic": "L0",
    "resource": "L0",
    "shared": "L0",
    "semantic": "L1",
    "associative": "L3",
    "reflective": "L4",
    "procedural": "L5",
    "prospective": "L5",
}

#: Write channels of a ``semantic`` record that lift it above L1.
_CHANNEL_LAYER: dict[str, str] = {
    "consolidation": "L2",
    "window_episode": "L2",  # constants.WINDOW_EPISODE_CHANNEL
    "anticipation": "L2",
    "session_close": "L2",  # constants.SESSION_CLOSE_CHANNEL (MF-01): episode and summary legs
    "multires": "L2",  # multi-resolution summaries
    "community_summary": "L3",
    "observation": "L4",  # observation channel written through the semantic door
    "standing": "L5",  # standing answers
    "extract_graph": "L3",
    "rule_edges": "L3",
    "reorganize": "L3",
    "entity_summary": "L3",  # constants.ENTITY_SUMMARY_CHANNEL
}

#: Channels whose records are machine-derived from parents: their provenance class is ``derived``
#: whatever role they carry (``constants.DERIVED_ROLE`` is ``assistant``).
DERIVED_CHANNELS: frozenset[str] = frozenset(
    {
        "mining",
        "calibration",
        "consolidation",
        "window_episode",
        "anticipation",
        "extract_graph",
        "rule_edges",
        "reorganize",
        "entity_summary",
        "reflection",
        "lesson",
        "trajectory",
    }
)

_ROLE_CLASS: dict[str, str] = {
    "user": "user_stated",
    "operator": "user_stated",
    "assistant": "assistant_proposed",
    "tool": "tool",
    "system": "tool",
    "document": "document",
    "external": "external",
}


def derive_layer(memory_type: str, channel: str = "") -> Layer:
    """The layer a record of ``memory_type`` written through ``channel`` sits on."""
    # ``memory_type`` is a real MEMORY TYPE (``record.memory_type``), never a write-route name such
    # as the door's "summary" (NEW-03): a route name is not in ``_TYPE_LAYER`` and fell to L0.
    if memory_type == "semantic" and channel in _CHANNEL_LAYER:
        return _CHANNEL_LAYER[channel]  # type: ignore[return-value]
    return _TYPE_LAYER.get(memory_type, "L0")  # type: ignore[return-value]


def derive_provenance_class(
    role: str, channel: str = "", has_parents: bool = False
) -> ProvenanceClass:
    """Closed provenance class (M-07) from today's ``source.role`` / ``source.channel``.

    Derived channels, and any record with parents, are ``derived``. ``user_confirmed`` cannot be read off a
    role: it is only ever set explicitly (a user's yes to an assistant proposal).
    """
    if channel in DERIVED_CHANNELS or has_parents:
        return "derived"
    return _ROLE_CLASS.get(role, "external")  # type: ignore[return-value]


def derive_axes(tags: Iterable[str]) -> dict[str, object]:
    """Speaker, addressee, subjects and asker from the perspective tags (``spk:``/``addr:``/``sub:``/``ask:``).

    First tag wins for the single-valued axes; ``subject`` keeps tag order. Absent axes are ``None`` / ``[]``.
    """
    speaker: str | None = None
    addressee: str | None = None
    asker: str | None = None
    subject: list[str] = []
    for tag in tags:
        if tag.startswith("spk:") and len(tag) > 4:
            speaker = speaker or tag[4:]
        elif tag.startswith("addr:") and len(tag) > 5:
            addressee = addressee or tag[5:]
        elif tag.startswith("ask:") and len(tag) > 4:
            asker = asker or tag[4:]
        elif tag.startswith("sub:") and len(tag) > 4 and tag[4:] not in subject:
            subject.append(tag[4:])
    return {"speaker": speaker, "subject": subject, "addressee": addressee, "asker": asker}
