"""Resource extraction + chunking (D-29): core fallbacks stay green."""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine.exceptions import MissingServiceError, StorageError
from memspine.memories.resource.extraction import _paragraph_chunks, chunk_text, extract_text


def test_plain_text_reads_without_the_ingest_extra(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text("# Title\n\nParagraph one.\n\nParagraph two.", encoding="utf-8")
    assert "Paragraph one." in extract_text(doc)


def test_rich_formats_require_ingest_extra_or_fail_loudly(tmp_path: Path) -> None:
    doc = tmp_path / "report.docx"
    doc.write_bytes(b"PK\x03\x04 not a real docx")
    try:
        import markitdown  # noqa: F401

        pytest.skip("markitdown installed — the extra path is live, not refusable")
    except ImportError:
        pass
    with pytest.raises(MissingServiceError, match=r"memspine\[ingest\]"):
        extract_text(doc)


def test_missing_file_is_loud(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="not found"):
        extract_text(tmp_path / "ghost.txt")


def test_paragraph_fallback_packs_to_budget() -> None:
    text = "\n\n".join(f"paragraph {i} " + "x" * 50 for i in range(10))
    chunks = _paragraph_chunks(text, max_chars=200)
    assert all(len(chunk) <= 200 for chunk in chunks)
    reassembled = "\n\n".join(chunks).replace("\n\n", " ")
    assert "paragraph 0" in reassembled and "paragraph 9" in reassembled  # nothing dropped


def test_paragraph_fallback_hard_splits_oversized_paragraph() -> None:
    text = "y" * 5000
    chunks = _paragraph_chunks(text, max_chars=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == 5000


def test_chunk_text_returns_nonempty_chunks() -> None:
    chunks = chunk_text("First paragraph.\n\n\n\nSecond paragraph.")
    assert chunks and all(chunk.strip() for chunk in chunks)
