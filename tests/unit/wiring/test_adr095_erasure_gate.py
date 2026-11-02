"""ADR-095 erasure gate (RM-135): one test per copy class, part of the phase C local gate.

Each test writes a record carrying distinctive names, hard-erases it (and soft-forgets where the
semantics differ), then proves no copy remains. "No copy" means, for the erased record:

* the raw event log and every row of every SQL table hold none of its content words, names,
  tags or content fingerprints (record ids MAY remain: FORGET events, tombstones and erasure
  manifests carry ids by design, never content);
* every file the store owns (the SQLite file, the LanceDB table, the Tantivy index, the
  LadybugDB file, the disk cache), read as raw bytes after the engine stopped, holds none of
  them, and the store's own API (versions, term statistics, graph queries) finds none of them;
* in-process caches hold no entry keyed on or holding the erased text.

Logs and traces (``test_logs_and_traces``): structlog output and tap traces are only checked for
what the engine itself emits and keeps. The gate requires that no log event and no tap trace,
before or after the erasure, carries the content (ids only) under the default tap settings;
whatever an operator's log shipper copied before the erasure is the operator's duty. With the
inspector's content opt-in (``observability.inspect_content``) traces hold masked content; the
hard erase drops every trace naming the erased id from the engine-owned sinks (memory, JSONL,
SQLite). The ``log`` and ``otel`` sinks hand traces to the operator's pipeline and keep no copy
in the engine, so they are the operator's duty like any log shipper.

An unsupported copy class fails the gate: ``COPY_CLASSES`` maps every ADR-095 class to its test,
and ``test_every_copy_class_has_a_gate_test`` fails when a class has none. No test here skips.
Select the gate with ``-m erasure_gate``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import lancedb
import orjson
import pytest
import structlog
import tantivy
import xxhash
import zstandard
from sqlalchemy import MetaData, Table, inspect, select, text

from memspine import Engine
from memspine.providers.registry import get_registry
from memspine.store.records import RecordStatus
from memspine.trust.erasure import retained_fields

pytestmark = pytest.mark.erasure_gate

NS = "a"
SECRET = "Zebulon Quartz lives in Vrbnik near the Quixotelle lighthouse"
NEEDLES = ["zebulon", "quartz", "vrbnik", "quixotelle"]
KEPT = "Dana bakes bread"

#: ADR-095 (with its amendment): copy class -> (gate module, test function). A class with no
#: entry, or an entry naming a missing test, fails ``test_every_copy_class_has_a_gate_test``.
COPY_CLASSES: dict[str, tuple[str, str]] = {
    "records": ("test_adr095_erasure_gate", "test_records"),
    "event payload redaction": ("test_adr095_erasure_gate", "test_event_payload_redaction"),
    "vector projection": ("test_adr095_erasure_gate", "test_vector_projection"),
    "lexical projection": ("test_adr095_erasure_gate", "test_lexical_projection_tantivy"),
    "lexical projection (fts5)": ("test_adr095_erasure_gate", "test_lexical_projection_fts5"),
    "graph projection": ("test_adr095_erasure_gate", "test_graph_projection"),
    "mention projection": ("test_adr095_erasure_gate", "test_mention_projection"),
    "caches (kv)": ("test_adr095_erasure_gate", "test_kv_caches"),
    "caches (read results and memos)": ("test_adr095_erasure_gate", "test_read_side_caches"),
    "cached query vectors": ("test_adr095_erasure_gate", "test_cached_query_vectors"),
    "resolver name caches": ("test_adr095_window_copy_classes", "test_resolver_name_caches"),
    "logs and traces": ("test_adr095_erasure_gate", "test_logs_and_traces"),
    "restored backups": ("test_adr095_erasure_gate", "test_restored_backup_re_hides"),
    "truncated packed excerpts": ("test_adr095_erasure_gate", "test_truncated_packed_excerpts"),
    "entity name index": (
        "test_adr095_identity_copy_classes",
        "test_hard_erase_leaves_no_entity_name_or_name_fingerprint",
    ),
    "alias text": (
        "test_adr095_identity_copy_classes",
        "test_hard_erase_of_alias_evidence_scrubs_the_alias",
    ),
    "message metadata": (
        "test_adr095_identity_copy_classes",
        "test_hard_erase_of_a_message_leaves_no_metadata_sender_name_or_input_hash",
    ),
    "copied excerpts": ("test_adr095_window_copy_classes", "test_copied_excerpts"),
    "unresolved annotations": (
        "test_adr095_window_copy_classes",
        "test_unresolved_annotations",
    ),
    "window drafts": ("test_adr095_window_copy_classes", "test_window_drafts"),
    "known-entity cache": ("test_adr095_window_copy_classes", "test_known_entity_cache"),
    "prepass mention cache": (
        "test_adr095_window_copy_classes",
        "test_prepass_mention_cache",
    ),
    "in-flight background work": (
        "test_adr095_window_copy_classes",
        "test_forget_during_in_flight_window_work",
    ),
}


# ---------------------------------------------------------------- helpers


def _kwargs(
    root: Path,
    *,
    graph: str | None = "sql_adjacency",
    embed: str = "hash",
    cache: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    storage: dict[str, Any] = {
        "records": {"provider": "sqlite", "settings": {"path": str(root / "m.db")}},
        "vector": {"provider": "lancedb"},
        "lexical": {"provider": "tantivy"},
    }
    memories: dict[str, Any] = {"episodic": {"enabled": True}, "semantic": {"enabled": True}}
    if graph is not None:
        storage["graph"] = {"provider": graph}
        memories["associative"] = {"enabled": True, "policies": {"entity_nodes": True}}
    if cache is not None:
        storage["cache"] = cache
    return {
        "template": "core",
        "dotenv_path": None,
        "storage": storage,
        "model": {"embedding": {"provider": embed, "settings": {}}},
        "memories": memories,
        "read": {"hybrid": True, "record_access": False, **extra.pop("read", {})},
        **extra,
    }


async def _start(root: Path, **options: Any) -> Engine:
    eng = Engine(**_kwargs(root, **options))
    await eng.start()
    return eng


async def _seed(eng: Engine) -> tuple[Any, Any]:
    gone = await eng.write(
        SECRET, namespace=NS, entity="Zebulon Quartz", tags=["dst:Vrbnik", "entity_type:person"]
    )
    kept = await eng.write(KEPT, namespace=NS, entity="Dana")
    return gone, kept


def _text(value: Any) -> str:
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        try:
            raw = zstandard.ZstdDecompressor().decompress(raw)
        except zstandard.ZstdError:
            pass
        return raw.decode("utf-8", errors="replace")
    return str(value)


async def _sql_scan(eng: Engine) -> str:
    """The decoded event log plus every row of every table, lower-cased."""
    assert eng._storage is not None and eng._sql is not None
    parts = [orjson.dumps(e.payload).decode() for e in await eng._storage.read_events(0, 10**6)]
    async with eng._sql.engine.connect() as conn:
        names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        meta = MetaData()
        tables = await conn.run_sync(
            lambda sync: {n: Table(n, meta, autoload_with=sync) for n in names}
        )
        for name, table in tables.items():
            for row in (await conn.execute(select(table))).all():
                parts.append(name + ": " + " | ".join(_text(v) for v in row))
    return "\n".join(parts).lower()


def _file_hits(
    root: Path, needles: list[str], *, skip: tuple[str, ...] = ()
) -> dict[str, list[str]]:
    """Raw-byte scan of every file under ``root`` (call after ``stop``; nothing is locked).
    ``skip``: name suffixes a separate (xfail) test covers."""
    hits: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or (skip and path.name.endswith(skip)):
            continue
        data = path.read_bytes().lower()
        found = [n for n in needles if n.lower().encode() in data]
        if found:
            hits[str(path.relative_to(root))] = found
    return hits


def _present(haystack: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n and n.lower() in haystack]


def _fingerprints(content: str) -> list[str]:
    return [xxhash.xxh64_hexdigest(content.encode())]


# ---------------------------------------------------------------- records + event log


@pytest.mark.parametrize("hard", [True, False])
async def test_records(tmp_path: Path, hard: bool) -> None:
    eng = await _start(tmp_path)
    try:
        gone, kept = await _seed(eng)
        await eng.forget(gone.record_id, namespace=NS, hard=hard)
        assert eng._storage is not None
        row = await eng._storage.get_record(gone.record_id)
        listed = {r.record_id for r in await eng._storage.list_records(NS)}
        if hard:
            assert row is None and gone.record_id not in listed
            haystack = await _sql_scan(eng)
            assert _present(haystack, [*NEEDLES, *_fingerprints(SECRET)]) == []
            assert "dana" in haystack
        else:  # soft: the read model keeps a DELETED row (the log keeps the history)
            assert row is not None and row.status is RecordStatus.DELETED
        served = {r.record_id for r, _ in await eng.search("Zebulon Vrbnik", namespace=NS)}
        assert gone.record_id not in served and kept.record_id
        assert all(r.record_id != gone.record_id for r in await eng.retrieve(namespace=NS))
    finally:
        await eng.stop()
    if hard:
        assert _file_hits(tmp_path, NEEDLES) == {}


async def test_event_payload_redaction(tmp_path: Path) -> None:
    eng = await _start(tmp_path)
    try:
        gone, _ = await _seed(eng)
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        assert eng._storage is not None
        events = await eng._storage.read_events(0, 10**6)
        assert all(not retained_fields(e.payload, gone.record_id) for e in events)
        log = "\n".join(orjson.dumps(e.payload).decode() for e in events).lower()
        assert _present(log, [*NEEDLES, *_fingerprints(SECRET)]) == []
        report = await eng.verify_forget(gone.record_id, namespace=NS, probe=SECRET)
        assert report["clean"] is True, report
    finally:
        await eng.stop()


# ---------------------------------------------------------------- vector projection


def _lance_rows(root: Path) -> list[dict[str, Any]]:
    """Every row of every retained version of every LanceDB table under ``root``."""
    rows: list[dict[str, Any]] = []
    for lance_dir in root.glob("*.lance"):
        db = lancedb.connect(str(lance_dir))
        listed = db.list_tables()
        names = getattr(listed, "tables", listed)
        for name in names:
            table = db.open_table(name)
            for version in table.list_versions():
                table.checkout(version["version"] if isinstance(version, dict) else version.version)
                rows.extend(table.to_arrow().to_pylist())
    return rows


@pytest.mark.parametrize("embed", ["hash", "onnx"])
@pytest.mark.parametrize("hard", [True, False])
async def test_vector_projection(tmp_path: Path, embed: str, hard: bool) -> None:
    eng = await _start(tmp_path, embed=embed)
    try:
        gone, kept = await _seed(eng)
        assert eng._embedder is not None
        inner = getattr(eng._embedder, "_inner", eng._embedder)
        [erased_vector] = await inner.embed([SECRET])
        assert await eng._vector.exists(gone.record_id)  # type: ignore[attr-defined]
        await eng.forget(gone.record_id, namespace=NS, hard=hard)
        assert not await eng._vector.exists(gone.record_id)  # type: ignore[attr-defined]
        if hard:
            assert await eng._vector.history_absent(gone.record_id) is True  # type: ignore[attr-defined]
    finally:
        await eng.stop()
    rows = _lance_rows(tmp_path)
    assert any(r["record_id"] == kept.record_id for r in rows)  # the scan sees live rows
    if hard:  # no retained version holds the erased row or its vector
        assert [r for r in rows if r["record_id"] == gone.record_id] == []
        assert all(
            max(abs(a - b) for a, b in zip(r["vector"], erased_vector, strict=True)) > 1e-6
            for r in rows
        )
        assert _file_hits(tmp_path, NEEDLES) == {}
    else:  # soft: the live table no longer returns it
        db = lancedb.connect(str(next(tmp_path.glob("*.lance"))))
        listed = db.list_tables()
        for name in getattr(listed, "tables", listed):
            live = db.open_table(name).to_arrow().to_pylist()
            assert all(r["record_id"] != gone.record_id for r in live)


# ---------------------------------------------------------------- lexical projection


def _doc_freq(index: Any, term: str) -> int:
    """Documents holding ``term`` in any segment, deleted ones included (tantivy's term
    statistics ignore tombstones): non-zero means the term is still on disk."""
    return int(index.searcher().doc_freq("content", term))


@pytest.mark.parametrize("hard", [True, False])
async def test_lexical_projection_tantivy(tmp_path: Path, hard: bool) -> None:
    eng = await _start(tmp_path, graph=None)
    try:
        await eng.write("Ola walks dogs at dawn", namespace=NS)
        gone, _ = await _seed(eng)
        for i in range(12):  # enough commits that the erased document shares a merged segment
            await eng.write(f"Ola walks dogs number {i}", namespace=NS)
        lexical = eng._lexical
        assert lexical is not None
        index = lexical.connector.index  # type: ignore[attr-defined]
        assert _doc_freq(index, "zebulon") == 1
        await eng.forget(gone.record_id, namespace=NS, hard=hard)
        assert not await lexical.exists(gone.record_id)
        assert gone.record_id not in {h.record_id for h in await lexical.search(NS, "zebulon")}
        index.reload()
        if hard:
            assert [n for n in NEEDLES if _doc_freq(index, n)] == []
        assert _doc_freq(index, "walks") >= 12  # live documents survive the purge
    finally:
        await eng.stop()
    if hard:  # the persisted index, reopened: no segment holds an erased term
        reopened = tantivy.Index.open(str(tmp_path / "m.db.tantivy"))
        assert [n for n in NEEDLES if _doc_freq(reopened, n)] == []
        assert _file_hits(tmp_path, NEEDLES) == {}


async def test_lexical_rebuild_never_resurrects_a_hard_erased_record(tmp_path: Path) -> None:
    """Regression (RM-135 follow-up): the erasure's lexical purge clears the index and re-deletes
    the erased id, leaving a no-op delete commit. Tantivy's ``delete_all_documents`` then dropped
    the first delete of the next rebuild, so replaying the redacted WRITE and the hard FORGET put
    the erased record back in the index. Erase, rebuild twice, restart: still absent, no erased
    term in any index file."""
    eng = await _start(tmp_path, graph=None)
    try:
        gone, kept = await _seed(eng)
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        lexical = eng._lexical
        assert lexical is not None
        assert not await lexical.exists(gone.record_id)
        for _ in range(2):
            await eng.rebuild()
            assert not await lexical.exists(gone.record_id)
            assert await lexical.exists(kept.record_id)  # live documents survive the rebuild
            report = await eng.verify_forget(gone.record_id)
            assert report["lexical_absent"] is True and report["clean"] is True
    finally:
        await eng.stop()
    eng = await _start(tmp_path, graph=None)
    try:
        lexical = eng._lexical
        assert lexical is not None
        assert not await lexical.exists(gone.record_id)
        assert await lexical.exists(kept.record_id)
        assert (await eng.verify_forget(gone.record_id))["clean"] is True
        index = lexical.connector.index  # type: ignore[attr-defined]
        index.reload()
        assert [n for n in NEEDLES if _doc_freq(index, n)] == []
    finally:
        await eng.stop()
    reopened = tantivy.Index.open(str(tmp_path / "m.db.tantivy"))
    assert [n for n in NEEDLES if _doc_freq(reopened, n)] == []
    assert _file_hits(tmp_path / "m.db.tantivy", NEEDLES) == {}


async def test_lexical_projection_fts5(tmp_path: Path) -> None:
    """The SQLite FTS5 lexical projection was removed (v0.2): no provider builds one and no
    FTS virtual table exists in the records database, so it holds no copy to erase. If it
    comes back, this fails until the class gets a real erasure test."""
    assert get_registry("lexical_indexes").find("sqlite_fts5") is None
    assert get_registry("lexical_indexes").find("fts5") is None
    eng = await _start(tmp_path)
    try:
        await _seed(eng)
        assert eng._sql is not None
        async with eng._sql.engine.connect() as conn:
            rows = (await conn.execute(text("select name, sql from sqlite_master"))).all()
        assert [n for n, sql in rows if sql and "fts" in sql.lower()] == []
    finally:
        await eng.stop()


# ---------------------------------------------------------------- graph projection


async def _ladybug_dump(eng: Engine) -> str:
    graph: Any = eng._graph
    rows: list[Any] = []
    for query in (
        "MATCH (n:MemoryNode) RETURN n.node_id, n.labels, n.properties",
        "MATCH (s:MemoryNode)-[e:MemoryLink]->(d:MemoryNode) "
        "RETURN s.node_id, d.node_id, e.rel_type, e.properties",
    ):
        rows.extend(await graph._execute(query, {}))
    try:
        rows.extend(await graph._execute("MATCH (v:EntityVec) RETURN v.node_id, v.text", {}))
    except Exception:  # no entity-vector table was created (graph.entity_embeddings off)
        pass
    return json.dumps(rows, default=str).lower()


@pytest.mark.parametrize("graph", ["sql_adjacency", "ladybug"])
@pytest.mark.parametrize("hard", [True, False])
async def test_graph_projection(tmp_path: Path, graph: str, hard: bool) -> None:
    eng = await _start(tmp_path, graph=graph)
    try:
        assert type(eng._graph).__name__ == (
            "SqlAdjacencyGraph" if graph == "sql_adjacency" else "LadybugGraphStore"
        )
        gone, kept = await _seed(eng)
        assert eng._graph is not None
        assert await eng._graph.edges_of(gone.record_id) or await eng._graph.node_count()
        await eng.forget(gone.record_id, namespace=NS, hard=hard)
        assert await eng._graph.edges_of(gone.record_id) == []
        edges = await eng._graph.edge_list(NS)
        assert all(gone.record_id not in (e.src, e.dst) for e in edges)
        if graph == "ladybug":
            dump = await _ladybug_dump(eng)
            assert gone.record_id.lower() not in dump
            if hard:
                assert _present(dump, NEEDLES) == []
            assert kept.record_id.lower() in dump  # the dump sees live nodes
        if hard:
            assert _present(await _sql_scan(eng), NEEDLES) == []
    finally:
        await eng.stop()
    if hard:
        assert _file_hits(tmp_path, NEEDLES) == {}


class _MentionFinder:
    """A GLiNER stand-in: exact, typed spans of the capitalised names in the text."""

    async def extract_mentions(self, content: str) -> list[Any]:
        from memspine.derive.gliner_mentions import MentionSpan

        spans = []
        for name in ("Zebulon Quartz", "Vrbnik", "Dana"):
            start = content.find(name)
            if start >= 0:
                spans.append(
                    MentionSpan(text=name, label="entity", start=start, end=start + len(name))
                )
        return spans

    async def extract(self, content: str) -> list[Any]:
        return []


@pytest.mark.parametrize("graph", ["sql_adjacency", "ladybug"])
@pytest.mark.parametrize("hard", [True, False])
async def test_mention_projection(tmp_path: Path, graph: str, hard: bool) -> None:
    eng = await _start(tmp_path, graph=graph)
    try:
        assert eng._semantic is not None and eng._graph is not None
        eng._semantic._extractor = _MentionFinder()  # type: ignore[attr-defined]
        gone = await eng.write(SECRET, namespace=NS)
        kept = await eng.write("Dana met Dana", namespace=NS)
        assert gone.fingerprint.entity_mentions  # the spans were bound to the source
        before = await eng._graph.node_count()
        await eng.forget(gone.record_id, namespace=NS, hard=hard)
        # every occurrence and surface node of the erased record is gone (soft and hard)
        nodes = (
            await _ladybug_dump(eng)
            if graph == "ladybug"
            else "\n".join(
                line
                for line in (await _sql_scan(eng)).splitlines()
                if line.startswith(("graph_nodes", "graph_edges"))
            )
        )
        assert _present(nodes, NEEDLES) == []
        assert gone.record_id.lower() not in nodes
        assert "dana" in nodes and kept.record_id
        assert await eng._graph.node_count() < before
        if hard:
            assert _present(await _sql_scan(eng), NEEDLES) == []
    finally:
        await eng.stop()
    if hard:  # LadybugDB's own file included (see test_mention_projection_ladybug_file)
        assert _file_hits(tmp_path, NEEDLES) == {}


async def test_mention_projection_ladybug_file(tmp_path: Path) -> None:
    """LadybugDB DETACH DELETE leaves the deleted mention-surface strings in the database file
    pages (no vacuum); the hard erase rewrites the database into a fresh file (export/import,
    atomic swap at the same path). No file keeps an erased name, the live graph survives, and
    the swapped database keeps working (a write and a restart after the erase)."""
    eng = await _start(tmp_path, graph="ladybug")
    try:
        assert eng._semantic is not None and eng._graph is not None
        eng._semantic._extractor = _MentionFinder()  # type: ignore[attr-defined]
        gone = await eng.write(SECRET, namespace=NS)
        kept = await eng.write("Dana met Dana", namespace=NS)
        before = await eng._graph.node_count()
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        assert not getattr(eng._graph, "holds_deleted_strings", True)
        assert 0 < await eng._graph.node_count() < before
        dump = await _ladybug_dump(eng)
        assert kept.record_id.lower() in dump and _present(dump, NEEDLES) == []
        later = await eng.write("Dana met Ola", namespace=NS)
        assert later.record_id.lower() in await _ladybug_dump(eng)
    finally:
        await eng.stop()
    assert _file_hits(tmp_path, NEEDLES) == {}
    assert not [p for p in tmp_path.iterdir() if ".compact-" in p.name]  # no work dir left
    eng = await _start(tmp_path, graph="ladybug")
    try:
        dump = await _ladybug_dump(eng)
        assert kept.record_id.lower() in dump and later.record_id.lower() in dump
        assert gone.record_id.lower() not in dump
    finally:
        await eng.stop()


# ---------------------------------------------------------------- caches


def _kv_keys(kv: Any) -> list[str]:
    data = getattr(kv, "_data", None)
    assert data is not None, f"no key listing for {type(kv).__name__}"
    return list(data)


@pytest.mark.parametrize("backend", ["memory", "disk"])
async def test_kv_caches(tmp_path: Path, backend: str) -> None:
    """The embedding cache (and the extraction cache, keyed the same way) drops every entry
    keyed on the erased text: document and query vectors."""
    cache = (
        {"provider": "memory"}
        if backend == "memory"
        else {"provider": "disk", "settings": {"path": str(tmp_path / "kv")}}
    )
    eng = await _start(tmp_path, cache=cache)
    digest = xxhash.xxh64_hexdigest(SECRET.encode())
    try:
        gone, kept = await _seed(eng)
        await eng.search(SECRET, namespace=NS)  # caches the query vector of the erased text too
        embedder: Any = eng._embedder
        keys = [embedder._key(SECRET, p) for p in ("emb", "embq")]
        assert await eng._cache.get(keys[0]) is not None  # type: ignore[union-attr]
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        assert [k for k in keys if await eng._cache.get(k) is not None] == []  # type: ignore[union-attr]
        assert await eng._cache.get(embedder._key(KEPT, "emb")) is not None  # type: ignore[union-attr]
        if backend == "memory":
            assert [k for k in _kv_keys(eng._cache) if digest in k] == []
    finally:
        await eng.stop()
    assert _file_hits(tmp_path, NEEDLES) == {}


async def test_kv_disk_cache_files(tmp_path: Path) -> None:
    """The disk cache deletes the erased text's key logically; the hard erase then compacts
    the cache database (VACUUM, WAL truncated), so no file page keeps the key's xxh64."""
    cache = {"provider": "disk", "settings": {"path": str(tmp_path / "kv")}}
    eng = await _start(tmp_path, cache=cache)
    try:
        gone, _ = await _seed(eng)
        await eng.forget(gone.record_id, namespace=NS, hard=True)
    finally:
        await eng.stop()
    assert _file_hits(tmp_path / "kv", [xxhash.xxh64_hexdigest(SECRET.encode())]) == {}


