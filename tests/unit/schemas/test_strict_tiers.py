# ruff: noqa: E501
"""Strict-tier CI gate (S1-4): durable and public schemas change only with a version bump.

``docs/v2/schemas/{durable,public}/`` is the committed baseline. Each exported schema records its version
(``x-schema-version`` for durable kinds, ``x-semver`` for public models). Two checks:

1. the working tree: a fresh export must equal the committed baseline; a difference without a version change
   is reported as a missing bump;
2. history (skipped without git): the committed baseline in the working tree is compared with the same files
   at ``$MEMSPINE_SCHEMA_BASE_REF`` (default ``HEAD``); a changed file whose version did not change fails, so
   re-running the export after an unversioned edit cannot hide it. In CI set the variable to the merge base.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

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


# --- history check (git) -----------------------------------------------------------------------------------


def _git_show(ref: str, rel: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel}"], capture_output=True, cwd=REPO, text=False, check=False
    )
    return proc.stdout.decode("utf-8").replace("\r\n", "\n") if proc.returncode == 0 else None


def test_working_baseline_changes_vs_git_ref_are_versioned():
    ref = os.environ.get("MEMSPINE_SCHEMA_BASE_REF", "HEAD")
    try:
        ok = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            cwd=REPO,
            check=False,
        ).returncode
    except FileNotFoundError:
        pytest.skip("git not available")
    if ok != 0:
        pytest.skip(f"git ref {ref!r} not resolvable")
    working = _committed_strict()
    base = {}
    for name in working:
        old = _git_show(ref, f"docs/v2/schemas/{name}")
        if old is not None:  # files new since the ref are additions, not changes
            base[name] = old
    problems = [
        p
        for p in strict_violations({k: v for k, v in working.items() if k in base}, base)
        if "without a version bump" in p
    ]
    assert not problems, "\n".join(problems)
