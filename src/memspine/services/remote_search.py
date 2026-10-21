"""Shared safe primitives for remote search adapters."""

from __future__ import annotations

import hashlib
import re


def owner_scope(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()


def document_id(owner_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{owner_id}\0{record_id}".encode()).hexdigest()


def bounded_query(query: str, *, max_chars: int = 2048, max_terms: int = 64) -> str:
    if not isinstance(query, str):
        raise TypeError("query must be text")
    text = query[:max_chars]
    # Preserve ordinary whitespace but strip controls, which are not meaningful
    # search terms and can produce inconsistent behavior across server versions.
    text = "".join(ch for ch in text if ch in "\t\n\r" or (ord(ch) >= 32 and ord(ch) != 127))
    terms = re.findall(r"[^\W_]+", text, flags=re.UNICODE)[:max_terms]
    return " ".join(terms)
