"""#28 (SimpleMem multi-view fields): the view tags of a mined fact.

A fact mined with ``consolidation.mine_multiview`` carries, beside its statement,
the people it involves, where it happened and its list class (topic). They are
stored as tags (``person:<name>``, ``loc:<place>``, ``topic:<class>``) holding the
normalised value, so a read leg or a derived stage can prefilter on them without a
model. Pure functions, no I/O.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence

from memspine.core.records import MemoryRecord

__all__ = [
    "LOCATION_PREFIX",
    "PERSON_PREFIX",
    "TOPIC_PREFIX",
    "normalise_view",
    "tag_values",
    "view_tags",
]

PERSON_PREFIX = "person:"
LOCATION_PREFIX = "loc:"
TOPIC_PREFIX = "topic:"


def normalise_view(text: str) -> str:
    """NFKC, case-folded, whitespace collapsed: ``" Lake  TAHOE "`` -> ``"lake tahoe"``."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def view_tags(persons: Sequence[str], location: str | None, topic: str | None) -> list[str]:
    """The view tags of one fact, in a stable order, empty values and repeats left out."""
    tags: list[str] = []
    values = [
        *((PERSON_PREFIX, p) for p in persons),
        (LOCATION_PREFIX, location),
        (TOPIC_PREFIX, topic),
    ]
    for prefix, value in values:
        norm = normalise_view(value) if value else ""
        tag = f"{prefix}{norm}"
        if norm and tag not in tags:
            tags.append(tag)
    return tags


def tag_values(record: MemoryRecord | Iterable[str], prefix: str) -> list[str]:
    """The values of ``record``'s tags that start with ``prefix``, in tag order."""
    tags = record.tags if isinstance(record, MemoryRecord) else record
    return [tag[len(prefix) :] for tag in tags if tag.startswith(prefix) and len(tag) > len(prefix)]
