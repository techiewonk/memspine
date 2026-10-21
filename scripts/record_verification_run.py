"""Append a portable, manually supplied verification record to JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

STATUSES = {"passed", "failed", "skipped", "not_run"}
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|redis)://[^/@\s:]+:[^/@\s]+@"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def _check_text(label: str, value: str) -> None:
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError(f"{label} appears to contain credentials; remove them before recording")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_records(root: Path, references: Sequence[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    resolved_root = root.resolve()
    for reference in references:
        _check_text("artifact reference", reference)
        candidate = Path(reference)
        if candidate.is_absolute():
            raise ValueError(f"artifact reference must be repository-relative: {reference}")
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(resolved_root):
            raise ValueError(f"artifact reference escapes repository: {reference}")
        if not resolved.is_file():
            raise ValueError(f"artifact does not exist or is not a file: {reference}")
        result.append(
            {
                "path": resolved.relative_to(resolved_root).as_posix(),
                "sha256": _sha256(resolved),
            }
        )
    return result


def record_run(
    *,
    root: Path,
    output: Path,
    gate: str,
    status: str,
    command: str,
    passed: int,
    failed: int,
    skipped: int,
    artifacts: Sequence[str] = (),
    evidence: Sequence[str] = (),
) -> dict[str, object]:
    """Validate explicit evidence and append one record; never run ``command``."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(sorted(STATUSES))}")
    for name, count in (("passed", passed), ("failed", failed), ("skipped", skipped)):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{name} count must be a non-negative integer")
    if status == "passed" and (failed != 0 or passed == 0):
        raise ValueError("passed status requires at least one pass and zero failures")
    if status == "failed" and failed == 0:
        raise ValueError("failed status requires at least one failure")
    if status == "skipped" and (passed != 0 or failed != 0 or skipped == 0):
        raise ValueError("skipped status requires skips and zero passes/failures")
    if status == "not_run" and (passed != 0 or failed != 0 or skipped != 0):
        raise ValueError("not_run status requires all counts to be zero")
    for label, value in (
        ("gate", gate),
        ("command", command),
        *(("evidence", item) for item in evidence),
    ):
        _check_text(label, value)
    if not gate.strip() or not command.strip():
        raise ValueError("gate and command must not be empty")

    root = root.resolve()
    artifacts_list = _artifact_records(root, artifacts)
    output_path = output if output.is_absolute() else root / output
    record: dict[str, object] = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "gate": gate,
        "status": status,
        "command": command,
        "source": {
            "git_head": _git(root, "rev-parse", "HEAD"),
            "git_dirty": bool(_git(root, "status", "--porcelain")),
            "pyproject_toml_sha256": _sha256(root / "pyproject.toml"),
            "uv_lock_sha256": _sha256(root / "uv.lock"),
        },
        "runtime": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "counts": {"passed": passed, "failed": failed, "skipped": skipped},
        "artifacts": artifacts_list,
        "evidence": list(evidence),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    needs_separator = output_path.exists() and output_path.stat().st_size > 0
    if needs_separator:
        with output_path.open("rb") as existing:
            existing.seek(-1, 2)
            needs_separator = existing.read(1) != b"\n"
    with output_path.open("a", encoding="utf-8", newline="\n") as stream:
        if needs_separator:
            stream.write("\n")
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", required=True, help="name of the verification gate")
    parser.add_argument("--status", required=True, choices=sorted(STATUSES))
    parser.add_argument(
        "--command", required=True, help="exact command text; recorded, never executed"
    )
    parser.add_argument("--passed", required=True, type=int)
    parser.add_argument("--failed", required=True, type=int)
    parser.add_argument("--skipped", required=True, type=int)
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="existing repository-relative artifact (repeatable)",
    )
    parser.add_argument(
        "--evidence", action="append", default=[], help="brief evidence metadata (repeatable)"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("docs/provider_verification/runs.jsonl")
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        record_run(
            root=args.root,
            output=args.output,
            gate=args.gate,
            status=args.status,
            command=args.command,
            passed=args.passed,
            failed=args.failed,
            skipped=args.skipped,
            artifacts=args.artifact,
            evidence=args.evidence,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
