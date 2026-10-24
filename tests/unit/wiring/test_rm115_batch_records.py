"""RM-115: the RM-88 caller check reads the admitted edges' source records in one batch
(``get_records``) instead of one ``get_record`` per edge, with identical results.

Proved on the RM-93 world (leg edges and ``Engine.search``) and on the RM-88 grant and
record-viewer worlds; a storage without the batch method still works one id at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import test_rm88_rm91_graph_caller as caller_tests
from test_rm93_graph_filter import NS, QUERY, _build, _engine

from memspine.derive.pipeline.stages.edge_compiler import compile_graph_edges
from memspine.read.pipeline import legs as pipeline_legs
from memspine.services.graph.qualifiers import (
    AssertionModality,
    ClaimState,
    QualifierFilter,
    graph_filter_scope,
)
from memspine.store.records import MemoryRecord

LATER = datetime.now(UTC) + timedelta(days=1)

FILTERS: list[QualifierFilter] = [
    QualifierFilter(assertion_modalities=frozenset({AssertionModality.ASSERTED})),
    QualifierFilter(known_as_of=LATER),
    QualifierFilter(claim_states=frozenset(ClaimState)),
]


class _PerIdStorage:
    """Only ``get_record`` (the pre-RM-115 read), counting calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        self.calls += 1
        result: MemoryRecord | None = await self._inner.get_record(record_id)
        return result


class _CountingStorage(_PerIdStorage):
    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self.batches: list[list[str]] = []

    async def get_records(self, record_ids: list[str]) -> dict[str, MemoryRecord]:
        self.batches.append(list(record_ids))
        result: dict[str, MemoryRecord] = await self._inner.get_records(record_ids)
        return result


def _key(edges: list[Any]) -> list[tuple[str, str, str, str]]:
    return [(e.src, e.dst, e.rel_type, str(e.properties.get("record_id"))) for e in edges]


@pytest.mark.parametrize("flt", FILTERS, ids=[f"f{i}" for i in range(len(FILTERS))])
@pytest.mark.parametrize("frontier", [True, False], ids=["seeds", "namespace"])
async def test_batch_lookup_matches_per_id_lookup(flt: QualifierFilter, frontier: bool) -> None:
    eng = await _engine()
    try:
        await _build(eng)
        seeds = list(await eng._graph_seeds(NS, QUERY))
        batch = _CountingStorage(eng._storage)
        per_id = _PerIdStorage(eng._storage)
        results = {}
        for name, storage in (("batch", batch), ("per_id", per_id)):
            view = SimpleNamespace(_graph=eng._graph, _shared=eng._shared, _storage=storage)
            with graph_filter_scope(flt):
                results[name] = await pipeline_legs._graph_leg_edges(
                    view, NS, seeds, {}, flt, use_frontier=frontier
                )
    finally:
        await eng.stop()
    a, b = results["batch"], results["per_id"]
    assert _key(a.edges) == _key(b.edges)
    assert (a.compiled, a.hidden, a.scope) == (b.compiled, b.hidden, b.scope)
    assert batch.calls == 0  # no per-edge read
    distinct = {str(e.properties.get("record_id")) for e in a.edges}
    if per_id.calls:  # something was admitted by the filter
        assert len(batch.batches) == 1 and len(batch.batches[0]) == per_id.calls
        assert distinct <= set(batch.batches[0])


async def test_records_in_the_read_are_not_fetched() -> None:
    eng = await _engine()
    flt = QualifierFilter(known_as_of=LATER)
    try:
        await _build(eng)
        seeds = list(await eng._graph_seeds(NS, QUERY))
        by_id = {r.record_id: r for r in await eng._storage.list_records(NS)}
        storage = _CountingStorage(eng._storage)
        view = SimpleNamespace(_graph=eng._graph, _shared=eng._shared, _storage=storage)
        with graph_filter_scope(flt):
            selected = await pipeline_legs._graph_leg_edges(view, NS, seeds, by_id, flt)
    finally:
        await eng.stop()
    assert selected.edges
    assert storage.batches == [] and storage.calls == 0


async def test_search_identical_with_batch_and_per_id_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flt = QualifierFilter(
        assertion_modalities=frozenset({AssertionModality.ASSERTED}), known_as_of=LATER
    )
    real = pipeline_legs._fetch_records

    async def per_id(storage: Any, record_ids: Any) -> dict[str, MemoryRecord]:
        return dict(await real(_PerIdStorage(storage), record_ids))

    eng = await _engine()
    runs: list[list[Any]] = []
    try:
        await _build(eng)
        for fetch in (real, per_id, real):
            monkeypatch.setattr(pipeline_legs, "_fetch_records", fetch)
            hits = await eng.search(QUERY, namespace=NS, top_k=8, graph_filter=flt)
            runs.append([(rec.record_id, round(score, 6)) for rec, score in hits])
    finally:
        await eng.stop()
    assert runs[0] and runs[0] == runs[1] == runs[2]


async def test_grant_and_viewer_layers_unchanged_with_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RM-88 both layers on the batch path: another scope's edge needs a grant, and a
    participants-only source record hides its edges from an outsider."""
    eng = await caller_tests._engine()
    spy = caller_tests._Spy(monkeypatch)
    try:
        private = await caller_tests._fact(
            eng,
            "a",
            "Ilse",
            "knows",
            "Bram",
            "Ilse introduced Bram to the chess club",
            participants=["Ilse", "Tomasz"],
            visibility="participants",
        )
        await caller_tests._fact(
            eng, "a", "Tomasz", "employs", "Bram", "Tomasz hired Bram as a carpenter"
        )
        await compile_graph_edges(eng._pipeline_ctx())
        assert eng._graph is not None
        private_edges = sum(
            1
            for e in await eng._graph.frontier_edges(["ent:a:ilse", "ent:a:tomasz"], namespace="a")
            if e.properties.get("record_id") == private
        )
        flt = caller_tests._flt()
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=flt, viewer="Ilse")
        member = spy.last()
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=flt, viewer="Dana")
        outsider = spy.last()
    finally:
        await eng.stop()
    assert private_edges > 0
    assert member["caller_hidden"] == 0 and outsider["caller_hidden"] == private_edges


async def test_sql_storage_get_records_round_trip() -> None:
    eng = await _engine()
    try:
        await _build(eng)
        live = await eng._storage.list_records(NS)
        ids = [r.record_id for r in live] + ["missing", live[0].record_id]
        batch = await eng._storage.get_records(ids)
        single = {r.record_id: await eng._storage.get_record(r.record_id) for r in live}
    finally:
        await eng.stop()
    assert set(batch) == set(single)
    assert all(
        batch[k].model_dump(mode="json") == v.model_dump(mode="json")
        for k, v in single.items()
        if v
    )
