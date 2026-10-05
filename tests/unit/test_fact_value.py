"""#3 corroboration: fact values keep the symbols that change their meaning."""

from __future__ import annotations

import pytest

from memspine.engine import _fact_value

# ── fact values keep the symbols that change their meaning ────────────────


@pytest.mark.parametrize(
    ("left", "right"),
    [("-500", "500"), ("C++", "C"), ("$100", "€100"), ("3.5", "3 5"), ("C#", "C"), ("50%", "50")],
)
def test_fact_value_keeps_meaningful_symbols(left: str, right: str) -> None:
    assert _fact_value(left) != _fact_value(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1,000", "1000"),
        ("$1,250,000", "$1250000"),
        ("Lives in  PARIS.", "lives in paris"),
        ("\uff21\uff22\uff23", "abc"),
        ("blue-green", "blue green"),
    ],
)
def test_fact_value_equates_formatting_variants(left: str, right: str) -> None:
    assert _fact_value(left) == _fact_value(right)
