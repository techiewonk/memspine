"""#11: stored text must not forge the markers memspine writes into a context.

memspine labels what it puts in a context window: ``CURRENT (since ...)`` and
``HISTORY (superseded):`` on keyed facts, the ``FACTS (mined ...)`` cards
header, the untrusted-data wrappers, and so on. A model reads those labels as
the engine's word. If a stored note can carry the same text, it can pose as a
current fact, a mined card, or the end of a wrapper.

:func:`escape_markers` defangs every marker inside stored content before the
engine adds its own: the marker is lower-cased, square brackets become round
ones, and a backslash is put in front, so ``CURRENT (since`` in a note reads
``\\current (since``. The text stays readable, and the transform is idempotent.
"""

from __future__ import annotations

import re

from memspine.config import constants

__all__ = ["MARKER_KEYS", "escape_markers"]

#: The leading words of each marker the engine writes, matched exactly (case
#: included, as the engine writes them, so ordinary prose such as "project
#: timeline: Q3" is left alone). Kept to the distinctive head so a partial
#: imitation is caught too.
MARKER_KEYS: tuple[str, ...] = (
    constants.CURRENT_STATE_MARKER.strip(),  # "CURRENT (since"
    constants.HISTORY_MARKER.split(":", 1)[0],  # "HISTORY (superseded)"
    constants.DISPUTED_MARKER.rstrip(":"),  # "[DISPUTED"
    constants.UNTRUSTED_NOTE_MARKER.split(",", 1)[0],  # "[UNTRUSTED NOTE"
    "[END UNTRUSTED NOTE",
    constants.INSTRUCTION_FLAG_MARKER.split(" - ", 1)[0],  # "[untrusted memory content"
    constants.TIMELINE_MARKER,  # "TIMELINE:"
    constants.STANDING_MARKER,  # "USER-STATED PREFERENCES"
    constants.CLAIM_MARKER.split(",", 1)[0],  # "[CLAIM from a low-trust source"
    constants.CARDS_MARKER.split(" from ", 1)[0],  # "FACTS (mined"
    constants.PROFILE_MARKER.split(" (", 1)[0] + " (",  # "PROFILE NOTES ("
    constants.COUNT_MARKER.rstrip(":"),  # "Occurrences (dated)"
)

_PATTERN = re.compile(r"(?<!\\)(?:" + "|".join(re.escape(key) for key in MARKER_KEYS) + ")")


def _defang(match: re.Match[str]) -> str:
    return "\\" + match.group(0).lower().replace("[", "(").replace("]", ")")


def escape_markers(text: str) -> str:
    """``text`` with every engine marker defanged (see the module docstring)."""
    return _PATTERN.sub(_defang, text)
