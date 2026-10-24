"""Current durable/public schema contract and committed export checks.

This first-cycle base establishes a fresh contract; earlier git revisions are
not supported schema baselines. The committed export must match current model
schemas, and schema validation continues to reject unknown/future formats.
"""

from __future__ import annotations

import json
from pathlib import Path

from memspine.schemas.export import render, schema_version_of, strict_violations

REPO = Path(__file__).resolve().parents[3]
COMMITTED = REPO / "docs" / "v2" / "schemas"
STRICT_PREFIXES = ("durable/", "public/")


def _committed_strict() -> dict[str, str]:
    out: dict[str, str] = {}
    for tier in ("durable", "public"):
        for path in sorted((COMMITTED / tier).glob("*.json")):
            out[f"{tier}/{path.name}"] = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return out


def test_committed_strict_tiers_match_fresh_export_or_report_missing_bump():
    fresh = {k: v for k, v in render().items() if k.startswith(STRICT_PREFIXES)}
    problems = strict_violations(fresh, _committed_strict())
    assert not problems, (
        "\n".join(problems) + "\n(run: python -m memspine.schemas export docs/v2/schemas)"
    )


def test_every_strict_schema_records_its_version():
    committed = _committed_strict()
    assert committed
    for name, text in committed.items():
        data = json.loads(text)
        if name.startswith("public/"):
            assert data["x-semver"].count(".") == 2, name
        elif "x-kind" in data:
            assert isinstance(data["x-schema-version"], int) and data["x-schema-version"] >= 1, name
        else:
            assert "x-schema-version" not in data, f"{name}: value types carry no version"


# --- the checker itself ------------------------------------------------------------------------------------


def _baseline() -> dict[str, str]:
    return {k: v for k, v in render().items() if k.startswith(STRICT_PREFIXES)}


def _edit(text: str, **top: object) -> str:
    data = json.loads(text)
    data.update(top)
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_checker_flags_durable_change_without_bump():
    base = _baseline()
    fresh = dict(base)
    name = "durable/MemoryEvent.json"
    data = json.loads(base[name])
    data["properties"]["added_field"] = {"type": "string"}
    fresh[name] = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    problems = strict_violations(fresh, base)
    assert len(problems) == 1 and "without a version bump" in problems[0] and name in problems[0]


def test_checker_flags_public_change_without_semver_bump_and_accepts_bump_after_reexport():
    base = _baseline()
    name = "public/Receipt.json"
    changed = _edit(base[name], description="changed")
    problems = strict_violations({**base, name: changed}, base)
    assert len(problems) == 1 and "without a version bump" in problems[0]
    bumped = _edit(base[name], description="changed", **{"x-semver": "1.1.0"})
    stale = strict_violations({**base, name: bumped}, base)
    assert len(stale) == 1 and "stale" in stale[0]  # bumped, baseline not yet refreshed
    assert strict_violations({**base, name: bumped}, {**base, name: bumped}) == []


def test_checker_flags_value_type_change_and_added_or_removed_models():
    base = _baseline()
    part = "durable/Evidence.json"
    assert schema_version_of("durable", base[part]) is None
    problems = strict_violations({**base, part: _edit(base[part], description="x")}, base)
    assert len(problems) == 1 and "value type changed" in problems[0]
    gone = {k: v for k, v in base.items() if k != "public/CacheKey.json"}
    assert any("removed from the models" in p for p in strict_violations(gone, base))
    assert any("missing from the committed baseline" in p for p in strict_violations(base, gone))


def test_internal_tier_is_not_gated():
    base = _baseline()
    fresh = dict(base)
    fresh["internal/Whatever.json"] = "{}\n"
    assert strict_violations(fresh, base) == []