QUERY = "where does Zebulon live"


@pytest.mark.parametrize("backend", ["memory", "disk"])
async def test_cached_query_vectors(tmp_path: Path, backend: str) -> None:
    """A query naming the erased subject is cached under its own text, which no record holds,
    so the erasure cannot name its key: a hard erase drops every cached query vector (memory
    and disk KV; the disk files keep no page of the key's xxh64). Document vectors of live
    records stay."""
    cache = (
        {"provider": "memory"}
        if backend == "memory"
        else {"provider": "disk", "settings": {"path": str(tmp_path / "kv")}}
    )
    eng = await _start(tmp_path, cache=cache)
    try:
        gone, _ = await _seed(eng)
        await eng.search(QUERY, namespace=NS)
        embedder: Any = eng._embedder
        variant = getattr(getattr(embedder, "_inner", embedder), "query_variant", "")
        key = embedder._key(QUERY, f"embq{variant}")
        assert await eng._cache.get(key) is not None, "the query vector was not cached"  # type: ignore[union-attr]
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        assert await eng._cache.get(key) is None  # type: ignore[union-attr]
        assert await eng._cache.get(embedder._key(KEPT, "emb")) is not None  # type: ignore[union-attr]
        if backend == "memory":
            assert [k for k in _kv_keys(eng._cache) if k.startswith("embq")] == []
    finally:
        await eng.stop()
    if backend == "disk":
        assert _file_hits(tmp_path / "kv", [xxhash.xxh64_hexdigest(QUERY.encode())]) == {}


