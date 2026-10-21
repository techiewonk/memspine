"""Read-only drift gate for provider release references; never starts services."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_references(root: Path = ROOT) -> dict:
    """Run generators in isolated processes so their import hooks cannot leak."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root / "evals")))
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    commands = {
        "provider_defaults": [sys.executable, "scripts/dump_provider_defaults.py", "--check"],
        "stage_parameters": [sys.executable, "scripts/build_stage_params.py", "--check"],
        "configuration_snapshot": [sys.executable, "scripts/dump_config_baseline.py"],
    }
    checks = []
    for name, command in commands.items():
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        passed = result.returncode == 0
        if passed and name == "configuration_snapshot":
            expected = json.loads(
                (root / "tests/unit/config/data/config_dump_baseline.json").read_text(
                    encoding="utf-8"
                )
            )
            passed = json.loads(result.stdout) == expected
        # Native settings/errors can contain secrets: keep only the gate name and code.
        checks.append(
            {
                "name": name,
                "passed": passed,
                "returncode": result.returncode,
                "command": command[1:],
            }
        )
    references = (
        "docs/PROVIDER_DEFAULTS.md",
        "docs/v2/STAGE_PARAMS.md",
        "docs/v2/data/stage_params.json",
        "tests/unit/config/data/config_dump_baseline.json",
        "pyproject.toml",
        "uv.lock",
    )
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
        "sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in references
        },
        "limits": [
            "Read-only generated-reference comparison; not a full release sweep or service workload acceptance."
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs/provider_verification/release-reference-check.json",
    )
    args = parser.parse_args()
    report = check_references()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for check in report["checks"]:
        print(f"{check['name']}: {'PASS' if check['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
