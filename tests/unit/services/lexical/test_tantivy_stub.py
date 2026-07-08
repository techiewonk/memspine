"""Tantivy adapter is a stub in v0.1: construction hard-fails naming [tantivy]."""

from __future__ import annotations

import pytest

from memspine.exceptions import MissingServiceError
from memspine.services.lexical.tantivy import TantivyLexical


def test_tantivy_construction_names_the_extra() -> None:
    with pytest.raises(MissingServiceError, match="tantivy"):
        TantivyLexical()
