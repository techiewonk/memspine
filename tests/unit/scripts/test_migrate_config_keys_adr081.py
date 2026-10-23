"""scripts/migrate_config_keys_adr081.py: an old ``graph.mirror`` is reported as removed.

The ADR-081 amendment of 2026-10-23 removed the ``storage.graph_mirror`` slot (one graph client
runs at a time), so the codemod no longer emits it: an enabled mirror is dropped and listed as a
``manual:`` note naming ``storage.graph``; a disabled one is dropped silently.

migrate_config_keys_adr081: skip  (this file spells the old keys on purpose)
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import yaml

REPO = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migrate_config_keys_adr081", REPO / "scripts" / "migrate_config_keys_adr081.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def _removed(notes: list[str]) -> bool:
    return any("graph.mirror was removed" in n and "storage.graph" in n for n in notes)


def test_python_enabled_mirror_is_dropped_and_reported(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        textwrap.dedent(
            """\
            cfg = dict(
                graph={"provider": "ladybug", "mirror": {"enabled": True, "provider": "neo4j"}},
            )
            """
        ),
        encoding="utf-8",
    )
    out, notes = mod.rewrite_python(path)
    assert "graph_mirror" not in out and "mirror" not in out
    assert '"graph": {"provider": "ladybug"}' in out
    assert _removed(notes)


def test_python_disabled_mirror_is_dropped_silently(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(
        'cfg = {"graph": {"provider": "auto", "mirror": {"enabled": False}}}\n', encoding="utf-8"
    )
    out, notes = mod.rewrite_python(path)
    assert "mirror" not in out
    assert not _removed(notes)


def test_python_mirror_attribute_path_is_reported_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text("enabled = config.graph.mirror.enabled\n", encoding="utf-8")
    out, notes = mod.rewrite_python(path)
    assert "graph_mirror" not in out
    assert _removed(notes)


def test_mapping_enabled_mirror_is_dropped_and_reported() -> None:
    data = {"graph": {"mirror": {"enabled": True, "settings": {"database": "m"}}}}
    notes: list[str] = []
    assert mod.migrate_mapping(data, notes)
    assert data == {}
    assert _removed(notes)


def test_yaml_and_json_enabled_mirror_is_reported(tmp_path: Path) -> None:
    config = {"graph": {"provider": "neo4j", "mirror": {"enabled": True}}}
    yml = tmp_path / "c.yaml"
    yml.write_text(yaml.safe_dump(config), encoding="utf-8")
    out, notes = mod.rewrite_yaml(yml)
    assert out is not None and "mirror" not in out
    assert yaml.safe_load(out) == {"storage": {"graph": {"provider": "neo4j"}}}
    assert _removed(notes)
    js = tmp_path / "c.json"
    js.write_text(json.dumps(config), encoding="utf-8")
    out, notes = mod.rewrite_json(js)
    assert out is not None and json.loads(out) == {"storage": {"graph": {"provider": "neo4j"}}}
    assert _removed(notes)
