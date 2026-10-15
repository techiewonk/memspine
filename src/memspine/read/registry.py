"""Typed registry of read legs (MOD-02): leg name -> source family.

The family (``semantic`` | ``lexical`` | ``temporal``) says which evidence a leg votes from, so
fusion can tell independent votes from a leg that replays another leg's order (R02 / I75 v2).
Built-ins are the legs the engine already runs; a plug-in leg registers its family string.
Names with a prefix in :data:`FAMILY_PREFIXES` (``speaker_vote*``, ``temporal*``) resolve by prefix.
"""

from __future__ import annotations

from memspine.plugins import Registry

__all__ = ["FAMILY_PREFIXES", "READ_LEGS", "leg_family"]

READ_LEGS: Registry[str] = Registry("read_legs")

for _leg, _family in (
    ("vector", "semantic"),
    ("perspective", "semantic"),
    ("maxsim", "semantic"),
    ("session", "semantic"),
    ("graph_nodes", "semantic"),
    ("lexical", "lexical"),
    ("entity", "lexical"),
    ("view_tags", "lexical"),
):
    READ_LEGS.register(_leg, _family)

FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("speaker_vote", "semantic"),
    ("temporal", "temporal"),
)


def leg_family(name: str) -> str:
    """The source family of a leg name; ``other`` when unknown."""
    fam = READ_LEGS.find(name)
    if fam is not None:
        return fam
    for prefix, family in FAMILY_PREFIXES:
        if name.startswith(prefix):
            return family
    return "other"