def _read_cache_dump(eng: Engine) -> str:
    cache = eng.__dict__.get("_read_result_cache")
    if cache is None:
        return ""
    entries = getattr(getattr(cache, "_backend", None), "_d", None)
    assert entries is not None, f"no entry listing for {type(cache).__name__}"
    return repr(list(entries.values())).lower()


async def test_read_side_caches(tmp_path: Path) -> None:
    """The read-result cache, the N60 sentence vectors, the N30 date spans and the lexical stem
    memo hold no entry of the erased text after a hard erase."""
    from memspine.models.encoders.query import temporal_query
    from memspine.read.legs import lexical as lexical_leg

    eng = await _start(tmp_path, read={"cache": {"enabled": True}})
    try:
        gone, _ = await _seed(eng)
        await eng.read("where does Zebulon live", namespace=NS)
        await eng.assemble("where does Zebulon live", namespace=NS)
        assert "zebulon" in _read_cache_dump(eng), "the read-result cache was not filled"
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        assert "zebulon" not in _read_cache_dump(eng)
        assert all(key[1] != SECRET for key in eng._sentence_vectors)
        assert all(SECRET not in str(k) for k in getattr(temporal_query, "_MENTIONS", {}))
        assert lexical_leg._stem.cache_info().currsize == 0
        served = await eng.read("where does Zebulon live", namespace=NS)
        assert "zebulon" not in repr(served).lower()
    finally:
        await eng.stop()


