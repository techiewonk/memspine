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
from collections.abc import Sequence
from typing import Any

from memspine.core.records import MemoryRecord

__all__ = ["final_answer", "numbered_context", "verification"]

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


def numbered_context(context: Sequence[MemoryRecord] | str) -> tuple[list[str], list[str]]:
    """#39: ``context`` as numbered prompt lines ``[n] text``, and the id behind each line:
    a record id (one line per record, whitespace collapsed), or ``L<n>`` for a plain-text
    context (one line per non-empty text line)."""
    if isinstance(context, str):
        texts = [line.strip() for line in context.splitlines() if line.strip()]
        ids = [f"L{n}" for n in range(1, len(texts) + 1)]
    else:
        texts = [" ".join(r.content.split()) for r in context]
        ids = [r.record_id for r in context]
    return [f"[{n}] {text}" for n, text in enumerate(texts, start=1)], ids


def verification(
    supported: bool,
    evidence: Sequence[int],
    revised_answer: str | None,
    ids: Sequence[str],
    answer: str,
) -> dict[str, Any]:
    """#39: a verify-answer verdict as ``{supported, evidence_ids, revised_answer}``.

    Evidence numbers outside the context are dropped (and repeats); a revision is kept
    only for an unsupported answer and only when it differs from the answer."""
    evidence_ids = [ids[n - 1] for n in dict.fromkeys(evidence) if 1 <= n <= len(ids)]
    revised = revised_answer
    if supported or revised is None or revised.strip() == answer.strip():
        revised = None
    return {"supported": supported, "evidence_ids": evidence_ids, "revised_answer": revised}
