"""#11: stored text must not forge the markers memspine writes into a context.

memspine labels what it puts in a context window: ``CURRENT (since ...)`` and
``HISTORY (superseded):`` on keyed facts, the ``FACTS (mined ...)`` cards
header, the untrusted-data wrappers, and so on. A model reads those labels as
the engine's word. If a stored note can carry the same text, it can pose as a
current fact, a mined card, or the end of a wrapper.

:func:`escape_markers` defangs every marker inside stored content before the
engine adds its own: the marker is lower-cased, square brackets become round
ones, and a backslash is put in front, so ``CURRENT (since`` in a note reads
``\\current (since``. Disguised copies (full-width letters, zero-width
characters, other casings) are caught too. The text stays readable, and the
transform is idempotent.
"""

from __future__ import annotations

import re
import unicodedata

from memspine.config import constants

__all__ = ["MARKER_KEYS", "escape_markers"]

#: The leading words of each marker the engine writes. Kept to the distinctive
#: head so a partial imitation is caught too.
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

#: Matching runs on a normalised view of the text (NFKC, invisible format
#: characters such as zero-width spaces removed) and ignores case, so a
#: full-width, zero-width-split or re-cased copy of a marker is caught. A match
#: written entirely in lower case is left alone when the marker itself has
#: capitals: that is the escaped form, and ordinary prose ("project timeline:
#: Q3") reads that way.
_PATTERN = re.compile(
    r"(?<!\\)(?:" + "|".join(re.escape(key) for key in MARKER_KEYS) + ")", re.IGNORECASE
)
_HAS_CAPS = {key.casefold(): key != key.lower() for key in MARKER_KEYS}


def _normalised(text: str) -> tuple[str, list[int]]:
    """``text`` NFKC-normalised per character with format characters (zero-width
    space/joiners, soft hyphen, BOM) dropped, and for each output character the
    index of the original character it came from."""
    chars: list[str] = []
    owner: list[int] = []
    for index, char in enumerate(text):
        if unicodedata.category(char) == "Cf":
            continue
        folded = unicodedata.normalize("NFKC", char) if not char.isascii() else char
        chars.append(folded)
        owner.extend([index] * len(folded))
    return "".join(chars), owner


def _defanged(marker: str) -> str:
    return "\\" + marker.lower().replace("[", "(").replace("]", ")")


def escape_markers(text: str) -> str:
    """``text`` with every engine marker defanged (see the module docstring). The
    whole original span of a disguised marker is replaced by the canonical
    escaped form."""
    view, owner = _normalised(text)
    out: list[str] = []
    cursor = 0
    for match in _PATTERN.finditer(view):
        found = match.group(0)
        if found == found.lower() and _HAS_CAPS.get(found.casefold(), False):
            continue  # already lower case: the escaped form, or plain prose
        start = owner[match.start()]
        end = owner[match.end() - 1] + 1
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(_defanged(found))
        cursor = end
    if not out:
        return text
    out.append(text[cursor:])
    return "".join(out)
