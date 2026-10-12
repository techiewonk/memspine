"""P0-03: run identity, comparability key, and the INCOMPARABLE verdict."""

from __future__ import annotations

from memspine_evals.manifest import (
    build_cost,
    build_identity,
    comparability_key,
    compare_manifests,
    hash_flags,
)
from memspine_evals.paired_gate import format_result, paired


def _manifest(**over) -> dict:
    m = {
        "dataset": {"dataset_id": "locomo", "revision_id": "r1", "content_sha256": "aa"},
        "judge": {"judge_id": "j", "scale": "binary", "model": "m-judge", "prompt_hash": "ph"},
        "reader": {"model": "m-reader"},
        "harness_version": "1.0",
        "protocol": {"budget_tokens": 4096, "top_k": 10, "seed": 1},
        "identity": build_identity(flags={"a": 1}, slice_id="s", slice_ids=["q1", "q2"], environ={}),
    }
    m.update(over)
    return m


def test_flags_hash_is_order_independent() -> None:
    assert hash_flags({"a": 1, "b": 2}) == hash_flags({"b": 2, "a": 1})
    assert hash_flags({"a": 1}) != hash_flags({"a": 2})


def test_identity_picks_up_pinned_env_and_models() -> None:
    env = {"MEMSPINE_PINNED_COMMIT": "abc", "MEMSPINE_ENV_HASH": "e1", "MEMSPINE_VENV_HASH": "v1"}
    i = build_identity(prompts={"judge": "text"}, models={"reader": "r"}, environ=env)
    assert (i["commit"], i["env_hash"], i["venv_hash"]) == ("abc", "e1", "v1")
    assert i["models"]["reader"] == "r" and i["models"]["judge"] is None
    assert len(i["prompts"]["judge"]["sha256"]) == 64


def test_arm_flags_commit_and_cost_do_not_change_the_key() -> None:
    a = _manifest()
    b = _manifest(
        identity={**a["identity"], "flags": {"a": 2}, "commit": "zzz", "env_hash": "q",
                  "cost": build_cost(duration_s=9.0)}
    )
    assert comparability_key(a) == comparability_key(b)


def test_reader_judge_slice_or_harness_change_the_key() -> None:
    a = _manifest()
    assert comparability_key(a) != comparability_key(_manifest(reader={"model": "other"}))
    assert comparability_key(a) != comparability_key(_manifest(harness_version="2.0"))
    other = build_identity(slice_id="s", slice_ids=["q1", "q3"], environ={})
    assert comparability_key(a) != comparability_key(_manifest(identity=other))
    diff = compare_manifests(a, _manifest(reader={"model": "other"}))
    assert len(diff) == 1 and diff[0].startswith("reader.model")


def test_cost_block_is_optional_and_null_safe() -> None:
    c = build_cost()
    assert c["duration_s"] is None and c["latency_p95_s"] is None and c["tokens"] == {}
    c = build_cost(tokens={"reader": {"prompt": 10}}, question_latencies_s=[1, 2, 3, 4, 100])
    assert c["tokens"]["reader"] == {"prompt": 10, "completion": None}
    assert (c["latency_p50_s"], c["latency_p95_s"]) == (3, 100)


def _runs():
    ids = [f"c{c}/q{i}" for c in range(10) for i in range(40)]
    ref = {q: False for q in ids}
    arm = {q: int(q.split("q")[-1]) < 6 for q in ids}
    return ids, arm, ref


def test_gate_returns_incomparable_and_lists_fields() -> None:
    ids, arm, ref = _runs()
    r = paired(arm, ref, ids, arm_manifest=_manifest(), ref_manifest=_manifest(reader={"model": "x"}))
    assert r.verdict == "INCOMPARABLE" and r.manifest_status == "incomparable"
    assert any("reader.model" in f for f in r.incomparable_fields)


def test_gate_with_matching_manifests_keeps_verdict() -> None:
    ids, arm, ref = _runs()
    r = paired(arm, ref, ids, arm_manifest=_manifest(), ref_manifest=_manifest())
    assert r.verdict == "PROMOTE" and r.manifest_status == "ok" and not r.warnings


def test_gate_without_manifest_is_unchanged_but_flagged() -> None:
    ids, arm, ref = _runs()
    r = paired(arm, ref, ids, arm_manifest=_manifest())
    assert r.verdict == "PROMOTE" and r.manifest_status == "missing"
    assert "reference" in r.warnings[0]
    assert any("WARNING" in line for line in format_result("x", r))


def test_budget_differs_is_incomparable_but_top_k_and_seed_are_not() -> None:
    ids, arm, ref = _runs()
    b = _manifest(protocol={"budget_tokens": 2048, "top_k": 10, "seed": 1})
    r = paired(arm, ref, ids, arm_manifest=_manifest(), ref_manifest=b)
    assert r.verdict == "INCOMPARABLE" and any("protocol.budget_tokens" in f for f in r.incomparable_fields)
    knobs = _manifest(protocol={"budget_tokens": 4096, "top_k": 3, "seed": 9})
    assert comparability_key(_manifest()) == comparability_key(knobs)