# ---------------------------------------------------------------- logs and traces


def _tap_events(traces: list[dict[str, Any]]) -> str:
    return json.dumps(traces, default=str).lower()


async def test_logs_and_traces(tmp_path: Path) -> None:
    """No structlog event and no tap trace (memory and JSONL sinks, default content setting)
    carries the content, before or after the erasure: they hold ids only."""
    from memspine.observability import bindings, tap

    tap_dir = tmp_path / "tap"
    eng = await _start(
        tmp_path / "db",
        observability={"tap": {"enabled": True, "sink": "memory"}},
    )
    jsonl = None
    try:
        from memspine.observability.tap_sinks import JsonlSink

        jsonl = JsonlSink(tap_dir)
        with structlog.testing.capture_logs() as logs, bindings.bound(eng._binding):
            tap.add_sink(jsonl)
            gone, _ = await _seed(eng)
            await eng.search("Zebulon Vrbnik", namespace=NS)
            await eng.read("where does Zebulon live", namespace=NS)
            await eng.forget(gone.record_id, namespace=NS, hard=True)
            await eng.search("Dana", namespace=NS)
            traces = tap.memory_traces()
        assert traces, "the tap captured nothing"
        assert logs, "structlog captured nothing"
        assert _present(json.dumps(logs, default=str).lower(), NEEDLES) == []
        assert _present(_tap_events(traces), NEEDLES) == []
    finally:
        await eng.stop()
    assert _file_hits(tmp_path, NEEDLES) == {}


