"""Golden resolved-config snapshot for every shipped template and preset."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BASELINE = json.loads(
    (Path(__file__).parent / "data" / "config_dump_baseline.json").read_text(encoding="utf-8")
)


def _dump() -> dict:
    spec = importlib.util.spec_from_file_location(
        "dump_config_baseline", REPO / "scripts" / "dump_config_baseline.py"
    )
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return json.loads(json.dumps(mod.dump(), sort_keys=True, default=str))


@pytest.fixture(scope="module")
def current() -> dict:
    return _dump()


def test_baseline_covers_every_template_and_preset(current):
    assert set(BASELINE) == set(current)
    assert sum(k.startswith("template:") for k in BASELINE) >= 11
    assert sum(k.startswith("preset:") for k in BASELINE) >= 7


@pytest.mark.parametrize("key", sorted(BASELINE))
def test_resolved_config_unchanged(key, current):
    assert current[key] == BASELINE[key]
