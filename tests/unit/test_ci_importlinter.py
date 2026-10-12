"""Tests for scripts/ci_importlinter.py (X-02): the contract filter."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "ci_importlinter", REPO / "scripts" / "ci_importlinter.py"
    )
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CONFIG = """\
[importlinter]
root_package = memspine

[importlinter:contract:layers]
name = l
type = layers
layers =
    memspine.api
    memspine.runtime
    memspine.read : memspine.derive
    memspine.schemas

[importlinter:contract:forbid]
name = f
type = forbidden
source_modules =
    memspine.read
    memspine.derive
forbidden_modules =
    memspine.api
allow_indirect_imports = True

[importlinter:contract:only-missing]
name = m
type = forbidden
source_modules =
    memspine.ghost
forbidden_modules =
    memspine.schemas
"""


def _tree(tmp_path: Path, existing: list[str]) -> tuple[Path, Path]:
    src = tmp_path / "src"
    for m in existing:
        pkg = src.joinpath(*m.split("."))
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
    cfg = tmp_path / ".importlinter"
    cfg.write_text(CONFIG, encoding="utf-8")
    return cfg, src


def test_missing_modules_are_pruned(tmp_path: Path):
    mod = _load()
    cfg, src = _tree(tmp_path, ["memspine", "memspine.read", "memspine.schemas"])
    out = mod.filter_config(cfg, src)
    assert mod.kept_ids(out) == ["layers"]
    layers = [
        ln.strip() for ln in out["importlinter:contract:layers"]["layers"].split("\n") if ln.strip()
    ]
    assert layers == ["memspine.read", "memspine.schemas"]
    assert "importlinter:contract:only-missing" not in out.sections()


def test_new_package_is_picked_up_automatically(tmp_path: Path):
    mod = _load()
    cfg, src = _tree(tmp_path, ["memspine", "memspine.read", "memspine.schemas"])
    before = mod.filter_config(cfg, src)
    assert "importlinter:contract:forbid" not in before.sections()  # api missing: nothing to forbid
    _tree(tmp_path, ["memspine", "memspine.read", "memspine.schemas", "memspine.api"])
    after = mod.filter_config(cfg, src)
    assert "importlinter:contract:forbid" in after.sections()
    layers = after["importlinter:contract:layers"]["layers"]
    assert "memspine.api" in layers


def test_sibling_separator_kept(tmp_path: Path):
    mod = _load()
    cfg, src = _tree(tmp_path, ["memspine", "memspine.read", "memspine.derive", "memspine.schemas"])
    out = mod.filter_config(cfg, src)
    assert "memspine.read : memspine.derive" in out["importlinter:contract:layers"]["layers"]


def test_real_config_filters_to_existing_modules():
    mod = _load()
    out = mod.filter_config()
    assert out["importlinter"]["root_package"] == "memspine"
    for sec in out.sections():
        if not sec.startswith(mod.PREFIX):
            continue
        for key in ("layers", "source_modules", "forbidden_modules"):
            if key in out[sec]:
                for line in out[sec][key].splitlines():
                    for m in line.replace("|", ":").split(":"):
                        if m.strip():
                            assert mod.module_exists(m.strip()), (sec, m)
