# ruff: noqa: E501
"""TI-8: the stage -> parameter catalogue (docs/v2/data/stage_params.json) stays complete and current.

* every config key of the schema is attributed to a stage or listed in ``unattributed`` (with a reason);
* every stage id of the pipeline inventory has an entry;
* the committed JSON and Markdown are what ``scripts/build_stage_params.py`` generates now
  (the same check as ``python scripts/build_stage_params.py --check``).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JSON = _ROOT / "docs" / "v2" / "data" / "stage_params.json"
_MD = _ROOT / "docs" / "v2" / "STAGE_PARAMS.md"
_INVENTORY = _ROOT / "docs" / "v2" / "PIPELINE_STAGE_INVENTORY.md"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location(
        "build_stage_params", _ROOT / "scripts" / "build_stage_params.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(_JSON.read_text(encoding="utf-8"))


def test_every_schema_key_is_attributed_or_listed(gen, data) -> None:
    schema = set(gen.schema_keys())
    attributed = {
        k["key"]
        for st in data["stages"].values()
        for k in st["keys"]
        if not k["key"].startswith("cli:")
    }
    unattributed = set(data["unattributed"])
    assert len(schema) >= 40
    assert not schema - attributed - unattributed, sorted(schema - attributed - unattributed)
    assert not (attributed | unattributed) - schema, "stale keys: " + str(
        sorted((attributed | unattributed) - schema)
    )
    assert not attributed & unattributed, sorted(attributed & unattributed)
    assert data["unattributed_count"] == len(unattributed)
    assert all(reason.strip() for reason in data["unattributed"].values())
    # an explicit list, not a dumping ground: most keys land in a stage
    assert len(unattributed) < 0.25 * len(schema)


def test_every_inventory_stage_has_an_entry(data) -> None:
    ids = set(re.findall(r"^\| ([ABC]\d+[a-z]?) \|", _INVENTORY.read_text(encoding="utf-8"), re.M))
    assert len(ids) >= 80
    assert not ids - set(data["stages"]), sorted(ids - set(data["stages"]))


def test_entries_carry_the_contract_fields(data) -> None:
    for sid, st in data["stages"].items():
        assert {"name", "file_function", "keys", "prompts", "models"} <= set(st), sid
        for k in st["keys"]:
            assert {
                "key",
                "default",
                "type",
                "description",
                "effect",
                "rebuild",
                "screen_ids",
            } <= set(k), k
            assert isinstance(k["rebuild"], bool)
            assert isinstance(k["screen_ids"], list)
        for p in st["prompts"]:
            assert {"prompt_id", "role", "file", "version"} <= set(p), p


def test_generated_files_are_up_to_date(gen, data) -> None:
    fresh = gen.build()
    assert (
        _JSON.read_text(encoding="utf-8") == json.dumps(fresh, indent=1, ensure_ascii=False) + "\n"
    ), "docs/v2/data/stage_params.json is stale: run python scripts/build_stage_params.py"
    assert _MD.read_text(encoding="utf-8") == gen.render_md(fresh), (
        "docs/v2/STAGE_PARAMS.md is stale: run python scripts/build_stage_params.py"
    )


def test_no_dataset_text_in_the_catalogue(data) -> None:
    """Harness prompts are recorded by id and content hash only."""
    for st in data["stages"].values():
        for p in st["prompts"]:
            assert len(p["version"]) <= 24, p