@pytest.mark.parametrize(
    ("inspect_content", "sink"),
    [(False, "memory"), (True, "memory"), (True, "jsonl"), (True, "sqlite")],
)
async def test_logs_and_traces_with_tap_content_opt_in(
    tmp_path: Path, inspect_content: bool, sink: str
) -> None:
    """``observability.tap.content`` alone keeps no record text in this flow. The inspector's
    ``inspect_content`` puts the masked text in the traces; the hard erase drops every trace
    naming the erased id from the engine-owned sinks (memory, JSONL files, SQLite database).
    The ``log`` and ``otel`` sinks keep no copy in the engine (the operator's pipeline does)."""
    from memspine.observability import bindings, tap

    tap_dir = tmp_path / "tap"
    eng = await _start(
        tmp_path / "db",
        observability={
            "tap": {"enabled": True, "sink": sink, "content": True, "dir": str(tap_dir)},
            "inspect": inspect_content,
            "inspect_content": inspect_content,
        },
    )
    kept: list[Any] = []
    try:
        with bindings.bound(eng._binding):
            gone, kept_record = await _seed(eng)
            kept.append(kept_record)
            await eng.search("Zebulon Vrbnik", namespace=NS)
            if inspect_content:  # the negative control: the sink holds the text before
                if sink == "memory":
                    assert _present(_tap_events(tap.memory_traces()), NEEDLES)
                else:
                    assert _file_hits(tap_dir, NEEDLES)
            await eng.forget(gone.record_id, namespace=NS, hard=True)
            traces = tap.memory_traces()
        if sink == "memory":
            assert traces
            assert _present(_tap_events(traces), NEEDLES) == []
    finally:
        await eng.stop()
    if sink != "memory":
        assert _file_hits(tap_dir, NEEDLES) == {}
        assert _file_hits(tap_dir, [kept[0].record_id]), "the kept record's traces went too"


