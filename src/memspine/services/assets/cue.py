"""When does a question need what the picture shows? A generic surface cue, no model, no gold.

Fires on a what / which / who / where question that asks for an object, title, place or
similar identity word, or that points at a picture directly ("in the photo", "shown on").
"""

from __future__ import annotations

import re

__all__ = ["needs_visual_detail"]

_IDENTITY = (
    r"title|name|kind|type|sort|color|colour|breed|brand|book|movie|film|show|painting|"
    r"picture|photo|photograph|image|sign|poster|logo|flower|plant|animal|pet|place|location|"
    r"building|object|item|thing|artwork|dish|food|band|song|album|sculpture|mural|"
    r"banner|shirt|tattoo|landmark|monument|scene|park|beach|statue"
)
_WH_IDENTITY = re.compile(
    rf"\b(?:what|which|who|where)\b[^?.]{{0,40}}\b(?:{_IDENTITY})s?\b", re.IGNORECASE
)
_POINTS_AT_PICTURE = re.compile(
    r"\b(?:in|on|from|of) (?:the|that|this|a) (?:photo|picture|image|pic|photograph|painting)\b"
    r"|\b(?:shown|depicted|pictured|visible|written|displayed|printed) (?:in|on|at)\b"
    r"|\bwhat (?:does|did) (?:it|the \w+) (?:look|say|show|depict|read)\b"
    r"|\b(?:photo|picture|image|pic)\b[^?.]{0,25}\b(?:show|shows|showed|depict|of)\b",
    re.IGNORECASE,
)


def needs_visual_detail(question: str) -> bool:
    """True when answering plausibly needs the attachment itself, not just its caption."""
    return bool(_WH_IDENTITY.search(question) or _POINTS_AT_PICTURE.search(question))
