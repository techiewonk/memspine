"""N13 (plan v3.2, Mnemon ``focused``): a query-anchored excerpt of a long record.

A long multi-line record (a pasted document, a meeting note, a long message) spends
its tokens on lines the question never touches. :func:`focused_excerpt` keeps the
lines that share the most content words with the query, each with the line after it
(the answer often follows the line that names the topic), in their original order,
and marks every cut with an ellipsis line. Short records come back unchanged. Lexical
only, no model; the stored record is never changed.
"""

from __future__ import annotations

from memspine.core.query_shape import content_words

__all__ = ["ELLIPSIS", "focused_excerpt"]

ELLIPSIS = "…"


def focused_excerpt(text: str, query: str, *, min_lines: int = 6, keep: int = 2) -> str:
    """``text`` cut to its ``keep`` best-matching lines and their next lines.

    Unchanged when ``text`` has fewer than ``min_lines`` non-empty lines or no line
    shares a content word with ``query``."""
    lines = text.splitlines()
    if sum(1 for line in lines if line.strip()) < min_lines:
        return text
    wanted = content_words(query)
    scored = [
        (len(wanted & content_words(line)), index)
        for index, line in enumerate(lines)
        if line.strip()
    ]
    best = sorted((pair for pair in scored if pair[0] > 0), key=lambda p: (-p[0], p[1]))[:keep]
    if not best:
        return text
    chosen: set[int] = set()
    for _, index in best:
        chosen.add(index)
        follower = next((i for i in range(index + 1, len(lines)) if lines[i].strip()), None)
        if follower is not None:
            chosen.add(follower)
    out: list[str] = []
    previous = -1
    for index in sorted(chosen):
        if index != previous + 1:
            out.append(ELLIPSIS)
        out.append(lines[index])
        previous = index
    if previous < len(lines) - 1 and any(line.strip() for line in lines[previous + 1 :]):
        out.append(ELLIPSIS)
    return "\n".join(out)
