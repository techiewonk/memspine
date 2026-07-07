"""Smoke tests — keep the package importable and versioned as the scaffold grows."""

from __future__ import annotations

import memspine


def test_version_is_exposed() -> None:
    assert isinstance(memspine.__version__, str)
    assert memspine.__version__.count(".") >= 2
