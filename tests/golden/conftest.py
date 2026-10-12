"""Local conftest for the CPU golden: registers the ``golden`` marker (not in pyproject)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:  # `import harness` regardless of the pytest import mode
    sys.path.insert(0, _HERE)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "golden: CPU golden snapshot of the engine (deterministic, no model, no network)"
    )
