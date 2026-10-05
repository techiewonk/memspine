"""#34: the short final answer of a reply that reasons first.

``chat@dated3`` asks the model for one or two sentences of reasoning, then a final line
``Answer: <short answer>``. A grader (or a caller showing the answer) wants that last
part only. :func:`final_answer` extracts it robustly: hidden ``<think>`` blocks are
dropped, the LAST ``Answer:`` / ``Final answer:`` marker wins (Markdown bold and a
missing space tolerated), and a reply with no marker comes back whole, so a model that
skips the reasoning still yields its answer. Pure function, no model.
"""

from __future__ import annotations

import re

__all__ = ["final_answer"]

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
#: ``Answer:``, ``**Answer:**``, ``Final answer:``, a fullwidth colon ... after a boundary.
_MARKER = re.compile(
    r"(?:^|(?<=[\s*>#_(\[]))\**\s*(?:final\s+|short\s+)?answer\s*\**\s*[:\uff1a]\s*\**",
    re.I,
)


_OPEN_THINK = re.compile(r"<think>", re.I)


def _first_line(text: str) -> str:
    """The first non-empty line of ``text``, Markdown bold stripped."""
    for line in text.splitlines():
        stripped = line.strip().strip("*").strip()
        if stripped:
            return stripped
    return ""


def final_answer(text: str) -> str:
    """The first non-empty line after the last ``Answer:`` marker, else the whole
    reply (stripped).

    An empty answer after the last marker falls back to the text after the previous
    one, then to the reply before the first marker; a reply that is only a marker
    gives "". A ``<think>`` block cut off before ``</think>`` is dropped.
    """
    cleaned = _THINK.sub("", text)
    if "</think>" in cleaned.lower():  # an unclosed block: keep what follows its end
        cleaned = re.split(r"</think>", cleaned, flags=re.I)[-1]
    opened = _OPEN_THINK.search(cleaned)
    if opened is not None:  # a block never closed (cut off): dropped unless it holds a marker
        rest = cleaned[opened.end() :]
        cleaned = cleaned[: opened.start()] + (rest if _MARKER.search(rest) else "")
    cleaned = cleaned.strip()
    matches = list(_MARKER.finditer(cleaned))
    for match in reversed(matches):
        answer = _first_line(cleaned[match.end() :])
        if answer:
            return answer
    if matches:  # only dangling markers: the reply before the first one
        return cleaned[: matches[0].start()].strip()
    return cleaned
