"""Multi-format extraction + chunking (D-29): markitdown + chonkie, ``[ingest]``.

Only *extracted text* is ever indexed — raw binaries never enter the engine.
Plain-text formats work in the core install (D-03: slim core stays useful);
everything else needs ``pip install memspine[ingest]`` and fails loudly naming
the extra (D-10). Chunking prefers chonkie's recursive chunker and falls back
to a deterministic paragraph packer.

All functions here are synchronous CPU/disk work — callers run them via
``asyncio.to_thread`` (the store does).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memspine.config.constants import INGEST_CHUNK_MAX_CHARS
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["PLAIN_TEXT_SUFFIXES", "chunk_text", "extract_text"]

PLAIN_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".rst", ".csv", ".log"})


def extract_text(path: Path) -> str:
    """Document → text. Plain text reads directly; rich formats via markitdown."""
    if not path.is_file():
        raise StorageError(f"ingest source not found or not a file: {path}")
    if path.suffix.lower() in PLAIN_TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8")
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise MissingServiceError("resource.extraction", extra="ingest") from exc
    result = MarkItDown().convert(str(path))
    return str(result.text_content)


def chunk_text(text: str, max_chars: int = INGEST_CHUNK_MAX_CHARS) -> list[str]:
    """Text → retrieval-sized chunks. chonkie when installed, else paragraphs."""
    try:
        from chonkie import RecursiveChunker
    except ImportError:
        return _paragraph_chunks(text, max_chars)
    chunker = RecursiveChunker()
    # chonkie 1.0 types ``__call__`` as an overloaded ``Chunk | list[Chunk]``
    # union mypy will not narrow through; a single-string call returns a list of
    # chunks. Treat the result as untyped (the [ingest] extra's own contract) and
    # normalize to a flat list.
    result: Any = chunker(text)
    raw_chunks = result if isinstance(result, list) else [result]
    chunks = [str(chunk.text).strip() for chunk in raw_chunks]
    return [chunk for chunk in chunks if chunk]


def _paragraph_chunks(text: str, max_chars: int) -> list[str]:
    """Deterministic fallback: pack blank-line-separated paragraphs up to the
    budget; a single oversized paragraph is hard-split rather than dropped."""
    chunks: list[str] = []
    current = ""
    for paragraph in (part.strip() for part in text.split("\n\n")):
        if not paragraph:
            continue
        while len(paragraph) > max_chars:  # oversized paragraph: hard split
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:].strip()
        if not paragraph:
            continue
        joined = f"{current}\n\n{paragraph}" if current else paragraph
        if len(joined) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = joined
    if current:
        chunks.append(current)
    return chunks
