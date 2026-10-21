"""Tests for the portable verification ledger writer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from scripts.record_verification_run import record_run


def _root(path: Path) -> Path:
    path.mkdir()
    (path / "pyproject.toml").write_text("project = {}\n", encoding="utf-8")
    (path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return path


def _record(root: Path, output: Path, **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "root": root,
        "output": output,
        "gate": "unit",
        "status": "passed",
        "command": "python -m pytest tests/unit -q",
        "passed": 3,
        "failed": 0,
        "skipped": 1,
    }
    values.update(changes)
    return record_run(**values)  # type: ignore[arg-type]


def test_appends_without_replacing_existing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path / "repo")
    output = root / "runs.jsonl"
    prior = '{"older":"evidence"}\n'
    output.write_text(prior, encoding="utf-8")
    monkeypatch.setattr("scripts.record_verification_run._git", lambda *_args: "abc123")

    _record(root, output)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == prior.rstrip("\n")
    assert json.loads(lines[1])["schema_version"] == 1


@pytest.mark.parametrize("status", ["unknown", "PASS", ""])
def test_rejects_invalid_status(tmp_path: Path, status: str) -> None:
    root = _root(tmp_path / "repo")
    with pytest.raises(ValueError, match="status"):
        _record(root, root / "runs.jsonl", status=status)


@pytest.mark.parametrize("field,value", [("passed", -1), ("failed", 1.2), ("skipped", True)])
def test_rejects_invalid_counts(tmp_path: Path, field: str, value: object) -> None:
    root = _root(tmp_path / "repo")
    with pytest.raises(ValueError, match="count"):
        _record(root, root / "runs.jsonl", **{field: value})


@pytest.mark.parametrize(
    ("status", "passed", "failed", "skipped"),
    [
        ("passed", 3, 1, 0),
        ("passed", 0, 0, 0),
        ("failed", 2, 0, 0),
        ("skipped", 1, 0, 1),
        ("skipped", 0, 0, 0),
        ("not_run", 0, 0, 1),
    ],
)
def test_rejects_inconsistent_status_counts(
    tmp_path: Path, status: str, passed: int, failed: int, skipped: int
) -> None:
    root = _root(tmp_path / "repo")
    with pytest.raises(ValueError, match="status|requires"):
        _record(
            root, root / "runs.jsonl", status=status, passed=passed, failed=failed, skipped=skipped
        )


def test_rejects_artifact_traversal_outside_repository(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("evidence", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes repository"):
        _record(root, root / "runs.jsonl", artifacts=["../outside.txt"])


def test_rejects_missing_artifact(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")
    with pytest.raises(ValueError, match="does not exist"):
        _record(root, root / "runs.jsonl", artifacts=["logs/missing.txt"])


def test_records_artifact_path_and_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path / "repo")
    artifact = root / "evidence.log"
    artifact.write_text("provider gate passed\n", encoding="utf-8")
    monkeypatch.setattr("scripts.record_verification_run._git", lambda *_args: "abc123")

    record = _record(root, root / "runs.jsonl", artifacts=["evidence.log"])

    assert record["artifacts"] == [
        {"path": "evidence.log", "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    ]


def test_records_only_explicit_fields_and_refuses_clear_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path / "repo")
    output = root / "runs.jsonl"
    monkeypatch.setattr("scripts.record_verification_run._git", lambda *_args: "abc123")
    monkeypatch.setenv("VERIFICATION_LEDGER_TEST_SECRET", "must-not-be-captured")

    _record(root, output, command="echo exact command")
    content = output.read_text(encoding="utf-8")
    record = json.loads(content)
    assert record["command"] == "echo exact command"
    assert "VERIFICATION_LEDGER_TEST_SECRET" not in content
    assert "must-not-be-captured" not in content

    with pytest.raises(ValueError, match="credentials"):
        _record(root, output, evidence=["token=obviously-secret"])
