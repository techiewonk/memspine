"""CPU golden: two fresh runs agree with each other and with the committed snapshot."""

from __future__ import annotations

import difflib
from pathlib import Path

import harness
import pytest

SNAPSHOT = Path(__file__).parent / "snapshots" / "cpu_golden.json"

pytestmark = pytest.mark.golden


def _diff(a: str, b: str, a_name: str, b_name: str) -> str:
    lines = difflib.unified_diff(a.splitlines(), b.splitlines(), a_name, b_name, lineterm="", n=2)
    return "\n".join(list(lines)[:200])


async def test_cpu_golden(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "run1", tmp_path / "run2"
    first_dir.mkdir()
    second_dir.mkdir()
    first = harness.dumps(await harness.run(first_dir))
    second = harness.dumps(await harness.run(second_dir))
    assert first == second, "two runs differ:\n" + _diff(first, second, "run1", "run2")
    committed = SNAPSHOT.read_text(encoding="utf-8")  # universal newlines: autocrlf-safe
    assert first == committed, (
        "snapshot differs from the committed golden (see tests/golden/README.md):\n"
        + _diff(committed, first, "committed", "current")
    )
