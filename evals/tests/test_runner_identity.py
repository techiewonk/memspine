"""P0-01: the runner writes manifest identity on line 1 and the cost block as a trailing record. Offline, synthetic."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.judge import ExactMatchJudge
from memspine_evals.manifest import comparability_key
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import ScriptedReader
from memspine_evals.results import read_run
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import NaiveRAGSystem, VerbatimSystem

PROTOCOL = RunProtocol(protocol_id="smoke", budget_tokens=400, top_k=5, seed=11)


def do_run(system, tmp_path: Path, run_id: str, **cfg):
    dataset = SyntheticDataset(n_items=2, turns_per_item=12, facts_per_item=2)
    reader = ScriptedReader({q.text: (q.gold or "") for it in dataset.items() for q in it.queries})
    config = RunConfig(run_id=run_id, protocol=PROTOCOL, out_dir=tmp_path, expect_model_calls=False, **cfg)
    asyncio.run(EvalRunner(dataset, system, reader, ExactMatchJudge(), config).run())
    return tmp_path / run_id


def test_identity_is_populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMSPINE_PINNED_COMMIT", "abc123")
    monkeypatch.setenv("MEMSPINE_ENV_HASH", "env1")
    monkeypatch.setenv("MEMSPINE_VENV_HASH", "venv1")
    out = do_run(VerbatimSystem(), tmp_path, "r1", extra_limits={"item_ids": ["a", "b"]})
    manifest, _rows, _summary = read_run(out / "results.jsonl")
    ident = manifest["identity"]
    assert (ident["commit"], ident["env_hash"], ident["venv_hash"]) == ("abc123", "env1", "venv1")
    assert ident["flags"]["system_id"]
    assert len(ident["flags_hash"]) == 16
    assert ident["models"]["reader"] and ident["models"]["embedder"] is None
    d = ident["datasets"]
    assert d["dataset_id"] == manifest["dataset"]["dataset_id"]
    assert d["slice_hash"] and d["slice_id"] == "items:2"
    assert d["files"] == {manifest["dataset"]["source_path"]: manifest["dataset"]["content_sha256"]}
    assert ident["harness_version"] == manifest["harness_version"]


def test_flags_differ_between_arms_but_comparability_key_does_not(tmp_path: Path) -> None:
    a, _, _ = read_run(do_run(VerbatimSystem(), tmp_path, "a") / "results.jsonl")
    b, _, _ = read_run(do_run(NaiveRAGSystem(), tmp_path, "b") / "results.jsonl")
    assert a["identity"]["flags_hash"] != b["identity"]["flags_hash"]
    assert comparability_key(a) == comparability_key(b)


def test_cost_is_a_trailing_record_and_old_readers_still_work(tmp_path: Path) -> None:
    out = do_run(VerbatimSystem(), tmp_path, "c")
    lines = [json.loads(x) for x in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "manifest" and lines[-1]["kind"] == "cost"
    assert [x["kind"] for x in lines].count("summary") == 1
    cost = lines[-1]
    assert cost["duration_s"] is not None and cost["latency_p50_s"] is not None
    assert cost["tokens"]["reader"]["prompt"] >= 0
    assert cost["rerank_calls"] is None  # no reranker configured: unknown, not zero
    manifest, rows, summary = read_run(out / "results.jsonl")  # unchanged reader ignores the new kind
    assert rows and summary is not None
    assert manifest["identity"]["cost"]["duration_s"] is None  # line 1 is never rewritten
    sj = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert sj["manifest"]["identity"]["cost"]["duration_s"] == cost["duration_s"]
    assert comparability_key(manifest) == comparability_key({**manifest, "identity": sj["manifest"]["identity"]})
