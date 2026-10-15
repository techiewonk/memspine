"""I24 metrics, I35 second view, I14 errata, I15 splits, I16 noise floor (all offline)."""

from __future__ import annotations

import json
from pathlib import Path

import forensics_report as fr
import make_split
import noise_floor
import pytest
from memspine_evals import errata, second_view
from memspine_evals.split import Split, ids_hash, split_ids

EVALS = Path(__file__).resolve().parents[1]


def _row(cat: str, answer: str | None, correct: bool, mode: str = "qa", ctx: int = 100, retrieved=("D1:1",)) -> dict:
    return {"category": cat, "answer": answer, "correct": correct, "mode": mode, "status": "completed",
            "context_tokens": ctx, "retrieved_turns": list(retrieved), "item": "c", "qid": "q"}


# -- I24 ---------------------------------------------------------------------------------


def test_abstention_prf_counts_refusals_on_cat5_and_answers_on_the_rest() -> None:
    rows = [
        _row("adversarial", "Not mentioned in the conversation", True),  # tp
        _row("adversarial", "Caroline went to Paris", False),  # fn
        _row("single-hop", "I do not know", False),  # fp
        _row("single-hop", "She painted a sunrise", True),  # tn
        _row("temporal", "7 May 2023", True),  # tn
    ]
    ab = fr.abstention_block(rows)
    assert (ab["tp"], ab["fp"], ab["fn"]) == (1, 1, 1)
    assert ab["precision"] == 0.5 and ab["recall"] == 0.5 and ab["f1"] == 0.5
    assert ab["answer_rate_when_answerable"] == pytest.approx(2 / 3)
    assert ab["false_refusal_rate"] == pytest.approx(1 / 3)


def test_abstention_is_none_for_retrieval_runs_or_without_cat5() -> None:
    assert fr.abstention_block([_row("adversarial", None, False, mode="retrieval")]) is None
    assert fr.abstention_block([_row("single-hop", "x", True)]) is None


def test_per_category_table_includes_cat5_and_memory_used() -> None:
    rows = [_row("adversarial", "Not mentioned", True), _row("single-hop", "x", False, ctx=0, retrieved=())]
    t = fr.by_category_all(rows)
    assert t["adversarial"]["accuracy"] == 1.0 and t["adversarial"]["refusal_rate"] == 1.0
    assert t["single-hop"]["refusal_rate"] == 0.0
    mu = fr.memory_used_block(rows)
    assert mu["rate"] == 0.5 and mu["by_category"]["single-hop"] == 0.0


def test_trigger_block_reports_fired_rates_and_logged_gates() -> None:
    fx = [
        {"extra_legs": {"speaker_vote": [1], "bridge": [1]}, "bridge_gate": "cue", "rerank_scores": [1], "_correct": True},
        {"extra_legs": {}, "bridge_gate": "skipped", "rerank_scores": [], "_correct": False},
        {"extra_legs": {"session": [1]}, "rerank_scores": [1], "_correct": True},
    ]
    t = fr.trigger_block(fx)
    assert t["n"] == 3 and t["list_mode"]["fired"] == 1 and t["bridge_hop"]["fired"] == 1
    assert t["bridge_hop"]["gate_decisions"] == {"cue": 1, "skipped": 1}
    assert t["rerank"]["fired"] == 2 and t["legs"]["session"]["fired"] == 1
    assert t["decider"]["logged"] is False
    assert t["list_mode"]["accuracy_fired"] == 1.0 and t["list_mode"]["accuracy_not_fired"] == 0.5
    assert fr.trigger_block([]) is None


def test_summary_schema_accepts_the_new_blocks() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((EVALS / "schemas" / "forensic_run.schema.json").read_text(encoding="utf-8"))
    rows = [_row("adversarial", "Not mentioned", True), _row("single-hop", "x", True)]
    doc = {
        "schema_version": "forensic_run/v1", "run_id": "r", "mode": "qa", "has_stage_log": False,
        "n_questions": 1, "accuracy": 1.0, "by_category": {}, "primary_gaps": {}, "outcome_classes": {},
        "abstention": fr.abstention_block(rows), "by_category_all": fr.by_category_all(rows),
        "memory_used": fr.memory_used_block(rows), "triggers": None,
    }
    jsonschema.validate(doc, schema)


# -- I35 ---------------------------------------------------------------------------------


def _results(path: Path) -> None:
    base = {"kind": "result", "run_id": "r1", "item_id": "conv-26", "status": "completed"}
    rows = [
        {**base, "query_id": "0-1", "type_label": "cat1", "question": "Where?", "gold": "Paris", "answer": "In Paris", "score": "1.0"},
        {**base, "query_id": "0-2", "type_label": "cat2", "question": "When?", "gold": "May", "answer": "June", "score": "0.0"},
        {**base, "query_id": "0-3", "type_label": "cat5", "question": "Q5", "gold": "Not mentioned", "answer": "x", "score": "1.0"},
        {**base, "query_id": "0-4", "type_label": "cat1", "question": "Q?", "gold": "g", "answer": "a", "score": "0.0", "status": "error"},
    ]
    path.write_text("\n".join(["{\"kind\": \"manifest\"}", *map(json.dumps, rows)]), encoding="utf-8")


