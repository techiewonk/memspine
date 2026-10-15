"""ER-36: the blind exposure ledger refuses a second scoring of the same arm unless a reason is given."""

from __future__ import annotations

import json
from pathlib import Path

import eval_blind
import pytest
from memspine_evals import seal
from test_eval_gate_wiring import _blind, manifest


@pytest.fixture(autouse=True)
def _no_real_slices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        eval_blind, "HERE", tmp_path / "nowhere"
    )  # slice hash -> "unknown"; no real files read


def entries(runs: Path) -> list[dict]:
    return seal.read_ledger(seal.default_ledger(runs))


def setup(runs: Path, flags: dict | None = None, commit: str = "c1") -> None:
    _blind(runs, "arm", True, manifest(flags=flags, commit=commit))
    _blind(runs, "ref", False, manifest())


def run(runs: Path, *extra: str) -> int:
    return eval_blind.main(["arm", "--ref", "ref", "--runs-dir", str(runs), *extra])


def test_first_scoring_is_recorded_for_both_sets(tmp_path: Path) -> None:
    setup(tmp_path)
    assert run(tmp_path) == 0
    e = entries(tmp_path)
    assert {x["set_id"] for x in e} == {"blind-mab", "blind-cm"}
    x = e[0]
    assert x["verdict"] == "CONFIRMS" and x["reexpose"] is None and x["prior_exposures"] == 0
    assert x["arm"]["commit"] == "c1" and x["arm"]["identity_status"] == "identity"
    assert x["comparability_key"] and x["timestamp"].endswith("+00:00") and x["ref_run"] == "ref"
    assert "c0/q0" not in json.dumps(e)  # no question ids in the ledger


def test_only_the_ledger_is_written_under_logs(tmp_path: Path) -> None:
    setup(tmp_path)
    run(tmp_path)
    assert [p.name for p in (tmp_path / "_logs").iterdir()] == ["blind_exposure.jsonl"]


def test_second_scoring_of_same_arm_is_refused_without_output(tmp_path: Path, capsys) -> None:
    setup(tmp_path)
    run(tmp_path)
    capsys.readouterr()
    assert run(tmp_path) == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out and "--reexpose" in out and "verdict" not in out and " vs " not in out
    assert len(entries(tmp_path)) == 2  # nothing appended by the refusal


def test_reexpose_with_reason_is_allowed_and_recorded(tmp_path: Path) -> None:
    setup(tmp_path)
    run(tmp_path)
    assert run(tmp_path, "--reexpose", "judge outage rerun") == 0
    e = entries(tmp_path)
    assert len(e) == 4
    assert e[-1]["reexpose"] == "judge outage rerun" and e[-1]["prior_exposures"] == 1


def test_blank_reason_does_not_override(tmp_path: Path) -> None:
    setup(tmp_path)
    run(tmp_path)
    assert run(tmp_path, "--reexpose", "  ") == 2


def test_different_flags_are_a_different_arm(tmp_path: Path) -> None:
    setup(tmp_path, flags={"a": 1})
    run(tmp_path)
    _blind(tmp_path, "arm", True, manifest(flags={"a": 2}))  # same commit, new flags
    assert run(tmp_path) == 0
    assert len(entries(tmp_path)) == 4


def test_different_commit_is_a_different_arm(tmp_path: Path) -> None:
    setup(tmp_path, commit="c1")
    run(tmp_path)
    _blind(tmp_path, "arm", True, manifest(commit="c2"))
    assert run(tmp_path) == 0


def test_arm_without_identity_falls_back_to_run_id() -> None:
    a = seal.arm_id("arm-x", {"code": {"memspine_git": "abc"}})
    assert a.signature == "run:arm-x" and a.identity_status == "git-only"
    assert seal.arm_id("arm-y", None).signature == "run:arm-y"


def test_check_and_record_api(tmp_path: Path) -> None:
    led = tmp_path / "_logs" / "l.jsonl"
    a = seal.arm_id("r", manifest())
    assert seal.check(led, "blind-mab", a) == 0
    seal.record(
        led,
        set_id="blind-mab",
        slice_hash="h",
        arm=a,
        ref_run="ref",
        manifest=manifest(),
        verdict="NO CHANGE",
    )
    with pytest.raises(seal.SealRefused):
        seal.check(led, "blind-mab", a)
    assert seal.check(led, "blind-cm", a) == 0  # other set unaffected
    assert seal.check(led, "blind-mab", a, "because") == 1
    seal.record(
        led,
        set_id="blind-mab",
        slice_hash="h",
        arm=a,
        ref_run="ref",
        manifest=None,
        verdict="X",
        reexpose="because",
        prior=1,
    )
    assert len(seal.read_ledger(led)) == 2  # append-only: the first line is still there
    assert seal.read_ledger(led)[0]["verdict"] == "NO CHANGE"


def test_incomplete_attempt_does_not_spend_the_look(tmp_path: Path) -> None:
    _blind(tmp_path, "arm", True, manifest(), sets=("mab",))
    _blind(tmp_path, "ref", False, manifest())
    run(tmp_path)
    assert entries(tmp_path) == []
    _blind(tmp_path, "arm", True, manifest())  # the missing set arrives
    assert run(tmp_path) == 0 and len(entries(tmp_path)) == 2


def test_read_manifest_handles_absent_and_manifestless_files(tmp_path: Path) -> None:
    assert seal.read_manifest(tmp_path / "nope.jsonl") is None
    p = tmp_path / "r.jsonl"
    p.write_text('{"kind": "result"}\n', encoding="utf-8")
    assert seal.read_manifest(p) is None
