#!/usr/bin/env python3
"""Pass #5 orchestrator — extract staging, rebuild hub + all ecosystem CSV exports."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = [
    ROOT / "docs" / "survey" / "_staging" / "_extract_to_staging.py",
    ROOT / "docs" / "exports" / "_rebuild_prompts_hub.py",
    ROOT / "docs" / "exports" / "_rebuild_prompts_csv.py",
    ROOT / "docs" / "exports" / "_rebuild_ecosystem_csvs.py",
]


def run(script: Path) -> None:
    print(f"\n=== {script.name} ===")
    subprocess.run([sys.executable, str(script)], check=True, cwd=str(ROOT))


def main() -> None:
    for script in SCRIPTS:
        if script.exists():
            run(script)
        else:
            print(f"SKIP missing {script.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
