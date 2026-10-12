"""P0-08: the behavioural suite runs model-free on CPU and returns a well-formed report.

A FAIL on today's engine is information, not a test failure: the test checks the runner and the report,
and that the stored baseline covers exactly the current cases.
"""

from __future__ import annotations

import json
import logging

import pytest

from memspine_evals.behavioural import CASES, BehaviouralReport, Status, run_behavioural
from memspine_evals.behavioural_cases import FILLER_TURNS, STORY_A, rename, rename_story
from suite.gate import BASELINE_PATH


@pytest.fixture(scope="module")
def report() -> BehaviouralReport:
    logging.disable(logging.CRITICAL)
    try:
        return run_behavioural()
    finally:
        logging.disable(logging.NOTSET)


def test_report_is_well_formed(report: BehaviouralReport) -> None:
    assert [r.id for r in report.results] == [c.id for c in CASES]
    for r, c in zip(report.results, CASES, strict=True):
        assert r.kind in {"invariance", "safety"} and r.relation in {"equal", "subset", "disjoint", "abstain"}
        assert r.description and isinstance(r.status, Status)
        assert (r.kind, r.relation) == (c.kind, c.relation)
    counts = report.counts()
    assert sum(counts.values()) == len(CASES)
    assert report.by_kind().keys() == {"invariance", "safety"}
    assert BehaviouralReport.from_dict(json.loads(json.dumps(report.to_dict()))).statuses() == report.statuses()


def test_cases_cover_the_decided_kinds() -> None:
    ids = {c.id for c in CASES}
    assert {"INV-RENAME-SEARCH", "INV-PARAPHRASE", "INV-ORDER-SWAP", "INV-FILLER"} <= ids
    assert {"SAF-TENANT-SEARCH", "SAF-VIEWER", "SAF-UNANSWERABLE", "SAF-GENERIC"} <= ids


def test_tenant_isolation_holds(report: BehaviouralReport) -> None:
    """Not a baseline observation: a tenant leak must never reach main."""
    assert report.status_of("SAF-TENANT-SEARCH") is Status.PASS
    assert report.status_of("SAF-TENANT-READ") is Status.PASS


def test_unsupported_feature_is_not_a_pass() -> None:
    class NoViewer:
        async def search(self, query, namespace="default", top_k=5):  # no viewer parameter
            return []

        async def write(self, content, namespace="default"):
            return None

        async def read(self, query, namespace="default"):
            return None

        async def start(self):
            return self

        async def stop(self):
            return None

    rep = run_behavioural(lambda _d: NoViewer(), only={"SAF-VIEWER"})
    assert rep.status_of("SAF-VIEWER") is Status.NOT_SUPPORTED


def test_crashing_case_is_a_fail_not_a_pass() -> None:
    class Broken:
        async def start(self):
            return self

        async def stop(self):
            return None

    rep = run_behavioural(lambda _d: Broken(), only={"INV-FILLER"})
    assert rep.status_of("INV-FILLER") is Status.FAIL


def test_stored_baseline_covers_current_cases() -> None:
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert set(base["statuses"]) == {c.id for c in CASES}
    assert set(base["statuses"].values()) <= {s.value for s in Status}
    assert base["counts"] == {s.value: list(base["statuses"].values()).count(s.value) for s in Status}


def test_cases_use_invented_names_and_rename_is_whole_word() -> None:
    assert rename("Ilse and Ilsewood", {"Ilse": "Aiko"}) == "Aiko and Ilsewood"
    renamed = rename_story(STORY_A)
    assert "Ilse" not in " ".join(t for s in renamed.sessions for _, t in s.turns)
    assert all("Ilse" not in t for _, t in FILLER_TURNS)
