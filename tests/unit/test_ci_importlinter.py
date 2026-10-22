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
                        m = m.strip()
                        if m and (m == "memspine" or m.startswith("memspine.")):
                            assert mod.module_exists(m), (sec, m)


def test_external_modules_are_kept(tmp_path: Path):
    mod = _load()
    cfg, src = _tree(tmp_path, ["memspine", "memspine.read"])
    cfg.write_text(
        "[importlinter]\nroot_package = memspine\ninclude_external_packages = True\n\n"
        "[importlinter:contract:ext]\nname = e\ntype = forbidden\n"
        "source_modules =\n    memspine.read\nforbidden_modules =\n    redis\n    httpx\n",
        encoding="utf-8",
    )
    out = mod.filter_config(cfg, src, baseline=None)
    assert mod.kept_ids(out) == ["ext"]
    assert "redis" in out["importlinter:contract:ext"]["forbidden_modules"]


def test_baseline_becomes_ignore_imports(tmp_path: Path):
    mod = _load()
    cfg, src = _tree(tmp_path, ["memspine", "memspine.read", "memspine.derive", "memspine.api"])
    base = tmp_path / "baseline.txt"
    base.write_text(
        "# comment\n"
        "forbid memspine/read/x/__init__.py memspine.api -- RM-113 test\n"
        "forbid memspine/derive/y.py redis -- RM-113 test\n",
        encoding="utf-8",
    )
    out = mod.filter_config(cfg, src, baseline=base)
    sec = out["importlinter:contract:forbid"]
    ignores = [ln.strip() for ln in sec["ignore_imports"].splitlines() if ln.strip()]
    assert ignores == [
        "memspine.read.x -> memspine.api",
        "memspine.read.x -> memspine.api.**",
        "memspine.derive.y -> redis",
    ]
    assert sec["unmatched_ignore_imports_alerting"] == "none"
    assert "ignore_imports" not in out["importlinter:contract:layers"]


def test_baseline_rejects_entries_without_reason(tmp_path: Path):
    import pytest

    mod = _load()
    base = tmp_path / "baseline.txt"
    base.write_text("forbid memspine/read/x.py memspine.api\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.load_baseline(base)