def test_second_view_prepare_writes_mem0_prompts_and_never_calls_a_model(tmp_path: Path) -> None:
    res = tmp_path / "results.jsonl"
    _results(res)
    out = tmp_path / "batch.jsonl"
    info = second_view.prepare(res, out)
    assert info["requests"] == 2 and info["skipped_by_reason"] == {"cat5_abstention": 1, "not_completed": 1}
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "batch_manifest" and lines[0]["makes_model_calls"] is False
    req = lines[1]
    assert "be generous with your grading" in req["prompt"] and "Paris" in req["prompt"] and req["system"]
    assert req["custom_id"] == "r1|conv-26|0-1"


def test_second_view_collect_scores_replies(tmp_path: Path) -> None:
    res, batch, replies = tmp_path / "r.jsonl", tmp_path / "b.jsonl", tmp_path / "rep.jsonl"
    _results(res)
    second_view.prepare(res, batch)
    replies.write_text(
        json.dumps({"custom_id": "r1|conv-26|0-1", "reply": '{"reasoning": "ok", "label": "CORRECT"}'}) + "\n"
        + json.dumps({"custom_id": "r1|conv-26|0-2", "reply": '{"reasoning": "no", "label": "WRONG"}'}),
        encoding="utf-8")
    s = second_view.collect(batch, replies, tmp_path / "s.json")
    assert s["accuracy"]["ALL"]["mem0_official"] == 0.5 and s["accuracy"]["ALL"]["original_judge"] == 0.5
    assert s["missing_replies"] == 0 and s["accuracy"]["cat1"]["n"] == 1


# -- I14 errata --------------------------------------------------------------------------


def test_generic_errata_loads_any_dataset_and_aliases(tmp_path: Path) -> None:
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"schema": "errata/v1", "dataset": "prefeval", "dataset_sha256_prefix": "abc",
        "entries": [{"item_id": "u1", "query_id": "q1", "tag": "gold_error", "borderline": True},
                    {"item": "u1", "qid": "q1", "tag": "gold_error"},
                    {"item": "u2", "qid": "q2", "tag": "evidence_label_error"},
                    {"item": "u3", "qid": "q3", "tag": "custom"}]}), encoding="utf-8")
    got = errata.load_errata(f, dataset="prefeval", content_sha256="abcdef")
    assert got == {("u1", "q1"): {"tag": "gold_error", "borderline": False}}
    assert ("u3", "q3") in errata.load_errata(f, exclude_tags=["custom"])
    with pytest.raises(errata.ErrataError):
        errata.load_errata(f, dataset="beam")
    with pytest.raises(errata.ErrataError):
        errata.load_errata(f, content_sha256="zzz")
    assert errata.load_errata(tmp_path / "missing.json") == {}
    kept, dropped = errata.partition([{"item_id": "u1", "query_id": "q1"}, {"item_id": "u9", "query_id": "q"}], got)
    assert len(kept) == 1 and len(dropped) == 1
    assert errata.validate_errata_file(f)["by_tag"]["gold_error"] == 2


def test_the_locomo_errata_file_still_loads_through_the_generic_loader() -> None:
    path = EVALS / "analysis" / "locomo_errata.json"
    old = fr.load_errata(path)
    assert old and all(v["tag"] in fr.ERRATA_EXCLUDE_TAGS for v in old.values())
    assert errata.load_errata(path) == old


# -- I15 splits --------------------------------------------------------------------------


def test_split_ids_by_names_keeps_clusters_whole_and_hashes() -> None:
    ids = ["conv-1:A", "conv-1:B", "conv-2:C", "conv-3:D"]
    s = split_ids(ids, dataset_id="x", content_sha256="ab" * 32, unit="persona", dev=["conv-1:A"],
                  group_of=lambda u: u.split(":")[0])
    assert s.dev_items == ("conv-1:A", "conv-1:B")  # conv-1 stays whole
    assert s.heldout_items == ("conv-2:C", "conv-3:D")
    assert s.split_sha256 == ids_hash(s.dev_items, s.heldout_items)
    s.verify_ids()


def test_split_ids_by_hash_is_deterministic_and_validates() -> None:
    ids = [f"u{i}" for i in range(10)]
    a = split_ids(ids, dataset_id="x", content_sha256="0" * 64, n_dev=3, seed=5)
    b = split_ids(list(reversed(ids)), dataset_id="x", content_sha256="0" * 64, n_dev=3, seed=5)
    assert a.dev_items == b.dev_items and len(a.dev_items) == 3 and len(a.heldout_items) == 7
    with pytest.raises(ValueError):
        split_ids(ids, dataset_id="x", content_sha256="0" * 64, dev=["nope"])
    with pytest.raises(ValueError):
        split_ids(ids, dataset_id="x", content_sha256="0" * 64, n_dev=10)


