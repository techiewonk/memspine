"""Drift guard for the documented config surface (Phase 15, REL-01).

The ``## Config-key reference`` table in ``docs/USAGE.md`` must document **exactly**
the fields of :class:`~memspine.config.schema.MemspineConfig` and its sub-models —
no undocumented field, no documented-but-nonexistent key. This makes "the docs
describe the shipped config" a standing structural guarantee instead of a one-time
review: add a config key without a table row (or leave a stale row behind) and this
test fails.

The expected set is **introspected from the pydantic schema** (never hand-listed
here), so the test itself cannot drift from the code. The doc table is parsed from a
marker-delimited region, so surrounding prose can change freely without breaking it.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from pydantic import BaseModel

from memspine.config.schema import MemspineConfig

#: Placeholder for an open map keyed by name (an LLM role, memory type, or
#: namespace). ``llm.roles.<role>.model`` is documented as ``llm.roles.*.model``.
_WILDCARD = "*"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_USAGE = _REPO_ROOT / "docs" / "USAGE.md"

_TABLE_START = "<!-- CONFIG-KEYS-TABLE:START -->"
_TABLE_END = "<!-- CONFIG-KEYS-TABLE:END -->"

#: A dotted config key path: lowercase segments (or ``*``), dot-separated.
_KEY_RE = re.compile(r"^[a-z_]+(?:\.(?:[a-z_]+|\*))*$")


def _is_model(annotation: object) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _dict_value_type(annotation: object) -> object | None:
    """Return the value type of a ``dict[str, V]`` annotation, else ``None``."""
    if typing.get_origin(annotation) is dict:
        args = typing.get_args(annotation)
        if len(args) == 2:
            return args[1]
    return None


def _walk(model: type[BaseModel], prefix: str, out: set[str]) -> None:
    """Collect every leaf config key path of ``model`` into ``out``.

    Sub-models recurse (``storage`` -> ``storage.backend`` …); ``dict[str, SubModel]``
    fields recurse under a ``*`` wildcard segment (``llm.roles`` ->
    ``llm.roles.*.model`` …); scalars and open dicts (``read.scoring``,
    ``*.policies``, ``prompts.overrides``) are leaves documented by their own key.
    """
    for name, field in model.model_fields.items():
        annotation = field.annotation
        key = f"{prefix}{name}"
        if _is_model(annotation):
            _walk(annotation, key + ".", out)
            continue
        value_type = _dict_value_type(annotation)
        if value_type is not None and _is_model(value_type):
            _walk(value_type, f"{key}.{_WILDCARD}.", out)
            continue
        out.add(key)


def _schema_keys() -> set[str]:
    keys: set[str] = set()
    _walk(MemspineConfig, "", keys)
    return keys


def _documented_keys() -> set[str]:
    text = _USAGE.read_text(encoding="utf-8")
    assert _TABLE_START in text and _TABLE_END in text, (
        f"config-key table markers not found in {_USAGE} — expected "
        f"{_TABLE_START!r} … {_TABLE_END!r}"
    )
    region = text.split(_TABLE_START, 1)[1].split(_TABLE_END, 1)[0]
    keys: set[str] = set()
    for line in region.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1].strip()
        # First column is a single backticked key, e.g. ``| `storage.backend` | … |``.
        match = re.search(r"`([^`]+)`", first_cell)
        if match is None:
            continue  # header ("Key") / separator ("---") rows carry no backticks
        candidate = match.group(1)
        if _KEY_RE.match(candidate):
            keys.add(candidate)
    return keys


def test_documented_config_keys_match_schema_exactly() -> None:
    schema = _schema_keys()
    documented = _documented_keys()

    # Sanity: the schema introspection found the real tree (guards a refactor that
    # silently empties model_fields), and the doc parser found the real table.
    assert len(schema) >= 40, f"schema introspection collected too few keys: {sorted(schema)}"
    assert len(documented) >= 40, f"doc table parsed too few keys: {sorted(documented)}"

    undocumented = schema - documented
    nonexistent = documented - schema
    assert not undocumented, (
        "MemspineConfig fields missing from the docs/USAGE.md config-key table "
        f"(add a row): {sorted(undocumented)}"
    )
    assert not nonexistent, (
        "docs/USAGE.md config-key table documents keys that do not exist in "
        f"MemspineConfig (remove/fix the row): {sorted(nonexistent)}"
    )
    assert schema == documented
