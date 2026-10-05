"""#40: the packed profile header, as pure functions.

``read.profile_header_packing`` opens the volatile context with one block that packs,
within a token budget, three sections in a fixed order: the session summaries
(consolidation), then the profile observations (H14 insights), then the best other
hits for the query. It follows the ``[USER PROFILE]`` pattern of memory servers that
put a short, budgeted profile next to the question, with memspine's own
``PROFILE NOTES (`` marker as the header, so stored text cannot forge the block
(:mod:`memspine.core.escaping`) and an echoed block reads as recalled memory.

The engine chooses and gates the candidates and escapes their text; this module only
packs and renders. Packing is greedy in section order, each section best first: a
line that does not fit is skipped and a later, shorter one may still fit. Kept lines
are shown oldest first within their section (ties on ``record_id``), so the same
candidates always give the same block.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from memspine.config import constants
from memspine.core.policies.assembly import estimate_tokens
from memspine.core.records import MemoryRecord, chrono_key

__all__ = ["pack_profile", "packed_line", "render_packed_profile"]


def packed_line(record: MemoryRecord) -> str:
    """One dated line; the content's whitespace (newlines included) is collapsed, so a
    stored text can never start a line or a section of its own."""
    return f"- [{record.valid_from:%Y-%m-%d}] {' '.join(record.content.split())}"


def render_packed_profile(sections: Sequence[Sequence[MemoryRecord]]) -> str:
    """The block: the header, then each non-empty section under its label."""
    lines = [constants.PROFILE_PACK_MARKER]
    for label, records in zip(constants.PROFILE_PACK_SECTIONS, sections, strict=True):
        if not records:
            continue
        lines.append(label)
        ordered = sorted(records, key=chrono_key)
        lines.extend(packed_line(r) for r in ordered)
    return "\n".join(lines)


def pack_profile(
    candidates: Sequence[Sequence[MemoryRecord]],
    allowance: int,
    count: Callable[[str], int] = estimate_tokens,
) -> list[list[MemoryRecord]]:
    """The candidates kept per section (same order as ``candidates``) while the rendered
    block fits ``allowance`` tokens. A record already kept in an earlier section is not
    packed twice."""
    kept: list[list[MemoryRecord]] = [[] for _ in candidates]
    seen: set[str] = set()
    for index, section in enumerate(candidates):
        for record in section:
            if record.record_id in seen:
                continue
            kept[index].append(record)
            if count(render_packed_profile(kept)) <= allowance:
                seen.add(record.record_id)
            else:
                kept[index].pop()
    return kept
