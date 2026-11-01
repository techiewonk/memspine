"""Append a portable, manually supplied, revision-bound verification record to JSONL.

Every record carries a ``revision`` block (commit, tree, branch, dirty) and an ``env`` block
(Python, ``uv.lock`` hash, extras), ADR-087. A dirty working tree is refused unless
``--allow-dirty`` is passed; such a record is marked ``diagnostic_only``.
"""

from __future__ import annotations

import argparse
import contextlib
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
SCHEMA_VERSION = 2


class DirtyTreeError(ValueError):
    """The working tree has uncommitted or untracked changes (ADR-087)."""


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


def _porcelain_path(line: str) -> str:
    """The (destination) path of one ``git status --porcelain`` line; tolerant of the
    leading status column having been stripped from the first line."""
    path = line.strip().split(None, 1)[-1]
    return path.split(" -> ")[-1].strip('"')


def revision_info(root: Path, ledger: Path | None = None) -> dict[str, object]:
    """Commit SHA, tree hash, branch (``None`` when detached) and dirty state of ``root``.

    ``dirty`` is any output from ``git status --porcelain``: modified, staged and untracked
    files all count. The only exclusion is ``ledger``, the evidence file this recorder
    appends to, so the chunks of one run can be recorded on the same clean commit.
    """
    ignored = None
    if ledger is not None:
        resolved = (ledger if ledger.is_absolute() else root / ledger).resolve()
        with contextlib.suppress(ValueError):
            ignored = resolved.relative_to(root.resolve()).as_posix()
    changes = [
        line
        for line in _git(root, "status", "--porcelain", "--untracked-files=all").splitlines()
        if line.strip() and _porcelain_path(line) != ignored
    ]
    branch: str | None = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "commit": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "branch": None if branch == "HEAD" else branch,
        "dirty": bool(changes),
    }


def _normalise_extras(extras: Sequence[str] | None) -> list[str] | None:
    if extras is None:
        return None
    names = {part.strip() for item in extras for part in item.split(",")}
    return sorted(name for name in names if name)


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
    extras: Sequence[str] | None = None,
    allow_dirty: bool = False,
) -> dict[str, object]:
    """Validate explicit evidence and append one record; never run ``command``.

    Raises :class:`DirtyTreeError` when the tree is dirty and ``allow_dirty`` is false.
    ``extras`` ``None`` records that the installed extras were not stated.
    """
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
    revision = revision_info(root, output)
    if revision["dirty"] and not allow_dirty:
        raise DirtyTreeError(
            "working tree is dirty (git status --porcelain is not empty); commit or remove "
            "the changes, or pass --allow-dirty to write a diagnostic-only record"
        )
    artifacts_list = _artifact_records(root, artifacts)
    output_path = output if output.is_absolute() else root / output
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
        "gate": gate,
        "status": status,
        "command": command,
        "diagnostic_only": bool(revision["dirty"]),
        "revision": revision,
        "env": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "uv_lock_sha256": _sha256(root / "uv.lock"),
            "pyproject_toml_sha256": _sha256(root / "pyproject.toml"),
            "extras": _normalise_extras(extras),
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
        "--extras",
        action="append",
        default=None,
        help="installed optional extras, comma-separated (repeatable); omitted = not stated",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="record from a dirty tree; the record is marked diagnostic_only",
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
            extras=args.extras,
            allow_dirty=args.allow_dirty,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
