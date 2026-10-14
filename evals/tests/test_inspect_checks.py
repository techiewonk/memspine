"""TI-6: inspect-bundle invariant checks. Hand-built tiny bundles (invented names), CPU only."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS))
from memspine_evals.inspect import checks as C  # noqa: E402

RUN_DATE = "2026-10-14"


def good() -> dict:
    """A tiny bundle in which every check passes."""
    turns = [
        {"item": "c1", "session": "s1", "turn_id": "T1", "speaker": "Bram", "ts": "2024-03-01T10:00:00",
         "record_id": "r1", "indexed": {"vector": True, "lexical": True}},
        {"item": "c1", "session": "s1", "turn_id": "T2", "speaker": "Ilse", "ts": "2024-03-01T10:01:00",
         "record_id": "r2", "indexed": {"vector": True, "lexical": True}},
        {"item": "c1", "session": "s2", "turn_id": "T3", "speaker": "Noor", "ts": "2024-03-08T09:00:00",
         "record_id": "r3", "indexed": {"vector": True, "lexical": True}},
    ]
    windows = [
        {"item": "c1", "window_id": "w1", "member_turns": ["T1", "T2"], "cut_reason": "session_end",
         "prepass": {"verdict": "extract"}, "call": {"model": "stub", "cached": False, "tokens": 40},
         "sections": {"facts": 1, "edges": 0, "episode": 1}},
        {"item": "c1", "window_id": "w2", "member_turns": [], "cut_reason": "size",
         "prepass": {"verdict": "skip"}},
    ]
    envelopes = [
        {"item": "c1", "occ": "o1", "stage": "compile", "status": "kept"},
        {"item": "c1", "occ": "o1", "stage": "deposit", "status": "kept", "record_id": "r4",
         "integrate": {"op": "supersede", "target": "r5"}},
        {"item": "c1", "occ": "o2", "stage": "300", "status": "dropped", "reason": "ungrounded"},
    ]
    records = [
        {"item": "c1", "phase": "post_ingest", "record_id": "r1", "channel": "verbatim", "content": "Bram plays cello"},
        {"item": "c1", "phase": "post_ingest", "record_id": "r2", "channel": "verbatim", "content": "Ilse hikes"},
        {"item": "c1", "phase": "post_ingest", "record_id": "r3", "channel": "verbatim", "content": "Noor moved"},
        {"item": "c1", "phase": "post_ingest", "record_id": "r4", "channel": "fact", "content": "Bram plays viola",
         "parents": ["r1"], "parent_turns": ["T1"], "valid_from": "2024-03-01T10:00:00"},
        {"item": "c1", "phase": "post_ingest", "record_id": "r5", "channel": "fact", "content": "Bram plays cello",
         "parents": ["r1"], "parent_turns": ["T1"], "valid_from": "2024-02-01T00:00:00",
         "valid_to": "2024-03-01T10:00:00", "status": "superseded", "evolve_to": "r4"},
    ]
    links = [
        {"item": "c1", "kind": "lineage", "src": "r4", "dst": "r1"},
        {"item": "c1", "kind": "graph", "src": "r4", "dst": "n_bram"},
        {"item": "c1", "kind": "node", "src": "n_bram"},
        {"item": "c1", "kind": "mentions", "src": "r2", "dst": "Tomasz"},
    ]
    sleep = [
        {"item": "c1", "stage": "consolidate", "cycle": 1, "session_key": "s1", "input_ids": ["r1"], "output_ids": ["r4"]},
        {"item": "c1", "stage": "consolidate", "cycle": 2, "session_key": "s1", "input_ids": ["r1"], "output_ids": []},
    ]
    questions = [
        {"item": "c1", "query_id": "q1", "plan": {"route": "hybrid"}, "gold_turns": ["T1", "T3"],
         "legs": {"vector": [{"rid": "r1", "turn": "T1", "rank": 1}]},
         "fused": [{"rid": "r1", "turn": "T1"}], "pool": [{"rid": "r1", "turn": "T1"}],
         "rerank": [{"rid": "r1", "turn": "T1"}], "packed": [{"rid": "r1", "turn": "T1", "tokens": 30}],
         "budget": 100,
         "gold_path": [{"turn": "T1", "last_stage_reached": "packed"}, {"turn": "T3", "last_stage_reached": None}]},
    ]
    return {"turns": turns, "windows": windows, "envelopes": envelopes, "records": records, "links": links,
            "sleep": sleep, "questions": questions, "manifest": {"run_started_at": RUN_DATE + "T08:00:00"}}


def write(tmp_path: Path, bundle: dict) -> Path:
    for name, rows in bundle.items():
        if name == "manifest":
            (tmp_path / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
        else:
            (tmp_path / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return tmp_path


def by_check(results):
    out: dict[str, list] = {}
    for r in results:
        out.setdefault(r.check_id, []).append(r)
    return out


def rec(b, rid, phase="post_ingest"):
    return next(r for r in b["records"] if r["record_id"] == rid and r["phase"] == phase)


def mut_turn_twice(b): b["turns"].append(copy.deepcopy(b["turns"][0]))
def mut_not_vector(b): b["turns"][1]["indexed"]["vector"] = False
def mut_not_lexical(b): b["turns"][1]["indexed"]["lexical"] = False
def mut_no_members(b): b["windows"][0]["member_turns"] = []
def mut_no_call(b): b["windows"][0]["call"] = {}
def mut_uncounted(b): b["windows"][0]["sections"] = {"facts": 1}
def mut_cross_session(b): b["windows"][0]["member_turns"] = ["T2", "T3"]
def mut_env_open(b): b["envelopes"] = [e for e in b["envelopes"] if e["stage"] != "deposit"]
def mut_env_noreason(b): b["envelopes"][2].pop("reason")
def mut_supersede_notarget(b): b["envelopes"][1]["integrate"] = {"op": "supersede"}
def mut_no_parent(b): rec(b, "r4").update(parents=["gone"], parent_turns=[])
def mut_no_channel(b): rec(b, "r4").pop("channel")
def mut_cycle(b):
    rec(b, "r1")["parents"] = ["r4"]


def mut_evolve_missing(b): rec(b, "r5")["evolve_to"] = "nope"
def mut_time_order(b): rec(b, "r4")["valid_from"] = "2024-01-01T00:00:00"
def mut_dup_content(b):
    b["records"].append({"item": "c1", "phase": "post_ingest", "record_id": "r6", "channel": "fact",
                         "content": "  bram PLAYS viola ", "parents": ["r1"], "parent_turns": ["T1"]})


def mut_parent_turn_outside(b): rec(b, "r4")["parent_turns"] = ["T1", "T99"]
def mut_wall_clock(b): rec(b, "r4")["valid_from"] = RUN_DATE + "T08:00:00"
def mut_edge_missing(b): b["links"][1]["dst"] = "n_missing"
def mut_sleep_not_idempotent(b): b["sleep"][1]["output_ids"] = ["r9"]
def mut_no_plan(b): b["questions"][0]["plan"] = {}
def mut_no_leg(b): b["questions"][0]["legs"] = {}
def mut_over_budget(b): b["questions"][0]["budget"] = 20
def mut_gold_path(b): b["questions"][0]["gold_path"][0]["last_stage_reached"] = "pool"


CASES = [
    ("turn_written_once", mut_turn_twice),
    ("turn_in_vector_index", mut_not_vector),
    ("turn_in_lexical_index", mut_not_lexical),
    ("window_has_members", mut_no_members),
    ("window_call_outcome", mut_no_call),
    ("window_sections_counted", mut_uncounted),
    ("window_single_session_at_session_end", mut_cross_session),
    ("envelope_terminal", mut_env_open),
    ("envelope_terminal", mut_env_noreason),
    ("supersede_has_target", mut_supersede_notarget),
    ("derived_has_live_parent_and_channel", mut_no_parent),
    ("derived_has_live_parent_and_channel", mut_no_channel),
    ("no_parent_cycles", mut_cycle),
    ("supersede_target_and_time_order", mut_evolve_missing),
    ("supersede_target_and_time_order", mut_time_order),
    ("duplicate_record_content", mut_dup_content),
    ("derived_parent_turns_in_item", mut_parent_turn_outside),
    ("no_wall_clock_in_derived", mut_wall_clock),
    ("graph_edge_endpoints_exist", mut_edge_missing),
    ("sleep_idempotent", mut_sleep_not_idempotent),
    ("question_has_plan", mut_no_plan),
    ("question_has_leg", mut_no_leg),
    ("question_context_within_budget", mut_over_budget),
    ("gold_path_matches_stage_lists", mut_gold_path),
]


def test_good_bundle_passes_everything(tmp_path):
    res = C.run_checks(write(tmp_path, good()))
    assert [r for r in res if r.status == "fail"] == []
    assert [r for r in res if r.status == "skipped"] == []
    assert {r.check_id for r in res} == set(C.REGISTRY)


def test_every_registered_check_has_a_failing_case_and_docstring():
    assert {c for c, _ in CASES} == set(C.REGISTRY) - {"bundle_wellformed"}
    for c in C.REGISTRY.values():
        assert (c.fn.__doc__ or "").strip().startswith("Invariant:"), c.check_id


@pytest.mark.parametrize("check_id,mut", CASES, ids=[f"{c}-{m.__name__}" for c, m in CASES])
def test_check_fails_on_defect(tmp_path, check_id, mut):
    b = good()
    mut(b)
    res = by_check(C.run_checks(write(tmp_path, b)))
    failed = [r for r in res[check_id] if r.status == "fail"]
    assert failed, f"{check_id} did not fail"
    assert all(not r.ok and r.detail for r in failed)


def test_missing_files_are_skipped_never_failed(tmp_path):
    res = by_check(C.run_checks(write(tmp_path, {"turns": good()["turns"]})))
    assert all(r.status in ("ok", "skipped") for v in res.values() for r in v)
    assert res["window_has_members"][0].status == "skipped"
    assert res["question_has_plan"][0].status == "skipped"
    assert res["turn_written_once"][0].status == "ok"


def test_empty_directory_all_skipped(tmp_path):
    res = C.run_checks(tmp_path)
    assert {r.status for r in res if r.check_id != "bundle_wellformed"} == {"skipped"}
    assert not [r for r in res if r.status == "fail"]


def test_no_manifest_run_time_skips_wall_clock(tmp_path):
    b = good()
    b.pop("manifest")
    res = by_check(C.run_checks(write(tmp_path, b)))
    assert res["no_wall_clock_in_derived"][0].status == "skipped"


def test_wall_clock_ok_when_source_turn_is_run_date(tmp_path):
    b = good()
    b["turns"][0]["ts"] = RUN_DATE + "T07:00:00"
    rec(b, "r4")["valid_from"] = RUN_DATE + "T08:00:00"
    res = by_check(C.run_checks(write(tmp_path, b)))
    assert not [r for r in res["no_wall_clock_in_derived"] if r.status == "fail"]


def test_lexical_check_skipped_without_hybrid(tmp_path):
    b = good()
    for t in b["turns"]:
        t["indexed"].pop("lexical")
    res = by_check(C.run_checks(write(tmp_path, b)))
    assert res["turn_in_lexical_index"][0].status == "skipped"


def test_session_end_check_ignores_other_cut_reasons(tmp_path):
    b = good()
    mut_cross_session(b)
    b["windows"][0]["cut_reason"] = "size"
    res = by_check(C.run_checks(write(tmp_path, b)))
    assert not [r for r in res["window_single_session_at_session_end"] if r.status == "fail"]


def test_malformed_line_reported(tmp_path):
    write(tmp_path, good())
    with (tmp_path / "turns.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    res = by_check(C.run_checks(tmp_path))
    assert [r for r in res["bundle_wellformed"] if r.status == "fail"]


def test_outputs_written(tmp_path):
    b = good()
    mut_not_vector(b)
    C.run_checks(write(tmp_path, b))
    rows = [json.loads(ln) for ln in (tmp_path / "checks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"check_id", "scope", "id", "ok", "detail"} <= set(rows[0])
    md = (tmp_path / "checks_summary.md").read_text(encoding="utf-8")
    assert "turn_in_vector_index" in md and "T2" in md and "## Item c1" in md


def test_summary_lists_first_ten_failures_only(tmp_path):
    b = good()
    b["turns"] = [{"item": "c1", "session": "s1", "turn_id": f"X{i}", "record_id": f"q{i}",
                   "indexed": {"vector": False}} for i in range(15)]
    C.run_checks(write(tmp_path, {"turns": b["turns"]}))
    line = next(ln for ln in (tmp_path / "checks_summary.md").read_text(encoding="utf-8").splitlines()
                if ln.startswith("| turn_in_vector_index"))
    assert "X9" in line and "X10" not in line and "| 0 | 15 |" in line


def test_cli_exit_codes(tmp_path, capsys):
    b = good()
    d1 = tmp_path / "ok"
    d1.mkdir()
    write(d1, b)
    assert C.main([str(d1), "--fail-on", "error"]) == 0
    d2 = tmp_path / "bad"
    d2.mkdir()
    mut_no_plan(b)
    write(d2, b)
    assert C.main([str(d2)]) == 0
    assert C.main([str(d2), "--fail-on", "error"]) == 1
    # warnings alone do not trip --fail-on error but do trip warn
    d3 = tmp_path / "warn"
    d3.mkdir()
    b = good()
    mut_dup_content(b)
    write(d3, b)
    assert C.main([str(d3), "--fail-on", "error"]) == 0
    assert C.main([str(d3), "--fail-on", "warn"]) == 1
    assert C.main([str(tmp_path / "missing")]) == 2
