"""Measurement reports must not call invalid Python slices grounded spans."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "gliner_measurement_smoke", Path(__file__).parents[2] / "scripts/smoke_gliner_local.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "span",
    [
        {"start": -4, "end": 4, "text": "Mira"},
        {"start": False, "end": 4, "text": "Mira"},
        {"start": 0, "end": 5, "text": "Mira"},
        {"start": 3, "end": 2, "text": ""},
        {"start": 0, "end": 0, "text": ""},
        {"start": 0, "end": 4, "text": "Nora"},
        {"start": "0", "end": 4, "text": "Mira"},
        None,
    ],
)
def test_invalid_offsets_or_source_text_cannot_pass_grounding(span: object) -> None:
    assert MODULE.valid_span("Mira", span) is False


def test_exact_source_offsets_pass() -> None:
    assert MODULE.valid_span("Mira", {"start": 0, "end": 4, "text": "Mira"}) is True
