"""A07: reply and reference edges, inferred from the turn order (no model, no dataset terms).

A short reply ("Sounds great!", "Yes, in May") and a turn that opens with a deictic reference
("That was fun", "We should do this again") cannot be understood without the turn they answer,
and a retriever that returns the reply alone loses it. ``read.reference_edges: on`` records, at
write time, an edge from such a turn to its antecedent through the existing ``reply_to:<id>``
tag (so ``reply_links`` and the causal walk understand it) plus a ``ref:<kind>`` tag naming why
the edge was inferred; at read time the antecedent of a retrieved reply is attached, bounded and
logged.

Two rules, both about the shape of a chat, never about a topic:

* ``short_reply``: the turn has at most ``max_words`` words and its speaker differs from the
  previous turn's; the antecedent is that previous turn (the latest turn by another speaker).
* ``deictic``: the turn opens with ``this`` / ``that`` / ``these`` / ``those`` / ``we`` (or a
  short prepositional frame such as "like that", "about this"); the antecedent is the
  immediately preceding turn, whoever spoke it.

The deictic rule wins when both hold (it names its target). A turn with an explicit ``reply_to``
keeps it; nothing is inferred over it.
"""

from __future__ import annotations

import re

__all__ = [
    "REF_PREFIX",
    "ReferenceKind",
    "antecedent_kind",
    "reference_kinds",
]

#: Tag prefix of an inferred edge's kind: ``ref:short_reply`` / ``ref:deictic``.
REF_PREFIX = "ref:"

ReferenceKind = str  # "short_reply" | "deictic"

_SPEAKER_PREFIX = re.compile(r"^\s*[\w'-]{1,30}(?: [\w'-]{1,30})?:\s+")
_WORD = re.compile(r"[A-Za-z0-9'’]+")
_OPENERS = frozenset({"this", "that", "these", "those", "we"})
#: "like that", "about this", "of those": a short frame before the deictic word
_FRAME = frozenset({"like", "about", "of", "with", "from", "for", "in", "on", "at", "by", "to"})
_FRAME_WORDS = 2


def _words(content: str) -> list[str]:
    body = _SPEAKER_PREFIX.sub("", content, count=1)
    return [w.lower().replace("’", "'") for w in _WORD.findall(body)]


def _deictic(words: list[str]) -> bool:
    head = words[: _FRAME_WORDS + 1]
    for i, w in enumerate(head):
        base = w.removesuffix("'s")
        if base in _OPENERS:
            return i == 0 or all(x in _FRAME for x in head[:i])
        if w not in _FRAME:
            return False
    return False


def antecedent_kind(
    content: str,
    speaker: str | None,
    previous_speaker: str | None,
    *,
    max_words: int = 6,
    has_previous: bool = True,
) -> ReferenceKind | None:
    """The kind of edge ``content`` needs, or None. ``speaker`` / ``previous_speaker`` are
    compared case-insensitively; an unknown (None) speaker never makes a short reply."""
    if not has_previous:
        return None
    words = _words(content)
    if not words:
        return None
    if _deictic(words):
        return "deictic"
    if (
        len(words) <= max_words
        and speaker
        and previous_speaker
        and speaker.strip().lower() != previous_speaker.strip().lower()
    ):
        return "short_reply"
    return None


def reference_kinds(tags: list[str]) -> list[str]:
    """The ``ref:`` kinds in a record's tags."""
    return [t[len(REF_PREFIX) :] for t in tags if t.startswith(REF_PREFIX)]