# ---------------------------------------------------------------- restored backups


def _copy_store(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.iterdir():
        target = dst / path.name
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)


def _restore(backup: Path, live: Path) -> None:
    shutil.rmtree(live)
    _copy_store(backup, live)


async def test_restored_backup_re_hides(tmp_path: Path) -> None:
    """A backup taken before the erasure brings the record back; re-applying the erasure
    manifests (kept outside the backup) erases it again, in every store."""
    live, backup = tmp_path / "live", tmp_path / "backup"
    live.mkdir()
    eng = await _start(live)
    try:
        gone, kept = await _seed(eng)
    finally:
        await eng.stop()
    _copy_store(live, backup)

    eng = await _start(live)
    try:
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        manifests = await eng.erasure_manifests()
        assert [m.ids.record_ids for m in manifests] == [[gone.record_id]]
        assert (
            "zebulon"
            not in orjson.dumps([m.model_dump(mode="json") for m in manifests]).decode().lower()
        )
    finally:
        await eng.stop()
    _restore(backup, live)

    eng = await _start(live)
    try:
        assert eng._storage is not None
        assert await eng._storage.get_record(gone.record_id) is not None  # the backup's copy
        assert await eng.reapply_erasure_manifests(manifests) == [gone.record_id]
        assert await eng._storage.get_record(gone.record_id) is None
        served = {r.record_id for r, _ in await eng.search("Zebulon Vrbnik", namespace=NS)}
        assert gone.record_id not in served and kept.record_id in {
            r.record_id for r, _ in await eng.search("Dana bakes", namespace=NS)
        }
        assert _present(await _sql_scan(eng), [*NEEDLES, *_fingerprints(SECRET)]) == []
        assert await eng.reapply_erasure_manifests(manifests) == []  # idempotent
    finally:
        await eng.stop()
    assert _file_hits(live, NEEDLES) == {}
    eng = await _start(live)  # a restart keeps it hidden (the re-erasure wrote its manifest)
    try:
        assert eng._storage is not None
        assert await eng._storage.get_record(gone.record_id) is None
        from memspine.trust.pipeline.erasure_state import visible_id

        assert not visible_id(eng, gone.record_id)
    finally:
        await eng.stop()


