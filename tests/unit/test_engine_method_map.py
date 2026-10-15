"""Coverage gate for scripts/engine_method_map.py (P0-04).

Every class, method and module-level function of ``engine.py`` must be mapped to a
target module. Staleness of the committed ``docs/v2/data/engine_method_map.*`` is NOT
tested here (parallel move PRs would conflict); regenerate with
``python scripts/engine_method_map.py`` when you move code, and CI may run ``--check``.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "engine_method_map", REPO / "scripts" / "engine_method_map.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_engine_unit_is_mapped():
    mod = _load()
    data = mod.build()
    tree = ast.parse(mod.ENGINE.read_text(encoding="utf-8"))
    expected = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            expected.add(node.name)
        elif isinstance(node, ast.ClassDef):
            expected.add(node.name)
            expected |= {
                f"{node.name}.{n.name}"
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    mapped = {u["name"]: u for u in data["units_detail"]}
    assert expected == set(mapped)
    for u in mapped.values():
        assert u["module"] and u["package"] and u["work_package"], u["name"]
        assert u["confidence"] in {"rule", "manual", "heuristic"}, u["name"]
        assert u["end"] >= u["start"]
    assert all("/" in u["module"] and u["module"].endswith(".py") for u in mapped.values())
