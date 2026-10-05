"""Smoke test for the KB-8 graph benchmark (#24): a 200-edge graph end to end."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import bench_graph


def test_synthetic_graph_is_canonical_and_sized() -> None:
    edges = bench_graph.synthetic_graph(200, seed=3)
    assert len(edges) == 200
    assert edges == sorted(edges)
    assert all(a < b and 0.3 <= w <= 1.0 for a, b, w in edges)
    assert bench_graph.synthetic_graph(200, seed=3) == edges


def test_bench_size_reports_every_measurement() -> None:
    report = asyncio.run(bench_graph.bench_size(200, queries=3, ppr_queries=2, ladybug=False))
    sqlite = report["stores"]["sqlite_adjacency"]
    assert set(sqlite["bfs"]) == {"depth_1", "depth_2", "depth_3"}
    assert sqlite["bfs"]["depth_1"]["n"] == 3
    assert report["ppr"]["n"] == 2
    assert report["communities"]["lpa"]["order_independent"] is True


def test_main_writes_json_and_markdown(tmp_path: Path) -> None:
    out = tmp_path / "bench"
    args = ["--sizes", "200", "--queries", "2", "--ppr-queries", "1", "--no-ladybug"]
    assert bench_graph.main([*args, "--out", str(out)]) == 0
    report = json.loads((out / "bench_graph.json").read_text(encoding="utf-8"))
    assert report["sizes"][0]["edges"] == 200
    assert "| 200 |" in (out / "bench_graph.md").read_text(encoding="utf-8")