# ---------------------------------------------------------------- truncated packed excerpts


async def test_truncated_packed_excerpts(tmp_path: Path) -> None:
    """ADR-092: an oversize first record is truncated in the RESULT only. Packing persists
    nothing (no event, no row), and once the record is erased no later read returns it."""
    eng = await _start(tmp_path, graph=None)
    long_text = " ".join([SECRET + "."] * 30)
    try:
        gone = await eng.write(long_text, namespace=NS)
        assert eng._storage is not None
        before = await _sql_scan(eng)
        seq_before = len(await eng._storage.read_events(0, 10**6))
        ctx = await eng.assemble("Zebulon Vrbnik", namespace=NS, budget_tokens=40)
        hit = ctx.hits[gone.record_id]
        assert hit.truncated
        [packed] = [r for r in ctx.records if r.record_id == gone.record_id]
        assert len(packed.content) < len(long_text)
        assert len(await eng._storage.read_events(0, 10**6)) == seq_before  # nothing appended
        assert await _sql_scan(eng) == before  # nothing written anywhere
        assert (await eng._storage.get_record(gone.record_id)).content == long_text  # type: ignore[union-attr]
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        again = await eng.assemble("Zebulon Vrbnik", namespace=NS, budget_tokens=40)
        assert gone.record_id not in again.hits
        assert _present(await _sql_scan(eng), NEEDLES) == []
    finally:
        await eng.stop()
    assert _file_hits(tmp_path, NEEDLES) == {}


