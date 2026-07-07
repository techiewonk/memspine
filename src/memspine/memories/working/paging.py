"""MemGPT-style paging: pick which working records fall out of the hot window.

Pure selection logic — the *manager* turns the selection into page-out events
through the write door. Persona records are pinned and never paged."""

from __future__ import annotations

from memspine.core.records import MemoryRecord

__all__ = ["select_page_out"]


def select_page_out(records: list[MemoryRecord], page_size: int) -> list[MemoryRecord]:
    """Oldest-first eviction of unpinned working records beyond ``page_size``."""
    unpinned = [record for record in records if record.source.channel != "persona"]
    overflow = len(unpinned) - page_size
    if overflow <= 0:
        return []
    unpinned.sort(key=lambda record: record.recorded_at)
    return unpinned[:overflow]
