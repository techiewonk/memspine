"""Export determinism, committed-export gate, step coverage, import boundary."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import memspine.schemas as schemas_pkg
from memspine.schemas.export import collect_models, export, render
from memspine.schemas.steps import insert, offline, retrieve

REPO = Path(__file__).resolve().parents[3]
COMMITTED = REPO / "docs" / "v2" / "schemas"
PIPELINES = REPO / "docs" / "v2" / "pipelines"
PKG_DIR = Path(schemas_pkg.__file__).parent


def _read_tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_export_is_deterministic(tmp_path):
    export(tmp_path / "a")
    export(tmp_path / "b")
    first, second = _read_tree(tmp_path / "a"), _read_tree(tmp_path / "b")
    assert first == second
    assert all(text.endswith(b"\n") for text in first.values())
    sample = json.loads(first["index.json"])
    assert sample["models"], "index lists models"


def test_export_cli(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "memspine.schemas", "export", str(tmp_path / "cli")],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**__import__("os").environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert proc.returncode == 0, proc.stderr
    assert _read_tree(tmp_path / "cli") == {k: v.encode() for k, v in render().items()}


def test_committed_internal_export_matches_fresh_export():
    """Export reproducible in CI: docs/v2/schemas/internal must equal a fresh export."""
    fresh = {k: v.encode() for k, v in render().items() if k.startswith("internal/")}
    committed = {k: v for k, v in _read_tree(COMMITTED).items() if k.startswith("internal/")}
    assert committed == fresh, "run: python -m memspine.schemas export docs/v2/schemas"


def test_committed_index_and_tier_dirs():
    tree = _read_tree(COMMITTED)
    assert tree["index.json"] == render()["index.json"].encode()
    for tier in ("durable", "public"):
        assert f"{tier}/README.md" in tree


def test_model_names_unique_and_tiers_correct():
    models = collect_models()
    assert len(models["internal"]) > 100
    for tier, classes in models.items():
        for cls in classes:
            assert cls.__tier__ == tier
            assert cls.model_config["extra"] == "allow"


# --- every spec step has an Input and an Output model ------------------------------------------------------

_STEP_RE = re.compile(r"^### (\d+)\. ", re.M)
_SPECS = [
    ("insert.md", "InsertStep", insert),
    ("offline.md", "OfflineStep", offline),
    ("retrieve.md", "RetrieveStep", retrieve),
]
_EXPECTED_STEPS = {"insert.md": 23, "offline.md": 22, "retrieve.md": 20}


@pytest.mark.parametrize(("spec", "prefix", "module"), _SPECS)
def test_every_spec_step_has_input_and_output(spec, prefix, module):
    text = (PIPELINES / spec).read_text(encoding="utf-8")
    steps = [int(n) for n in _STEP_RE.findall(text)]
    assert len(steps) == _EXPECTED_STEPS[spec]
    assert steps == list(range(1, len(steps) + 1))
    missing = []
    for n in steps:
        for suffix in ("Input", "Output"):
            name = f"{prefix}{n:02d}{suffix}"
            if not hasattr(module, name):
                missing.append(name)
    assert not missing, missing


@pytest.mark.parametrize("module", [insert, offline, retrieve])
def test_no_orphan_step_models(module):
    """Every ``<Pipeline>StepNN*`` class in a module matches a real step number."""
    numbers = {
        int(m.group(1))
        for name in dir(module)
        if (m := re.fullmatch(r"\w+Step(\d\d)(Input|Output)", name))
    }
    assert numbers == set(range(1, max(numbers) + 1))


# --- import boundary: schemas is a pure leaf ---------------------------------------------------------------


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append("memspine.schemas")  # relative import stays inside the package
            elif node.module:
                found.append(node.module)
    return found


def test_schemas_imports_no_other_memspine_module():
    offenders = []
    files = sorted(PKG_DIR.rglob("*.py"))
    assert len(files) >= 10
    for path in files:
        for mod in _imported_modules(path):
            top = mod.split(".")
            if top[0] == "memspine" and not (len(top) >= 2 and top[1] == "schemas"):
                offenders.append((path.relative_to(PKG_DIR).as_posix(), mod))
    assert not offenders, offenders
