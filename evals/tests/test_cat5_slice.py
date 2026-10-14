"""S20-CAT5: LoCoMo category-5 (adversarial) slices, the slice-map `categories=5` column and the queue's --categories pass-through."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import build_targeted_slices as bts
import make_cat5_slice as m5
import make_sample_slice as mss
from memspine_evals.manifest import build_identity

EVALS = Path(__file__).resolve().parents[1]
ANALYSIS = EVALS / "analysis"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
ID = re.compile(r"^conv-\d+/\d+-\d+$")
FULL, C3 = ANALYSIS / "sample_slice_loc_cat5_s20261013.json", ANALYSIS / "sample_slice_loc_cat5_s20261013_c3.json"


def synthetic():
    """4 conversations with 10..40 category-5 questions among other categories; (order, conv, total)."""
    order, conv, total = [], {}, 0
    for k, n5 in enumerate((10, 20, 30, 40)):
        for j in range(n5 + 15):
            total += 1
            if j < n5:
                q = f"conv-{k}/{k}-{j}"
                order.append(q)
                conv[q] = f"conv-{k}"
    return order, conv, total


# ---------------------------------------------------------------- builder
def test_sample_is_deterministic_stratified_and_exact():
    order, conv, _ = synthetic()
    a = m5.sample(order, conv, 20, seed=3)
    assert sorted(a) == sorted(m5.sample(list(reversed(order)), conv, 20, seed=3))
    assert len(a) == len(set(a)) == 20 and a != m5.sample(order, conv, 20, seed=4)
    per = {c: sum(1 for q in a if conv[q] == c) for c in set(conv.values())}
    assert per == {"conv-0": 2, "conv-1": 4, "conv-2": 6, "conv-3": 8}  # proportional to the cat-5 population
    assert {conv[q] for q in m5.sample(order, conv, 6, seed=1, convs=["conv-1", "conv-2"])} == {"conv-1", "conv-2"}
    assert len(m5.sample(order, conv, 10_000, seed=1)) == len(order)  # capped at the population


def test_build_records_categories_and_hashes_without_text():
    order, conv, total = synthetic()
    sl = m5.build(order, conv, total, 20, seed=3)
    assert sl["categories"] == [5] and sl["categories_arg"] == "5" and sl["family"] == "cat5" and sl["sizes"] == {"cat5": 20}
    assert set(sl["category"].values()) == {"cat5"} and sl["n_total"] == total and sl["n_category"] == len(order)
    assert sl["hash"] == mss.digest(sl["query_ids"])
    ident = build_identity(slice_id=sl["slice_id"], slice_ids=sl["query_ids"], environ={})
    assert ident["datasets"]["slice_hash"] == sl["manifest_hash"]


# ---------------------------------------------------------------- committed files
@pytest.mark.parametrize("path,n,convs", [(FULL, 80, 10), (C3, 30, 3)])
def test_committed_cat5_slices(path, n, convs):
    s = json.loads(path.read_text(encoding="utf-8"))
    assert s["n"] == n == len(s["query_ids"]) == len(set(s["query_ids"])) and len(s["conversations"]) == convs
    assert all(ID.match(q) for q in s["query_ids"]) and s["seed"] == 20261013
    assert s["categories"] == [5] and set(s["category"].values()) == {"cat5"} and s["hash"] == mss.digest(s["query_ids"])
    assert "question" not in json.dumps(s).lower().replace("questions", "")  # ids and counts only, no benchmark text
    assert sum(s["per_conversation"].values()) == n
    c3_convs = json.loads((ANALYSIS / "sample_slice_loc_60_s20261013_c3.json").read_text(encoding="utf-8"))["conversations"]
    if convs == 3:
        assert s["conversations"] == c3_convs


def test_committed_cat5_slices_match_the_dataset_when_available():
    try:
        data = m5.find_data()
    except SystemExit:
        pytest.skip("locomo10.json not available here")
    order, conv, total = m5.read_universe(data)
    assert len(order) == 446
    c3 = json.loads((ANALYSIS / "sample_slice_loc_60_s20261013_c3.json").read_text(encoding="utf-8"))["conversations"]
    assert json.loads(FULL.read_text(encoding="utf-8")) == m5.build(order, conv, total, 80, m5.SEED)
    assert json.loads(C3.read_text(encoding="utf-8")) == m5.build(order, conv, total, 30, m5.SEED, c3)
    # every id is a real category-5 question, and the harness gives it the refusal gold
    from memspine_evals.datasets.locomo import LoCoMoDataset
    from memspine_evals.judge import ABSTENTION_GOLD

    qs = {f"{it.item_id}/{q.query_id}": q for it in LoCoMoDataset(data, "t", categories=(5,)).items() for q in it.queries}
    for p in (FULL, C3):
        for q in json.loads(p.read_text(encoding="utf-8"))["query_ids"]:
            assert qs[q].type_label == "cat5" and qs[q].gold == ABSTENTION_GOLD


# ---------------------------------------------------------------- the map
def test_map_rows_for_the_relevance_screens():
    rows = [ln.split("\t") for ln in (EVALS / "v2_slice_map.tsv").read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    cat5 = {r[0]: r for r in rows if len(r) > 5}
    assert set(cat5) == set(bts.CAT5_SCREENS) == {"S20", "S55", "S63", "S73", "S74"}
    for r in cat5.values():
        assert r[5] == "categories=5" and r[2].endswith("sample_slice_loc_cat5_s20261013.json") and r[3].endswith("_c3.json")
    assert all(len(r) == 5 for r in rows if r[0] not in cat5 or r[2].startswith("analysis/fail_slice"))  # other rows: no sixth column
    s20 = [r[2] for r in rows if r[0] == "S20"]
    assert s20 == ["analysis/sample_slice_loc_cat5_s20261013.json", "analysis/fail_slice_fail_mix_120_s20261013.json"]
    assert [r[0] for r in rows].count("S20") == 2  # OP-Bench stays the unmapped random sample slice


# ---------------------------------------------------------------- queue (dry run: no GPU, no state)
def dry(only, tmp_path, **env_extra):
    env = {**os.environ, "PINNED_DRY_RUN": "1", "V2_LEDGER_DIR": str(tmp_path), **env_extra}
    env.pop("MODE", None)
    p = subprocess.run([BASH, str(EVALS / "run_v2_queue.sh"), "--dry-run", "--only", only], capture_output=True, text=True,
                       env=env, timeout=120, cwd=EVALS)
    assert p.returncode == 0, p.stderr
    return p.stdout


def run_lines(out):
    return [ln for ln in out.splitlines() if "run.sh --arm" in ln]


@needs_bash
def test_s20_dry_run_has_cat5_mix_and_opbench_rows(tmp_path):
    out = dry("S20", tmp_path)
    runs = run_lines(out)
    cat5 = [ln for ln in runs if "sample_slice_loc_cat5" in ln]
    assert len(cat5) == 2  # the reference and the arm
    assert all("--categories 5" in ln and "--questions 80" in ln for ln in cat5)
    ref, arm = cat5
    assert "--run-id smp-ref-scr-pool-protect-jina-" in ref and "-sample-loc-cat5-80-s20261013-cat5 " in ref  # distinct cached name
    assert "--arm v2-R3-11-gate_v2" in arm and "smp-S20-tgt-cat5-" in arm
    assert "--ref smp-ref-scr-pool-protect-jina-" in out and "-cat5 --set loc --slice analysis/sample_slice_loc_cat5_s20261013.json" in out
    mix = [ln for ln in runs if "fail_slice_fail_mix_120" in ln]
    opb = [ln for ln in runs if "op_bench" in ln]
    assert len(mix) == 2 and len(opb) == 2 and "sample_slice_opb_60" in opb[0]
    assert not any("--categories" in ln for ln in mix + opb)  # categories 1-4 default, and OP-Bench has no categories
    assert out.count("eval_sample.py") == 3 and "run_blind_validation" not in out


@needs_bash
def test_cat5_reference_never_collides_with_the_default_reference(tmp_path):
    out = dry("S20,S55", tmp_path)
    refs = {m.group(1) for m in re.finditer(r"--run-id (smp-ref-\S+)", out)}
    assert any(r.endswith("-cat5") for r in refs) and all(r.endswith("-cat5") == ("cat5-80" in r) for r in refs)


@needs_bash
def test_queue_without_the_column_keeps_old_behaviour(tmp_path):
    m = tmp_path / "map.tsv"
    m.write_text("S20\tloc\tanalysis/fail_slice_fail_list_s20261013.json\tanalysis/fail_slice_fail_list_s20261013_c3.json\tnote\n",
                 encoding="utf-8")
    out = dry("S20", tmp_path, V2_SLICE_MAP=str(m))
    assert "--categories" not in out and "-cat5" not in out and "fail_slice_fail_list_s20261013.json" in out
    out = dry("S20", tmp_path, V2_SLICE_MAP="")
    assert "--categories" not in out and "sample_slice_loc_80" in out
