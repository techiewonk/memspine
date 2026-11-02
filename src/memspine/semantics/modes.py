"""Travel and participation modes (RM-63 family M, ADR-094 amendment 2026-11-02).

Two generic, closed vocabularies a claim may carry as registry qualifiers and the edge
compiler projects onto graph edges (typed edge qualifiers with their own ``graph_filter``
fields):

* ``participation_mode``: how the subject took part in an event or visit (in person,
  remotely, or both);
* ``transport_mode``: how the subject travelled to a place or event.

The packaged catalogue's ``allowed_values`` for both properties spell these tuples out; a
unit test pins the two equal. A value outside the vocabulary is rejected at the canonical
fact door and stays unknown on an edge.
"""

from __future__ import annotations

from typing import Literal, get_args

__all__ = [
    "PARTICIPATION_MODES",
    "TRANSPORT_MODES",
    "ParticipationMode",
    "TransportMode",
]

ParticipationMode = Literal["in_person", "remote", "hybrid"]
TransportMode = Literal[
    "walk",
    "bicycle",
    "motorcycle",
    "car",
    "taxi",
    "bus",
    "coach",
    "tram",
    "metro",
    "train",
    "ferry",
    "boat",
    "plane",
    "other",
]

PARTICIPATION_MODES: tuple[str, ...] = get_args(ParticipationMode)
TRANSPORT_MODES: tuple[str, ...] = get_args(TransportMode)