def test_split_file_edit_is_caught(tmp_path: Path) -> None:
    s = split_ids(["a", "b", "c"], dataset_id="x", content_sha256="1" * 64, dev=["a"])
    path = s.save(tmp_path / "s.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["dev_items"].append("b")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        Split.load(path).verify_ids()


def test_locomo_split_file_without_the_new_fields_still_loads() -> None:
    s = Split.load(EVALS / "analysis" / "locomo_split.json")
    s.verify_ids()
    assert s.split_sha256 == "" and "conv-26" in s.dev_items


def test_make_split_cli_opbench_personas(tmp_path: Path) -> None:
    data = tmp_path / "locomo10.json"
    names = [("conv-26", "Caroline"), ("conv-30", "Jon"), ("conv-41", "John"), ("conv-42", "Joanna"),
             ("conv-43", "Tim"), ("conv-44", "Audrey"), ("conv-47", "James"), ("conv-48", "Deborah"),
             ("conv-49", "Evan"), ("conv-50", "Calvin")]
    data.write_text(json.dumps([{"sample_id": c, "conversation": {"speaker_a": a, "speaker_b": "Z"}} for c, a in names]),
                    encoding="utf-8")
    out = tmp_path / "split.json"
    rc = make_split.main(["--dataset-id", "op_bench", "--unit", "persona", "--units", "locomo-first-speakers",
                          "--source", str(data), "--dev", "conv-26:Caroline", "conv-30:Jon", "conv-41:John",
                          "conv-42:Joanna", "--out", str(out)])
    assert rc == 0
    s = Split.load(out)
    assert s.dev_items == ("conv-26:Caroline", "conv-30:Jon", "conv-41:John", "conv-42:Joanna")
    assert s.heldout_items == ("conv-43:Tim", "conv-44:Audrey", "conv-47:James", "conv-48:Deborah",
                               "conv-49:Evan", "conv-50:Calvin")
    assert s.seed is None and s.unit == "persona"
    assert make_split.main(["--check", str(out), "--source", str(data)]) == 0
    data.write_text("[]", encoding="utf-8")
    assert make_split.main(["--check", str(out), "--source", str(data)]) == 1


def test_committed_opbench_split_matches_the_locomo_dev_conversations() -> None:
    s = Split.load(EVALS / "analysis" / "opbench_persona_split.json")
    s.verify_ids()
    locomo = Split.load(EVALS / "analysis" / "locomo_split.json")
    assert {i.split(":")[0] for i in s.dev_items} == set(locomo.dev_items)
    assert {i.split(":")[0] for i in s.heldout_items} == set(locomo.heldout_items)
    assert s.content_sha256 == locomo.content_sha256


# -- I16 noise ---------------------------------------------------------------------------


def test_paired_noise_identical_runs_have_no_noise() -> None:
    a = {f"q{i}": float(i % 2) for i in range(50)}
    r = noise_floor.paired_noise(a, dict(a), resamples=200)
    assert r["flips"] == 0 and r["sd_net_flips"] == 0 and r["coef"] == 0 and r["band95_questions"] == 0


def test_paired_noise_matches_the_binomial_expectation() -> None:
    n, flips_each = 400, 20
    a = {f"q{i}": 1.0 for i in range(n)}
    b = dict(a)
    for i in range(flips_each):
        b[f"q{i}"] = 0.0  # only-a right
    for i in range(flips_each):
        a[f"q{n - 1 - i}"] = 0.0
        b[f"q{n - 1 - i}"] = 1.0  # only-b right
    r = noise_floor.paired_noise(a, b, resamples=4000)
    assert r["flips"] == 40 and r["only_a"] == 20 and r["only_b"] == 20
    assert r["sd_net_flips"] == pytest.approx(40 ** 0.5, rel=0.1)  # sqrt(b + c)
    assert r["coef"] == pytest.approx((40 / 400) ** 0.5)
    assert r["delta_points"] == 0


def test_noise_floor_estimate_from_run_dirs_with_item_clusters(tmp_path: Path) -> None:
    def make(name: str, wrong: set[str]) -> Path:
        d = tmp_path / name
        d.mkdir()
        rows = [{"kind": "result", "item_id": f"c{i // 10}", "query_id": f"q{i}", "type_label": "cat1" if i % 2 else "cat2",
                 "status": "completed", "score": "0.0" if f"q{i}" in wrong else "1.0"} for i in range(40)]
        (d / "results.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
        return d

    res = noise_floor.estimate([make("a", {"q1", "q2"})], [make("b", {"q2", "q3", "q4"})], by_item=True)
    o = res["overall"]
    assert o["n"] == 40 and o["flips"] == 3 and o["cluster"] == "item"
    assert set(res["by_category"]) == {"cat1", "cat2"}
    assert "coef" in noise_floor.render(res)
