"""Tests for the PA-051 reference-host timing protocol helper (scripts/perf_protocol.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts.perf_protocol import (
    DirtyTreeError,
    Recorder,
    corpus_messages,
    corpus_queries,
    measure,
    percentile,
    revision,
    summarize,
)

_GIT = ("-c", "user.name=Perf Test", "-c", "user.email=perf@example.invalid")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *_GIT, *args], check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    (path / "a.txt").write_text("a", "utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_percentile_is_nearest_rank() -> None:
    samples = [float(v) for v in range(1, 101)]
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 95) == 95.0
    assert percentile(samples, 99) == 99.0
    assert percentile([3.0], 99) == 3.0
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1.0], 101)


def test_summarize_reports_the_protocol_fields() -> None:
    stats = summarize([1.0, 2.0, 3.0, 4.0], warmups=2)
    assert stats["n"] == 4 and stats["warmups"] == 2
    assert (stats["min_ms"], stats["max_ms"], stats["p50_ms"]) == (1.0, 4.0, 2.0)
    assert {"p95_ms", "p99_ms", "mean_ms", "stdev_ms"} <= stats.keys()
    assert summarize([]) == {"n": 0, "warmups": 0}


def test_measure_runs_warmups_untimed() -> None:
    calls: list[int] = []
    samples = measure(lambda: calls.append(1), reps=5, warmups=3)
    assert len(samples) == 5 and len(calls) == 8
    with pytest.raises(ValueError):
        measure(lambda: None, reps=0, warmups=0)


def test_revision_ignores_only_the_run_records(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")
    out = root / "records" / "perf.json"
    out.parent.mkdir()
    out.write_text("{}", "utf-8")
    assert revision(root, [out.parent])["dirty"] is False
    (root / "b.txt").write_text("b", "utf-8")
    rev = revision(root, [out.parent])
    assert rev["dirty"] is True and rev["dirty_paths"] == ["b.txt"]


def test_recorder_refuses_a_dirty_tree_unless_diagnostic(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r")
    (root / "b.txt").write_text("b", "utf-8")
    out = root / "out" / "perf.json"
    with pytest.raises(DirtyTreeError):
        Recorder("fam", out, root=root, command="x")
    rec = Recorder("fam", out, root=root, command="x", allow_dirty=True)
    rec.record["results"]["k"] = summarize([1.0, 2.0])
    rec.finish()
    data = json.loads(out.read_text("utf-8"))
    assert data["diagnostic_only"] is True
    assert data["revision"]["commit"]
    assert {"cpu_model", "logical_threads", "power_plan", "python", "packages"} <= data[
        "host"
    ].keys()
    assert "load_before" in data and "load_after" in data
    assert data["status"] == "completed"


def test_corpus_is_deterministic_and_varied() -> None:
    assert corpus_messages(50) == corpus_messages(50)
    assert len(set(corpus_messages(200))) > 150
    assert all(q.endswith("?") for q in corpus_queries(20))