# ---------------------------------------------------------------- completeness


def test_every_copy_class_has_a_gate_test() -> None:
    """An unsupported copy class fails the gate instead of being skipped silently."""
    required = {
        "records",
        "event payload redaction",
        "vector projection",
        "lexical projection",
        "lexical projection (fts5)",
        "graph projection",
        "mention projection",
        "copied excerpts",
        "unresolved annotations",
        "window drafts",
        "entity name index",
        "alias text",
        "message metadata",
        "known-entity cache",
        "prepass mention cache",
        "caches (kv)",
        "caches (read results and memos)",
        "cached query vectors",
        "resolver name caches",
        "logs and traces",
        "restored backups",
        "truncated packed excerpts",
        "in-flight background work",
    }
    assert required - set(COPY_CLASSES) == set(), "copy class without a gate test"
    here = Path(__file__).parent
    for name, (module, function) in COPY_CLASSES.items():
        path = here / f"{module}.py"
        assert path.exists(), f"{name}: gate module {module} is missing"
        source = path.read_text(encoding="utf-8")
        assert f"def {function}(" in source, f"{name}: no gate test {module}::{function}"
        assert "pytestmark = pytest.mark.erasure_gate" in source, f"{module} is not in the gate"
        skips = ("import" + "orskip", "pytest." + "skip(", "mark." + "skip")
        assert not any(s in source for s in skips), f"{module} skips a copy class"
