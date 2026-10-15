# ruff: noqa: E501
"""One-way Markdown export view of a namespace (M-01), built from the record read model.

The view is deterministic (records sorted by validity start then id) and carries each record's id and
layer in a comment line, so an edit can later re-enter as an ordinary write with lineage to that id. The
re-import path is not built here (it needs the W-01 write door); this is the export half.
"""

from __future__ import annotations

from collections.abc import Sequence

from memspine.store.records import MemoryRecord

__all__ = ["MarkdownExportView"]


class MarkdownExportView:
    """Render the records of one namespace as Markdown."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    def render(self, records: Sequence[MemoryRecord]) -> str:
        own = sorted(
            (r for r in records if r.namespace == self.namespace),
            key=lambda r: (r.valid_from.isoformat() if r.valid_from else "", r.record_id),
        )
        lines = [f"# Memory export: {self.namespace}", ""]
        for r in own:
            lines.append(f"<!-- id={r.record_id} layer={r.layer} type={r.memory_type} -->")
            lines.append(f"- {r.content.strip()}")
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"
