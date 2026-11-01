"""Tests for the portable verification ledger writer."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from scripts.record_verification_run import DirtyTreeError, main, record_run, revision_info

_GIT_IDENTITY = (
    "-c",
    "user.name=Ledger Test",
    "-c",
    "user.email=ledger@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *_GIT_IDENTITY, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(root: Path, message: str = "evidence") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _root(path: Path) -> Path:
    """A real, clean Git repository with the files the recorder hashes."""
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "pyproject.toml").write_text("project = {}\n", encoding="utf-8")
    (path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit_all(path, "init")
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


def test_appends_without_replacing_existing_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")
    output = root / "runs.jsonl"
    prior = '{"older":"evidence"}\n'
    output.write_text(prior, encoding="utf-8")
    _commit_all(root)

    _record(root, output)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == prior.rstrip("\n")
    assert json.loads(lines[1])["schema_version"] == 2


def test_clean_record_carries_revision_and_env(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")

    record = _record(root, root / "runs.jsonl", extras=["ner,graph", "dev", "graph"])

    assert record["revision"] == {
        "commit": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "branch": "main",
        "dirty": False,
    }
    assert record["diagnostic_only"] is False
    env = record["env"]
    assert isinstance(env, dict)
    assert env["uv_lock_sha256"] == hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    assert env["extras"] == ["dev", "graph", "ner"]
    assert isinstance(env["python"], str) and env["python"].count(".") == 2


def test_extras_not_stated_is_recorded_as_null(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")
    env = _record(root, root / "runs.jsonl")["env"]
    assert isinstance(env, dict) and env["extras"] is None


def test_detached_head_records_no_branch(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")
    _git(root, "checkout", "-q", "--detach")
    assert revision_info(root)["branch"] is None


@pytest.mark.parametrize("change", ["modified", "staged", "untracked"])
def test_dirty_tree_is_refused(tmp_path: Path, change: str) -> None:
    root = _root(tmp_path / "repo")
    if change == "untracked":
        (root / "new.txt").write_text("x\n", encoding="utf-8")
    else:
        (root / "uv.lock").write_text("version = 2\n", encoding="utf-8")
        if change == "staged":
            _git(root, "add", "uv.lock")
    output = tmp_path / "runs.jsonl"  # outside the repository: the refusal is not self-caused

    assert revision_info(root)["dirty"] is True
    with pytest.raises(DirtyTreeError, match="--allow-dirty"):
        _record(root, output)
    assert not output.exists()


def test_allow_dirty_marks_the_record_diagnostic_only(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")
    (root / "scratch.txt").write_text("x\n", encoding="utf-8")

    record = _record(root, tmp_path / "runs.jsonl", allow_dirty=True)

    revision = record["revision"]
    assert isinstance(revision, dict) and revision["dirty"] is True
    assert record["diagnostic_only"] is True


def test_cli_exits_non_zero_on_a_dirty_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path / "repo")
    (root / "scratch.txt").write_text("x\n", encoding="utf-8")
    output = tmp_path / "runs.jsonl"
    argv = [
        "--gate=unit",
        "--status=passed",
        "--command=pytest",
        "--passed=1",
        "--failed=0",
        "--skipped=0",
        f"--root={root}",
        f"--output={output}",
    ]

    with pytest.raises(SystemExit) as refused:
        main(argv)
    assert refused.value.code != 0
    assert "dirty" in capsys.readouterr().err
    assert not output.exists()

    assert main([*argv, "--allow-dirty", "--extras=dev"]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["diagnostic_only"] is True and written["env"]["extras"] == ["dev"]


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


def test_records_artifact_path_and_sha256(tmp_path: Path) -> None:
    root = _root(tmp_path / "repo")
    artifact = root / "evidence.log"
    artifact.write_text("provider gate passed\n", encoding="utf-8")
    _commit_all(root)

    record = _record(root, root / "runs.jsonl", artifacts=["evidence.log"])

    assert record["artifacts"] == [
        {"path": "evidence.log", "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    ]


def test_records_only_explicit_fields_and_refuses_clear_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path / "repo")
    output = tmp_path / "runs.jsonl"
    monkeypatch.setenv("VERIFICATION_LEDGER_TEST_SECRET", "must-not-be-captured")

    _record(root, output, command="echo exact command")
    content = output.read_text(encoding="utf-8")
    record = json.loads(content)
    assert record["command"] == "echo exact command"
    assert "VERIFICATION_LEDGER_TEST_SECRET" not in content
    assert "must-not-be-captured" not in content

    with pytest.raises(ValueError, match="credentials"):
        _record(root, output, evidence=["token=obviously-secret"])


def test_the_ledger_file_itself_does_not_make_the_tree_dirty(tmp_path: Path) -> None:
    """Chunks of one run append to the same ledger on one clean commit (ADR-087)."""
    import subprocess

    from scripts.record_verification_run import revision_info

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "init")
    ledger = tmp_path / "docs" / "runs.jsonl"
    ledger.parent.mkdir()
    ledger.write_text("{}\n", encoding="utf-8")
    assert revision_info(tmp_path, ledger)["dirty"] is False
    assert revision_info(tmp_path)["dirty"] is True
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    assert revision_info(tmp_path, ledger)["dirty"] is True
