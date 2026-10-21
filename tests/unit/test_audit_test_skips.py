"""Source-only skip audit coverage; no optional runtime imports."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "audit_test_skips", Path(__file__).resolve().parents[2] / "scripts/audit_test_skips.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)


def inventory(tmp_path: Path, source: str):
    (tmp_path / "tests").mkdir()
    (tmp_path / "evals/tests").mkdir(parents=True)
    (tmp_path / "tests/conftest.py").write_text(source)
    records, errors = _AUDIT.collect_callsites(tmp_path)
    assert not errors
    return records


def test_alias_fixture_bare_marker_and_xfail(tmp_path):
    records = inventory(
        tmp_path,
        """import pytest as pt
from pytest import importorskip as need
from unittest import skipIf
REASON = "optional extra absent"
@pt.fixture
def resource():
    pt.skip(REASON)
need("optional_sdk")
@pt.mark.skip
def test_bare(): pass
@pt.mark.xfail(reason="known failure")
def test_expected(): pass
@skipIf(True, "platform")
def test_platform(): pass
""",
    )
    assert [r["kind"] for r in records] == ["skip", "importorskip", "skip", "xfail", "skipif"]
    assert records[0]["context"] == "fixture"
    assert records[0]["reason_known_literal"] == "optional extra absent"
    assert records[2]["reason_status"] == "reason_not_supplied"
    assert records[3]["category"] == "expected_failure_not_skip"
    assert records[4]["reason_known_literal"] == "platform"


def test_evals_module_marker_and_no_execution(tmp_path):
    inventory(tmp_path, "raise RuntimeError('must not execute')")
    (tmp_path / "evals/tests/test_sample.py").write_text(
        "import pytest\npytestmark = pytest.mark.skipif(not READY, reason=f'dynamic {READY}')\n"
    )
    records, errors = _AUDIT.collect_callsites(tmp_path)
    assert not errors
    assert records[0]["file"] == "evals/tests/test_sample.py"
    assert records[0]["context"] == "module"
    assert records[0]["reason_status"] == "dynamic_expression"


def test_collection_controls(tmp_path):
    records = inventory(
        tmp_path,
        """collect_ignore = ["slow.py"]
def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(marker_factory())
""",
    )
    assert len(records) == 3
    assert all(r["kind"] == "collection_control" for r in records)


def test_review_metadata_requires_explicit_ownership(tmp_path):
    records = inventory(tmp_path, "import pytest\npytest.skip('no service')")
    assert _AUDIT.apply_dispositions(records, {}) == [records[0]["id"]]
    metadata = {
        "categories": {
            "service": {
                "owner_role": "service maintainer",
                "next_milestone": "service contract gate",
                "disposition": "deployment prerequisite",
            }
        },
        "sites": {
            records[0]["id"]: {
                "category": "service",
                "reason": "service unavailable",
                "review_date": "2026-10-18",
                "prerequisites": [],
                "source_sha256": records[0]["source_sha256"],
            }
        },
    }
    assert _AUDIT.apply_dispositions(records, metadata) == []
    assert records[0]["review"]["reason"] == "service unavailable"
    metadata["sites"][records[0]["id"]]["source_sha256"] = "stale"
    assert _AUDIT.apply_dispositions(records, metadata) == [records[0]["id"]]


def test_parse_error_is_reported(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "evals/tests").mkdir(parents=True)
    (tmp_path / "tests/test_bad.py").write_text("def broken(")
    records, errors = _AUDIT.collect_callsites(tmp_path)
    assert not records
    assert errors[0]["file"] == "tests/test_bad.py"


def test_deterministic_order_and_hash(tmp_path):
    first = inventory(tmp_path, "import pytest\npytest.skip('reason')")
    second, errors = _AUDIT.collect_callsites(tmp_path)
    assert not errors
    assert first == second
    assert len(first[0]["source_sha256"]) == 64


def test_unittest_skiptest_and_unrelated_method(tmp_path):
    records = inventory(
        tmp_path,
        """import unittest as ut
class Example(ut.TestCase):
    def test_conditional(self):
        self.skipTest("no live rows")
class Unrelated:
    def method(self):
        self.skipTest("not unittest")
""",
    )
    assert len(records) == 1
    assert records[0]["form"] == "unittest TestCase runtime skip"
    assert records[0]["node"] == "Example.test_conditional"


def test_missing_reason_flagged_without_execution(tmp_path):
    records = inventory(tmp_path, "import pytest\npytest.skip()")
    assert records[0]["reason_status"] == "reason_not_supplied"


def test_cli_rejects_orphan_reviews(tmp_path, monkeypatch):
    import json
    import sys

    inventory(tmp_path, "import pytest\npytest.skip('reason')")
    metadata = tmp_path / "reviews.json"
    metadata.write_text(json.dumps({"sites": {"deleted.py:1:skip": {}}}))
    output = tmp_path / "audit.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit",
            "--root",
            str(tmp_path),
            "--output",
            str(output),
            "--dispositions",
            str(metadata),
        ],
    )
    assert _AUDIT.main() == 1
    result = json.loads(output.read_text())
    assert result["stale_review_sites"] == ["deleted.py:1:skip"]
    assert len(result["missing_reviews"]) == 1
