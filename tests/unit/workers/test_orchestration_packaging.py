"""Breaking orchestration packaging and optional-import guarantees."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_durable_extra_contains_both_frameworks_and_no_removed_dependency() -> None:
    root = Path(__file__).resolve().parents[3]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "dbos" not in extras
    assert any(requirement.startswith("temporalio") for requirement in extras["temporal"])
    assert any(requirement.startswith("resonate-sdk") for requirement in extras["temporal"])
    assert not any(
        "dbos" in requirement.lower()
        for requirements in extras.values()
        for requirement in requirements
    )
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    assert all(package["name"] != "dbos" for package in lock["package"])
    assert not (root / "src/memspine/workers/dbos_runner.py").exists()


def test_inline_import_and_lifecycle_do_not_import_optional_orchestration_sdks() -> None:
    root = Path(__file__).resolve().parents[3]
    code = """
import asyncio
import importlib.abc
import sys
class BlockOrchestration(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'temporalio', 'resonate', 'dbos'}:
            raise AssertionError('Optional framework imported by inline path: ' + fullname)
sys.meta_path.insert(0, BlockOrchestration())
import memspine
assert 'memspine.api.engine' not in sys.modules
from memspine import Engine
from memspine.workers.inline import InlineRunner
async def verify():
    runner = InlineRunner()
    await runner.start()
    await runner.close()
asyncio.run(verify())
"""
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_selected_temporal_runner_names_extra_when_sdk_import_is_unavailable() -> None:
    root = Path(__file__).resolve().parents[3]
    code = """
import asyncio
import importlib.abc
import sys
import tempfile
from pathlib import Path
class MissingOrchestration(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'temporalio', 'resonate'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, MissingOrchestration())
from memspine.exceptions import MissingServiceError
from memspine.workers.temporal_runner import TemporalRunner
async def verify(path):
    runner = TemporalRunner(address='localhost:7233', namespace='default',
        task_queue='test', deployment_id='test', journal_path=path,
        context_factory=lambda: None)
    async def probe(ctx):
        return {'status': 'ok'}
    runner.register('probe', probe)
    try:
        await runner.start()
    except MissingServiceError as exc:
        assert exc.extra == 'temporal'
    else:
        raise AssertionError('Selected runner silently bypassed missing SDK')
    finally:
        await runner.close()
    assert not Path(path).exists()
with tempfile.TemporaryDirectory() as directory:
    asyncio.run(verify(str(Path(directory) / 'steps.sqlite')))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
