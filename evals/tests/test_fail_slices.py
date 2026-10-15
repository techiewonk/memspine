"""FAIL-SLICE: failure-enriched slices, the screen -> slice map, the queue's per-row override, the reading rule."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import build_targeted_slices as bts
import eval_sample
import make_sample_slice as mss
import pytest
import v2_ledger

EVALS = Path(__file__).resolve().parents[1]
ANALYSIS = EVALS / "analysis"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
ID = re.compile(r"^conv-\d+/\d+-\d+$")
ALLOWED_KEYS = {
    "benchmark",
    "slice_id",
    "conversations",
    "kind",
    "family",
    "source_run",
    "seed",
    "stratify",
    "reading_rule",
    "n_total",
    "n",
    "n_target",
    "n_control",
    "sizes",
    "target_classes",
    "conv_policy",
    "uses_heldout_conversations",
    "dev_conversations_used",
    "heldout_conversations_used",
    "hash",
    "manifest_hash",
    "category",
    "target_ids",
    "target_class",
    "control_ids",
    "query_ids",
}
SPLIT = {"dev_items": ["conv-0", "conv-1"], "heldout_items": ["conv-2", "conv-3"]}


def synthetic(per_cell=40):
    """4 conversations x categories cat1..cat4; ids 'conv-k/k-j'. Every 4th question wrong, with rotating classes."""
    classes = list(bts.FIXABLE) + ["gold_error", "judge_false_negative"]
    order, meta, right, labels = [], {}, set(), {}
    n = 0
    for k in range(4):
        for cat in ("cat1", "cat2", "cat3", "cat4"):
            for j in range(per_cell):
                q = f"conv-{k}/{k}-{n}"
                n += 1
                order.append(q)
                meta[q] = (cat, f"conv-{k}")
                if n % 4 == 0:
                    labels[q] = classes[(n // 4) % len(classes)]
                else:
                    right.add(q)
    return labels, order, meta, right


# ---------------------------------------------------------------- builder
def test_families_are_disjoint_and_exclude_unfixable():
    seen = [c for cls in bts.FAMILIES.values() for c in cls]
    assert len(seen) == len(set(seen))
    assert not set(seen) & set(bts.NEVER_TARGET)
    assert {"gold_error", "judge_false_negative"} <= set(bts.NEVER_TARGET)


def test_builder_is_deterministic_and_seeded():
    labels, order, meta, right = synthetic()
    a = bts.build_all(labels, order, meta, right, SPLIT, seed=5)
    b = bts.build_all(labels, order, meta, right, SPLIT, seed=5)
    c = bts.build_all(labels, order, meta, right, SPLIT, seed=6)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert json.dumps(a, sort_keys=True) != json.dumps(
        c, sort_keys=True
    )  # the control draw moves with the seed
    # the draw does not depend on the input row order
    rev = bts.build_all(
        labels, list(order), dict(reversed(list(meta.items()))), right, SPLIT, seed=5
    )
    assert json.dumps(rev, sort_keys=True) == json.dumps(a, sort_keys=True)


def test_slices_hold_ids_only_and_never_target_unfixable_classes():
    labels, order, meta, right = synthetic()
    for name, s in bts.build_all(labels, order, meta, right, SPLIT, seed=5).items():
        assert set(s) == ALLOWED_KEYS, name
        for key in ("query_ids", "target_ids", "control_ids"):
            assert all(ID.match(q) for q in s[key]), (name, key)
        assert set(s["target_class"].values()) <= set(bts.FIXABLE)
        assert not set(s["target_ids"]) & right and set(s["control_ids"]) <= right
        assert not set(s["target_ids"]) & set(s["control_ids"])
        assert s["hash"] == mss.digest(s["query_ids"]) and s["manifest_hash"] == mss.manifest_hash(
            s["query_ids"]
        )
        assert s["n"] == s["n_target"] + s["n_control"] == len(s["query_ids"])


def test_control_ratio_cap_and_matching():
    labels, order, meta, right = synthetic(per_cell=60)
    out = bts.build_all(labels, order, meta, right, SPLIT, seed=5)
    for name, s in out.items():
        assert s["n_target"] <= bts.CAP_WRONG
        if s["family"] == bts.MIX:
            assert (s["n_target"], s["n_control"]) == (60, 60)
        else:
            assert abs(s["n_control"] / s["n"] - bts.CONTROL_SHARE) <= 0.03, name
        used = set(s["conversations"])
        assert {
            meta[q][1] for q in s["control_ids"]
        } <= used  # controls come from the slice's own conversations
    # a family that fits under the cap keeps every one of its wrong questions
    fusion = out[bts.slice_name("fail_fusion", 5, None)]
    want = {q for q, c in labels.items() if c in bts.FAMILIES["fail_fusion"]}
    assert (
        set(fusion["target_ids"]) == want
        if len(want) <= bts.CAP_WRONG
        else len(fusion["target_ids"]) == bts.CAP_WRONG
    )
    # controls follow the (conversation x category) mix of the targets
    s = out[bts.slice_name("fail_list", 5, None)]
    tc = {(meta[q][1], meta[q][0]) for q in s["target_ids"]}
    cc = {(meta[q][1], meta[q][0]) for q in s["control_ids"]}
    assert len(cc & tc) >= len(cc) * 0.6


def test_c3_variant_keeps_three_conversations_with_most_failures():
    labels, order, meta, right = synthetic()
    s = bts.build_all(labels, order, meta, right, SPLIT, seed=5)[
        bts.slice_name("fail_reader", 5, 3)
    ]
    assert len(s["conversations"]) <= 3 and s["slice_id"].endswith("-c3")


def test_heldout_flag_is_written():
    labels, order, meta, right = synthetic()
    for s in bts.build_all(labels, order, meta, right, SPLIT, seed=5).values():
        used = set(s["conversations"])
        assert s["uses_heldout_conversations"] == bool(used & set(SPLIT["heldout_items"]))
        assert set(s["heldout_conversations_used"]) | set(s["dev_conversations_used"]) == used


# ---------------------------------------------------------------- committed files
def committed():
    return sorted(ANALYSIS.glob("fail_slice_*_s20261013*.json"))


def test_committed_slices_match_the_label_file():
    labels = bts.read_labels(EVALS / bts.LABELS)
    files = committed()
    assert len(files) == 12  # 6 slices x (all conversations, c3)
    for f in files:
        s = json.loads(f.read_text(encoding="utf-8"))
        assert (
            set(s) == ALLOWED_KEYS
            and s["kind"] == "targeted"
            and f.name
            == bts.slice_name(s["family"], s["seed"], 3 if s["slice_id"].endswith("-c3") else None)
        )
        assert all(ID.match(q) for q in s["query_ids"]) and len(set(s["query_ids"])) == s["n"]
        assert s["hash"] == mss.digest(s["query_ids"])
        fam = bts.FAMILIES.get(s["family"], bts.FIXABLE)
        assert all(
            labels[q] in fam and labels[q] == s["target_class"][q] for q in s["target_ids"]
        )  # wrong and fixable
        assert not set(s["control_ids"]) & set(
            labels
        )  # controls are never a labelled-wrong question
        assert not {"gold_error", "judge_false_negative"} & set(s["target_class"].values())
        assert s["n_target"] <= 60
        ratio = s["n_control"] / s["n"]
        assert abs(ratio - (0.5 if s["family"] == bts.MIX else 0.4)) <= 0.05, (f.name, ratio)
        split = json.loads((EVALS / bts.SPLIT).read_text(encoding="utf-8"))
        assert s["uses_heldout_conversations"] == bool(
            set(s["conversations"]) & set(split["heldout_items"])
        )


def test_committed_slices_are_current_when_the_run_is_available():
    try:
        runs = bts.find_runs_dir()
    except SystemExit:
        pytest.skip("reference run results.jsonl not available here")
    labels = bts.read_labels(EVALS / bts.LABELS)
    order, meta, right = bts.read_universe(runs)
    split = json.loads((EVALS / bts.SPLIT).read_text(encoding="utf-8"))
    for name, s in bts.build_all(labels, order, meta, right, split).items():
        assert json.loads((ANALYSIS / name).read_text(encoding="utf-8")) == s, name


# ---------------------------------------------------------------- the screen map
def test_map_matches_the_derivation_and_points_at_real_slices():
    queue = bts.read_queue(EVALS / "v2_queue.tsv")
    smap = json.loads((EVALS / bts.SOLUTION_MAP).read_text(encoding="utf-8"))
    rows = bts.build_map(queue, smap)
    assert (EVALS / "v2_slice_map.tsv").read_text(encoding="utf-8") == bts.render_map(rows)
    ids = {q["id"] for q in queue}
    for sid, st, sl, slg, *_ in rows:
        assert sid in ids and st == "loc" and (EVALS / sl).exists() and (EVALS / slg).exists()
    by = {}
    for r in rows:
        if Path(r[2]).name.startswith(
            "fail_slice_"
        ):  # the category-5 rows (S20-CAT5) are tested in test_cat5_slice.py
            by.setdefault(r[0], []).append(Path(r[2]).name)
    fam = lambda sid: {re.sub(r"fail_slice_|_s20261013\.json", "", n) for n in by[sid]}
    assert fam("S34") == fam("S35") == fam("S40") == {"fail_list"}
    assert fam("S60") == fam("S67") == fam("S14") == fam("S51") == {"fail_fusion", "fail_mix_120"}
    assert (
        "fail_retrieval" in fam("S55")
        and "fail_retrieval" in fam("S56")
        and fam("S61") == {"fail_retrieval"}
    )
    assert fam("S62") == {"fail_temporal"} and fam("S63") == {"fail_reader"}
    # parity / reference / report rows and screens without target classes keep the random sample slice
    for sid in ("S05", "S09", "S25", "S26", "S29", "S47", "S65", "S66", "S41", "S42"):
        assert sid not in by


# ---------------------------------------------------------------- queue override (dry run: no GPU, no state)
def dry(only, tmp_path, **env_extra):
    env = {**os.environ, "PINNED_DRY_RUN": "1", "V2_LEDGER_DIR": str(tmp_path), **env_extra}
    env.pop("MODE", None)
    p = subprocess.run(
        [BASH, str(EVALS / "run_v2_queue.sh"), "--dry-run", "--only", only],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=EVALS,
    )
    assert p.returncode == 0, p.stderr
    return p.stdout


@needs_bash
def test_queue_row_picks_its_targeted_slice(tmp_path):
    out = dry(
        "S34", tmp_path
    )  # kind GC: the c3 variant of fail_list; its OP-Bench set keeps the random OP-Bench slice
    assert (
        "--query-ids @analysis/fail_slice_fail_list_s20261013_c3.json" in out
        and "targeted fail_list" in out
    )
    assert (
        "smp-S34-tgt-fail_list-" in out
        and "sample_slice_loc_60" not in out
        and "sample_slice_opb_60" in out
    )
    assert (
        "--slice analysis/fail_slice_fail_list_s20261013_c3.json" in out
    )  # eval_sample reads the same file
    out = dry(
        "S14", tmp_path
    )  # kind J, two slices: fail_fusion + fail_mix_120, each with its own reference
    assert (
        "fail_slice_fail_fusion_s20261013.json" in out
        and "fail_slice_fail_mix_120_s20261013.json" in out
    )
    assert out.count("building reference") == 2 and out.count("eval_sample.py") == 2
    assert "sample_slice_loc_80" not in out


@needs_bash
def test_queue_keeps_old_behaviour_without_a_mapping(tmp_path):
    for sid, frag in (("S05", "sample_slice_loc_80"), ("S34", "sample_slice_loc_60_s20261013_c3")):
        out = dry(sid, tmp_path, **({"V2_SLICE_MAP": ""} if sid == "S34" else {}))
        assert (
            frag in out and "targeted" not in out and "fail_slice" not in out and "-tgt-" not in out
        )
    out = dry("S10", tmp_path, V2_SLICE_MAP=str(tmp_path / "does_not_exist.tsv"))
    assert "sample_slice_loc_80" in out and "fail_slice" not in out


@needs_bash
def test_queue_reads_a_custom_map_file(tmp_path):
    m = tmp_path / "map.tsv"
    m.write_text(
        "# test\nS10\tloc\tanalysis/fail_slice_fail_list_s20261013.json\tanalysis/fail_slice_fail_list_s20261013_c3.json\tnote\n",
        encoding="utf-8",
    )
    out = dry("S10", tmp_path, V2_SLICE_MAP=str(m))
    assert "fail_slice_fail_list_s20261013.json" in out and "fail_slice_fail_fusion" not in out


# ---------------------------------------------------------------- reading rule
def slice_obj():
    return {
        "family": "fail_list",
        "target_ids": ["a", "b", "c", "d"],
        "control_ids": ["x", "y", "z"],
        "target_class": {
            "a": "count_wrong",
            "b": "count_wrong",
            "c": "list_incomplete",
            "d": "list_incomplete",
        },
    }


def test_target_report_counts_recovered_and_lost():
    ref = {
        "a": 0.0,
        "b": 0.0,
        "c": 0.0,
        "d": 1.0,
        "x": 1.0,
        "y": 1.0,
        "z": 0.0,
    }  # d is already right in this reference
    arm = {"a": 1.0, "b": 0.0, "c": 1.0, "d": 1.0, "x": 0.0, "y": 1.0, "z": 1.0}
    r = eval_sample.target_report(slice_obj(), arm, ref)
    assert r["tgt_rec"] == "2/3" and r["ctl_lost"] == "1/2"
    assert r["by_class"] == {"count_wrong": (1, 2), "list_incomplete": (1, 1)}
    assert (
        eval_sample.target_report({"query_ids": ["a"]}, arm, ref) is None
    )  # random sample slices: nothing extra


def test_ledger_shows_tgt_rec_and_ctl_lost(tmp_path, capsys):
    path = tmp_path / "v2_results.tsv"
    base = {
        "mode": "sample",
        "screen": "S34",
        "arm": "a",
        "n": 100,
        "wins": 3,
        "losses": 1,
        "ties": 96,
        "net": 2,
        "delta": "2.0",
    }
    v2_ledger.append({**base, "tgt_rec": "5/30", "ctl_lost": "1/40"}, path)
    assert v2_ledger.read(path)[-1]["tgt_rec"] == "5/30"
    os.environ["V2_LEDGER_DIR"] = str(tmp_path)
    try:
        assert v2_ledger.main(["summary", "--mode", "sample"]) == 0
    finally:
        del os.environ["V2_LEDGER_DIR"]
    out = capsys.readouterr().out
    assert re.search(r"tgt_rec\s+5/30 ctl_lost\s+1/40", out) and "NOT overall accuracy" in out


def test_ledger_with_an_old_header_folds_the_new_columns_into_notes(tmp_path):
    path = tmp_path / "v2_results.tsv"
    old = v2_ledger.COLUMNS[:-2]
    path.write_text(
        "\t".join(old) + "\n" + "\t".join("x" if c != "screen" else "S01" for c in old) + "\n",
        encoding="utf-8",
    )
    v2_ledger.append(
        {"mode": "sample", "screen": "S34", "arm": "a", "tgt_rec": "2/9", "ctl_lost": "0/6"}, path
    )
    rows = v2_ledger.read(path)
    assert list(rows[-1]) == list(old) and "tgt_rec=2/9" in rows[-1]["notes"]
    assert (
        v2_ledger._tgt(rows[-1], "tgt_rec") == "2/9"
        and v2_ledger._tgt(rows[-1], "ctl_lost") == "0/6"
    )
