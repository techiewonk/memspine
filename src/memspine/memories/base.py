"""BaseMemory ABC: what every memory-type package declares (M13).

``needs`` mirrors the registry dependency table (§3); ``required_services``
feeds the D-10 hard-fail check. Hooks are the seams the engine calls — a
memory type never talks to storage directly, only through the write door.
"""

from __future__ import annotations

from typing import ClassVar

from memspine.core.records import MemoryRecord

__all__ = ["BaseMemory"]


class BaseMemory:
    """Concrete-with-defaults base (not abstract): a memory type overrides only
    the hooks it cares about; the defaults are valid no-op behavior."""

    #: Memory type name — must match a registry entry.
    name: ClassVar[str] = ""
    #: Hard dependencies on other memory types (mirrors core.registry).
    needs: ClassVar[tuple[str, ...]] = ()
    #: Optional services this type requires (feeds D-10).
    required_services: ClassVar[tuple[str, ...]] = ()

    async def on_write(self, record: MemoryRecord) -> None:
        """Called after a record of this type passes the write door."""

    async def on_read(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        """Called on retrieval; may filter or annotate. Default: pass-through."""
        return records

    async def on_forget(self, namespace: str, record_id: str) -> None:
        """M7 delete hook: called after a FORGET event lands, so a memory type
        can drop derived state (caches, indexes) that references the record."""
