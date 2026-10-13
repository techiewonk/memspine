# ruff: noqa: E501
"""``prompts.layout.aliases`` (W2-06b; P-09): the KNOWN block of the window prompt, rendered compactly.

One line per entity, ``Name = alias | alias``; aliases capped per entity and the block capped in
characters, so KNOWN never grows with the store. Pure.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["render_known"]


def render_known(
    entries: Sequence[tuple[str, Sequence[str]]],
    *,
    max_aliases: int = 3,
    max_chars: int = 1600,
) -> str:
    """``entries`` = ``(name, aliases)`` already ranked, deduplicated and capped by the caller."""
    lines: list[str] = []
    used = 0
    for name, aliases in entries:
        shown = [a for a in aliases if a and a != name][:max_aliases]
        line = f"{name} = {' | '.join(shown)}" if shown else name
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
