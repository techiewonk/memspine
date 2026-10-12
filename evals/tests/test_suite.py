"""P0-08: suite definition, registry validation and the adoption gate (no engine needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from memspine_evals.behavioural import BehaviouralReport, CaseResult, Status
from suite.gate import BASELINE_PATH, adopt, regressions
from suite.suite import SUITE_PATH, SuiteError, load_suite


def _suite_dict() -> dict:
    return yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, d: dict) -> Path:
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(d), encoding="utf-8")
    return p


def test_suite_loads_and_has_the_decided_roles() -> None:
    s = load_suite()
    assert {e.id for e in s.by_role("dev")} == {"locomo", "op_bench"}
    assert {e.dataset for e in s.by_role("blind")} == {"memoryagentbench", "convomem", "prefeval"}
    assert [e.id for e in s.by_role("behavioural")] == ["behavioural"]
    assert 5 in next(e for e in s.entries if e.id == "locomo").categories
    assert all(e.metric and e.judge and e.slice and e.pass_rule for e in s.entries)


def test_longmemeval_never_appears() -> None:
    s = load_suite()
    assert all("longmemeval" not in (e.dataset or "").lower() and "longmemeval" not in e.id.lower() for e in s.entries)


@pytest.mark.parametrize("name", ["longmemeval", "LongMemEval", "long_mem_eval"])
def test_longmemeval_is_refused(tmp_path: Path, name: str) -> None:
    d = _suite_dict()
    d["entries"].append({"id": "lme", "dataset": name, "role": "dev", "slice": "x", "metric": "m",
                         "judge": "j", "pass_rule": "r"})
    with pytest.raises(SuiteError, match="(?i)excluded|registry"):
        load_suite(_write(tmp_path, d))


def test_unknown_dataset_id_is_refused(tmp_path: Path) -> None:
    d = _suite_dict()
    d["entries"][0]["dataset"] = "no_such_benchmark"
    with pytest.raises(SuiteError, match="not in the dataset registry"):
        load_suite(_write(tmp_path, d))


def test_missing_required_set_is_refused(tmp_path: Path) -> None:
    d = _suite_dict()
    d["entries"] = [e for e in d["entries"] if e.get("dataset") != "prefeval"]
    with pytest.raises(SuiteError, match="prefeval"):
        load_suite(_write(tmp_path, d))


def test_missing_field_and_bad_role_are_refused(tmp_path: Path) -> None:
    d = _suite_dict()
    d["entries"][1].pop("metric")
    with pytest.raises(SuiteError, match="lacks"):
        load_suite(_write(tmp_path, d))
    d = _suite_dict()
    d["entries"][1]["role"] = "gold"
    with pytest.raises(SuiteError, match="unknown role"):
        load_suite(_write(tmp_path, d))


def test_unadapted_dataset_is_refused(tmp_path: Path) -> None:
    d = _suite_dict()
    d["entries"][0]["dataset"] = "statemembench"
    with pytest.raises(SuiteError, match="no adapter"):
        load_suite(_write(tmp_path, d))


# ------------------------------------------------------------------ gate


def _report(**status: str) -> BehaviouralReport:
    return BehaviouralReport(
        [CaseResult(k, "safety", "d", "disjoint", Status(v)) for k, v in status.items()]
    )


ALL_PROMOTE = {i: "PROMOTE" for i in ("locomo", "op_bench", "mab_cr", "convomem", "prefeval")}
REF = {"A": "PASS", "B": "FAIL", "C": "NOT_SUPPORTED"}


def test_promote_without_regression_stays_promote() -> None:
    r = adopt(ALL_PROMOTE, _report(A="PASS", B="FAIL", C="NOT_SUPPORTED"), REF)
    assert r.verdict == "PROMOTE" and not r.regressions
    assert r.not_supported == ["C"]


def test_pass_to_fail_blocks_adoption() -> None:
    r = adopt(ALL_PROMOTE, _report(A="FAIL", B="FAIL", C="NOT_SUPPORTED"), REF)
    assert r.verdict == "HOLD" and r.regressions == ["A"]
    assert r.reasons[0] == "behavioural regression"


def test_fail_to_fail_and_not_supported_never_block() -> None:
    r = adopt(ALL_PROMOTE, _report(A="PASS", B="FAIL", C="FAIL"), REF)
    assert r.verdict == "PROMOTE"
    r = adopt(ALL_PROMOTE, _report(A="NOT_SUPPORTED", B="FAIL", C="NOT_SUPPORTED"), REF)
    assert r.verdict == "PROMOTE" and r.not_supported == ["A", "C"]


def test_dropped_case_is_a_regression() -> None:
    assert regressions(_report(B="FAIL"), REF) == ["A"]


def test_reject_is_not_softened_and_missing_inputs_refuse() -> None:
    bad = {**ALL_PROMOTE, "convomem": "REJECT"}
    assert adopt(bad, _report(A="PASS"), REF).verdict == "REJECT"
    assert adopt({"locomo": "PROMOTE"}, _report(A="PASS"), REF).verdict == "INCOMPLETE"
    assert adopt(ALL_PROMOTE, None, REF).verdict == "INCOMPLETE"


def test_default_reference_is_the_stored_baseline() -> None:
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["statuses"]
    passing = [k for k, v in base.items() if v == "PASS"]
    assert passing, "the baseline should hold at least one PASS to guard"
    report = _report(**{k: ("FAIL" if k == passing[0] else v) for k, v in base.items()})
    r = adopt(ALL_PROMOTE, report)
    assert r.verdict == "HOLD" and r.regressions == [passing[0]]


def test_tenant_or_viewer_fail_rejects_regardless_of_reference() -> None:
    for cid in ("SAF-TENANT-SEARCH", "SAF-TENANT-READ", "SAF-VIEWER"):
        # reference already FAILs the case: still REJECT
        r = adopt(ALL_PROMOTE, _report(**{cid: "FAIL"}), {cid: "FAIL"})
        assert r.verdict == "REJECT" and r.reasons[0].startswith("safety failure") and cid in r.reasons[0]
        r = adopt(ALL_PROMOTE, _report(**{cid: "FAIL"}), {cid: "PASS"})
        assert r.verdict == "REJECT"


def test_other_safety_and_invariance_stay_regression_based() -> None:
    ref = {"SAF-GENERIC": "FAIL", "SAF-UNANSWERABLE": "PASS", "INV-RENAME-SEARCH": "FAIL"}
    r = adopt(ALL_PROMOTE, _report(**{"SAF-GENERIC": "FAIL", "SAF-UNANSWERABLE": "PASS", "INV-RENAME-SEARCH": "FAIL"}), ref)
    assert r.verdict == "PROMOTE"
    r = adopt(ALL_PROMOTE, _report(**{"SAF-GENERIC": "FAIL", "SAF-UNANSWERABLE": "FAIL", "INV-RENAME-SEARCH": "FAIL"}), ref)
    assert r.verdict == "HOLD" and r.regressions == ["SAF-UNANSWERABLE"]


def test_tenant_not_supported_does_not_reject() -> None:
    assert adopt(ALL_PROMOTE, _report(**{"SAF-VIEWER": "NOT_SUPPORTED"}), {"SAF-VIEWER": "PASS"}).verdict == "PROMOTE"


def test_judge_ids_are_real_and_unknown_ones_refused(tmp_path: Path) -> None:
    from suite.suite import known_judge_ids

    ids = known_judge_ids()
    assert {"alias-contains", "qwen3-32b-rubric", "qwen3-32b-opbench", "qwen3-32b-constraint"} <= ids
    assert all(e.judge in ids for e in load_suite().entries if e.role != "behavioural")
    d = _suite_dict()
    d["entries"][0]["judge"] = "qwen3-32b-binary"
    with pytest.raises(SuiteError, match="unknown judge id"):
        load_suite(_write(tmp_path, d))
    d = _suite_dict()
    d["entries"][-1]["judge"] = "contains"
    with pytest.raises(SuiteError, match="unknown judge id"):
        load_suite(_write(tmp_path, d))
