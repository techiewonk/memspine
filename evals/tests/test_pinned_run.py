"""P0-02: dry-run and lock hashing of pinned_run.sh (no venv is ever built)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "pinned_run.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or shutil.which("git") is None, reason="needs bash and git")


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "evals").mkdir(parents=True)
    shutil.copy(SCRIPT, r / "evals" / "pinned_run.sh")
    (r / "uv.lock").write_text("lock-v1\n", newline="\n")
    (r / "pyproject.toml").write_text("[project]\nname='x'\n", newline="\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "one")
    return r


def _dry(repo: Path, **env: str) -> dict[str, str]:
    e = {**os.environ, "PINNED_DRY_RUN": "1", **env}
    if "PINNED_SHARED_VENV" not in env:
        e.pop("PINNED_SHARED_VENV", None)
    out = subprocess.run(
        [BASH, str(repo / "evals" / "pinned_run.sh"), "HEAD", "x.sh"],
        capture_output=True, text=True, env=e, check=True,
    ).stdout
    return dict(line.strip().split("=", 1) for line in out.splitlines() if "=" in line)


def test_dry_run_reports_hash_venv_and_build(repo: Path) -> None:
    d = _dry(repo)
    assert len(d["lock_hash"]) == 16 and d["action"] == "build"
    assert d["venv"].replace("\\", "/").endswith(f"memspine-venvs/{d['lock_hash']}")
    assert d["commit"] == _git(repo, "rev-parse", "HEAD")
    assert not (repo.parent / "memspine-venvs").exists()  # a dry run touches nothing


def test_hash_follows_the_lock_and_extras_not_the_commit(repo: Path) -> None:
    h1 = _dry(repo)["lock_hash"]
    (repo / "README").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")
    assert _dry(repo)["lock_hash"] == h1  # same lock and pyproject: same venv reused
    (repo / "uv.lock").write_text("lock-v2\n", newline="\n")
    _git(repo, "commit", "-qam", "three")
    assert _dry(repo)["lock_hash"] != h1
    assert _dry(repo, PINNED_EXTRAS="--extra dev")["lock_hash"] != _dry(repo)["lock_hash"]


def test_ready_venv_is_reused_and_shared_escape_hatch(repo: Path) -> None:
    d = _dry(repo)
    venv = repo.parent / "memspine-venvs" / d["lock_hash"]  # d["venv"] is a bash-style path on Windows
    venv.mkdir(parents=True)
    (venv / ".ready").write_text("x")
    assert _dry(repo)["action"] == "reuse"
    s = _dry(repo, PINNED_SHARED_VENV="1")
    assert s["action"] == "shared" and s["venv"].replace("\\", "/").endswith("repo/.venv")
