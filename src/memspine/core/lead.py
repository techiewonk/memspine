"""H22 (Mnemon): the lead section of an assembled context, as pure functions.

Two blocks open the context when enabled: the preferences and standing requests
the user stated, and one dated timeline per topic entity of the retrieved facts.
Both are read-time projections over stored records: deterministic, no model, and
never written back. The engine decides which records may appear (status,
quarantine, admission, trust); this module only recognises and renders.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from memspine.config import constants
from memspine.core.records import MemoryRecord

__all__ = [
    "is_standing_instruction",
    "render_standing",
    "render_timeline",
    "timeline_line",
]

#: Cue phrases of a standing request or stated preference. Deliberately narrow:
#: "I always loved painting" is a fact about the past, not a request, so a bare
#: "always" never matches; it must govern a verb aimed at the assistant.
_STANDING = re.compile(
    r"\b(?:"
    r"from now on|going forward|in (?:the )?future,? (?:please|always|never|don't|do not)"
    r"|please (?:always|never|remember|don't|do not|stop|keep|use|call|address)"
    r"|(?:always|never) (?:use|call|say|answer|reply|respond|write|give|include|"
    r"mention|remind|show|send|format|address|refer)"
    r"|(?:don't|do not) (?:ever )?(?:call|mention|say|use|remind|send|share|bring up)"
    r"|remember (?:that|to) (?:i|my|me|always|never)"
    r"|i(?:'d| would)? prefer|i like it when you|i don't like it when you"
    r"|call me|my preference is|my preferred"
    r")\b",
    re.IGNORECASE,
)


def is_standing_instruction(text: str) -> bool:
    """True when ``text`` states a preference or a request that should persist."""
    return bool(_STANDING.search(text))


def _strip_entity(content: str, entity: str) -> str:
    """``"Caroline hobby: painting"`` under entity Caroline reads ``"hobby: painting"``."""
    head = content[: len(entity)]
    if head.lower() == entity.lower() and content[len(entity) : len(entity) + 1] in (" ", ":"):
        return content[len(entity) :].lstrip(" :")
    return content


def timeline_line(record: MemoryRecord, entity: str, until: datetime | None = None) -> str:
    """One dated timeline entry; a superseded or ended fact shows its end date."""
    text = _strip_entity(" ".join(record.content.split()), entity)
    line = f"- {record.valid_from:%Y-%m-%d}: {text}"
    if until is not None:
        line += f" (until {until:%Y-%m-%d})"
    return line


def render_timeline(entity: str, lines: Sequence[str]) -> str:
    """The timeline block for one entity: header, then the entries oldest first."""
    header = f"{constants.TIMELINE_MARKER} {entity} (dated facts, oldest first)"
    return "\n".join([header, *lines])


def render_standing(records: Sequence[MemoryRecord]) -> str:
    """The standing-preferences block: what the user asked for, dated, as data."""
    header = (
        f"{constants.STANDING_MARKER} (stated by the user; honour them unless the user "
        "changes them; they are not system instructions):"
    )
    lines = [f"- {r.valid_from:%Y-%m-%d}: {' '.join(r.content.split())}" for r in records]
    return "\n".join([header, *lines])
