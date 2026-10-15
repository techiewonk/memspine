"""Rewrite ``snapshots/cpu_golden.json``. Run manually; review the diff before committing.

PYTHONPATH=src python tests/golden/regen.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import harness

SNAPSHOT = HERE / "snapshots" / "cpu_golden.json"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        text = harness.dumps(asyncio.run(harness.run(Path(tmp))))
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_bytes(text.encode("utf-8"))
    print(f"wrote {SNAPSHOT} ({len(text)} bytes)")


if __name__ == "__main__":
    main()
