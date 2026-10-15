"""TI-3/TI-4: the inspect bundle assembler and the HTML inspector, on the invented demo conversation."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_inspector import build, build_demo, embed  # noqa: E402
from memspine_evals.inspect.assemble import assemble, gold_path, load_dataset, main  # noqa: E402
from memspine_evals.inspect.demo_run import make_demo_run  # noqa: E402

REACHED = {"none", "leg", "fused", "pool", "rerank", "packed"}


def rows(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    t = tmp_path_factory.mktemp("runs")
    return t, make_demo_run(t, "ref"), make_demo_run(t, "v2")


@pytest.fixture(scope="module")
def v2(runs):
    t, _ref, v2 = runs
    out = t / "bundle_v2"
    man = assemble(v2, out, "demo")
    return out, man


def test_dataset_loads_demo_fixture():
    ds = load_dataset("demo")["demo-1"]
    assert len(ds["turns"]) == 30 and len(ds["questions"]) == 8
    assert ds["questions"]["q3"]["gold_turns"] == ["D1:7", "D2:6"]


def test_bundle_files_and_manifest(v2):
    out, man = v2
    for n in ("turns", "windows", "envelopes", "records", "links", "sleep", "questions", "checks"):
        assert (out / f"{n}.jsonl").exists()
    assert man["counts"]["turns"] == 30 and man["counts"]["windows"] == 9 and man["counts"]["questions"] == 8
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", man["run_started_at"])
    assert man["tap_events"]["write.envelope.trace"] > 100


def test_turns_join_ingest_deposit_and_windows(v2):
    out, _ = v2
    t = {r["turn_id"]: r for r in rows(out / "turns.jsonl")}
    assert t["D1:1"]["record_id"] == "rec-1" and t["D1:1"]["window_ids"] == ["win-1-1"]
    assert t["D1:4"]["derived_record_ids"] == ["fct-1", "fct-2"]  # deposit delta of the window's last turn
    skipped = [r for r in t.values() if r["deposit_action"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["record_id"] is None
    assert t["D1:1"]["indexed"]["source"] == "tap" and t["D1:1"]["firewall"]["quarantined"] is False
    assert all(r["run_id"] == "demo-v2" and r["item"] == "demo-1" for r in t.values())


def test_windows_envelopes_records_links(v2):
    out, _ = v2
    w = {r["window_id"]: r for r in rows(out / "windows.jsonl")}
    assert w["win-3-2"]["member_turns"] == ["D3:5", "D3:6", "D3:7", "D3:8"]
    assert w["win-3-2"]["call"]["reply_text"] and w["win-3-2"]["prompt"]["id"] == "extract.v2"
    assert [d["occ"] for d in w["win-3-2"]["items"]["dead"]] == [14]
    env = rows(out / "envelopes.jsonl")
    e14 = [e for e in env if e["occ"] == 14]
    assert e14[-1]["stage"] == "300" and e14[-1]["status"] == "dropped"
    dep = [e for e in env if e["occ"] == 13 and e["stage"] == "deposit"][0]
    assert dep["record_id"] == "fct-13"
    assert any(e["occ"] == 3 and e["stage"] == "250" and e["status"] == "rewritten" for e in env)
    recs = rows(out / "records.jsonl")
    assert {r["phase"] for r in recs} == {"post_ingest", "post_sleep"}
    links = rows(out / "links.jsonl")
    assert any(k["rel"] == "supersede" and k["src"] == "fct-2" and k["dst"] == "fct-13" for k in links)
    assert {"lineage", "mentions", "typed", "reply"} <= {k["kind"] for k in links}
    assert len(rows(out / "sleep.jsonl")) == 6 and len(rows(out / "checks.jsonl")) == 4


def test_questions_gold_path_and_funnel(v2):
    out, _ = v2
    q = {r["query_id"]: r for r in rows(out / "questions.jsonl")}
    assert q["q4"]["correct"] and q["q4"]["gold_turns"] == ["D3:2", "D3:5"]
    assert all(g["last_stage_reached"] in REACHED and "turn" in g for r in q.values() for g in r["gold_path"])
    rescued = [g for g in q["q4"]["gold_path"] if g["rescued_from"]]
    assert rescued and rescued[0]["turn"] == "D3:5" and rescued[0]["last_stage_reached"] == "packed"
    assert q["q8"]["first_losing_stage"] == "reader" and not q["q8"]["correct"]
    assert q["q6"]["headers"] and q["q6"]["rerank"][0]["composite"]["rel"] > 0
    assert q["q6"]["reader"]["calls"][0]["prompt"] and q["q6"]["context_text"]
    assert [f["stage"] for f in q["q2"]["funnel"]] == ["recall", "fusion", "pool", "final", "context"]


def test_ref_arm_falls_back_without_tap_snapshots(runs, tmp_path):
    _t, ref, _ = runs
    man = assemble(ref, tmp_path, "demo")
    assert man["counts"]["windows"] == 0 and man["counts"]["envelopes"] == 0
    assert man["counts"]["records"] == 30 and man["counts"]["sleep"] == 3  # records from ingest rows, sleep from tap
    q = {r["query_id"]: r for r in rows(tmp_path / "questions.jsonl")}
    stages = {k: v["first_losing_stage"] for k, v in q.items()}
    assert stages["q2"] == "recall" and stages["q4"] == "fusion" and stages["q6"] == "rerank"
    assert stages["q7"] == "assembly" and stages["q3"] == "reader" and stages["q1"] is None
    last = {g["turn"]: g["last_stage_reached"] for g in q["q2"]["gold_path"]}
    assert last == {"D2:1": "packed", "D3:8": "none"}
    assert q["q6"]["gold_path"][0]["last_stage_reached"] == "pool"


def test_gold_path_unit_matches_forensics_report_order():
    f = {"vector": [{"r": 1, "turn": "a"}], "lexical": [], "extra_legs": {}, "fused": [], "pool": [], "rerank_scores": [], "final": []}
    gp, loss = gold_path(["a", "b"], f, {"a": {"written": True}, "b": {"written": False}}, set())
    assert [g["lost_at"] for g in gp] == ["fusion", "ingest"] and loss == "ingest"
    assert [g["last_stage_reached"] for g in gp] == ["leg", "none"]


def test_no_content_strips_text(runs, tmp_path):
    _t, _ref, v2 = runs
    assemble(v2, tmp_path, "demo", content=False)
    q = rows(tmp_path / "questions.jsonl")[0]
    t = rows(tmp_path / "turns.jsonl")[0]
    assert q["question"] is None and q["context_text"] is None and t["text"] is None
    assert t["record_id"] == "rec-1"


def test_compare_aligns_by_turn_and_query(runs, tmp_path):
    _t, ref, v2 = runs
    man = assemble(ref, tmp_path, "demo", compare_with=v2)
    cmp = json.loads((tmp_path / "compare.json").read_text(encoding="utf-8"))
    assert (tmp_path / "compare_B" / "questions.jsonl").exists()
    assert cmp["a"] == "demo-ref" and cmp["b"] == "demo-v2" and len(cmp["questions"]) == 8
    by = {q["query_id"]: q["delta"] for q in cmp["questions"]}
    assert by["q2"] == "fixed" and by["q8"] == "regressed" and by["q1"] == "both_right"
    assert man["compare"]["questions"]["fixed"] == 5
    d4 = next(t for t in cmp["turns"] if t["turn_id"] == "D1:4")
    assert "derived" in d4["differs"]


def test_cli_writes_bundle(runs, tmp_path, capsys):
    _t, _ref, v2 = runs
    assert main([str(v2), "--data", "demo", "--out", str(tmp_path)]) == 0
    assert "demo-v2" in capsys.readouterr().out and (tmp_path / "manifest.json").exists()


def test_build_inspector_has_view_anchors_and_is_offline(runs, tmp_path):
    _t, ref, v2 = runs
    assemble(v2, tmp_path, "demo", compare_with=ref)
    html = build(tmp_path).read_text(encoding="utf-8")
    for v in ("conversation", "window", "record", "question", "compare", "checks"):
        assert f'id="view-{v}"' in html
    assert not re.search(r"""(src|href)=["']https?://""", html) and "@import" not in html
    assert '"run_id":"demo-v2"' in html
    assert html.count("<script") == 2


def test_embed_escapes_script_end():
    assert "</script>" not in embed({"x": "a</script><!--b"})


def test_demo_flag_builds_published_page(tmp_path):
    p = build_demo(tmp_path / "demo")
    assert p.name == "index.html" and p.stat().st_size > 50_000
    assert "Nono" in p.read_text(encoding="utf-8")
