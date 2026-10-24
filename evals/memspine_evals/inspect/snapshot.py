"""TI-2: the store snapshot dumper (Trace Inspector, ``docs/v2/TRACE_INSPECTOR_PLAN.md`` section 1).

After ingest and again after sleep, ``dump_snapshot`` reads one namespace straight from the
stores (read-only, CPU only, no model call) and appends rows to three JSONL files, plus one
additive file:

* ``records.jsonl``: every stored record, archived / superseded / quarantined included, one row
  per record per ``phase`` (``post_ingest`` | ``post_sleep``), with the assertion envelope.
* ``links.jsonl``: lineage-table rows (``lineage``), ``source.parents`` edges (``derived_from``)
  and every graph edge (``graph`` / ``reply`` / ``mentions`` / ``derived_from`` / ``typed``).
* ``sleep.jsonl``: the sleep markers read from the event log (``stage_done``, ``graph_extracted``,
  ``entity_resolved``, ``entity_summarized``, ``community_partition``, ``profile_card``,
  ``session_closed``) and the CONSOLIDATE events. Each event is written once per out dir (the
  last seq dumped is kept in ``.snapshot_state.json``), so the files are append-safe.
* ``graph_nodes.jsonl`` (additive): the graph nodes, one row per node per phase.

This is the ground truth for "how it is stored and linked", independent of tap events.

CLI (an existing on-disk store)::

    python -m memspine_evals.inspect.snapshot --store path/to/memspine.db --namespace default \\
        --out runs/_analysis/inspect/x --phase post_sleep [--no-content]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["dump_snapshot", "main"]

PHASES = ("post_ingest", "post_sleep")
STATE_FILE = ".snapshot_state.json"
#: keys of an envelope that carry conversation text; dropped when ``content`` is off.
_TEXT_KEYS = frozenset({"text", "quote", "snippet"})
#: graph edge relations that get their own ``kind`` in links.jsonl.
_REL_KINDS = {"mentions": "mentions", "derived_from": "derived_from", "reply_to": "reply"}
_PLAIN_EDGE_PROPS = frozenset({"weight", "reason", "kind"})
#: id-bearing payload keys of a marker.
_INPUT_KEYS = frozenset(
    {"record_id", "member_record_ids", "record_ids", "sources", "parents", "members"}
)
_OUTPUT_KEYS = frozenset({"output_ids", "created", "written", "summary_id", "summary_record_id"})
_MARKER_STAGE = {
    "graph_extracted": "extract_graph",
    "entity_resolved": "resolve_entities",
    "entity_summarized": "summarize_entities",
    "community_partition": "communities",
    "profile_card": "reflect_profile",
    "session_closed": "session_close",
}


def _plain(value: Any) -> Any:
    """JSON-safe copy (datetimes to ISO, enums to values, bytes dropped to a length)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, bytes):
        return {"bytes": len(value)}
    return getattr(value, "value", value)


def _strip_text(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_text(v) for k, v in value.items() if k not in _TEXT_KEYS}
    if isinstance(value, list):
        return [_strip_text(v) for v in value]
    return value


def _append(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(_plain(row), ensure_ascii=False, sort_keys=False) + "\n")
    return len(rows)


def _ids(value: Any) -> list[str]:
    """Strings found in a nested marker field (an id, a list of ids, id/fingerprint pairs...)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        if "record_id" in value:
            return _ids(value["record_id"])
        return [str(k) for k in value]
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(x, str) for x in value):
            return [value[0]]  # (record id, fingerprint)
        return [i for v in value for i in _ids(v)]
    return []


def _walk_ids(payload: Mapping[str, Any], keys: frozenset[str]) -> list[str]:
    found: list[str] = []
    for key, value in payload.items():
        if key in keys:
            found.extend(_ids(value))
        elif key in ("decisions", "entities") and isinstance(value, list):
            for entry in value:
                if isinstance(entry, Mapping):
                    found.extend(_walk_ids(entry, keys))
    return list(dict.fromkeys(found))


def _value_of(record: Any, envelope: dict[str, Any] | None) -> Any:
    value = getattr(record, "value", None)
    if value is None and envelope:
        value = envelope.get("value")
    return value


def _edge_kind(rel: str, props: Mapping[str, Any]) -> str:
    if rel in _REL_KINDS:
        return _REL_KINDS[rel]
    if any(k not in _PLAIN_EDGE_PROPS for k in props) or props.get("kind"):
        return "typed"
    return "graph"


def _turns_of(rid: str, parents: dict[str, list[str]], origin: Mapping[str, str]) -> list[str]:
    """Dataset turn ids of every ancestor of ``rid`` the harness knows (nearest first)."""
    out: list[str] = []
    seen = {rid}
    frontier = list(parents.get(rid, ()))
    while frontier:
        nxt: list[str] = []
        for pid in frontier:
            if pid in seen:
                continue
            seen.add(pid)
            if pid in origin:
                out.append(origin[pid])
            nxt.extend(parents.get(pid, ()))
        frontier = nxt
    return list(dict.fromkeys(out))


async def _lineage_rows(storage: Any, namespace: str) -> list[dict[str, Any]]:
    from sqlalchemy import or_, select

    from memspine.services.storage.sqlite.schema import lineage

    client = getattr(storage, "_client", None)
    if client is None:
        return []
    stmt = select(lineage).where(
        or_(lineage.c.namespace == namespace, lineage.c.namespace.is_(None))
    )
    async with client.engine.connect() as conn:
        rows = (await conn.execute(stmt.order_by(lineage.c.created_seq))).all()
    return [dict(r._mapping) for r in rows]


async def _graph_rows(
    engine: Any, storage: Any, namespace: str
) -> tuple[list[Any], list[dict[str, Any]]]:
    graph = getattr(engine, "_graph", None)
    if graph is None:
        return [], []
    edges = list(await graph.edge_list(namespace))
    nodes: list[dict[str, Any]] = []
    import orjson

    client = getattr(graph, "_client", None)
    if hasattr(client, "engine"):  # sqlite adjacency
        from sqlalchemy import select

        from memspine.services.storage.sqlite.schema import graph_nodes

        stmt = select(graph_nodes).where(graph_nodes.c.namespace == namespace)
        async with client.engine.connect() as conn:
            for r in (await conn.execute(stmt)).all():
                m = r._mapping
                nodes.append(
                    {
                        "node_id": m["node_id"],
                        "labels": list(orjson.loads(m["labels"])),
                        "properties": dict(orjson.loads(m["properties"])),
                        "kind": m["kind"],
                    }
                )
    elif hasattr(graph, "_execute"):  # LadybugDB / Kuzu
        rows = await graph._execute(
            "MATCH (n:MemoryNode) WHERE n.namespace = $ns "
            "RETURN n.node_id, n.labels, n.properties, n.kind",
            {"ns": namespace},
        )
        for node_id, labels, props, kind in rows:
            nodes.append(
                {
                    "node_id": node_id,
                    "labels": list(orjson.loads(labels)) if labels else [],
                    "properties": dict(orjson.loads(props)) if props else {},
                    "kind": kind,
                }
            )
    else:  # another backend: the endpoints of the edges are all we can name
        ids = dict.fromkeys(x for e in edges for x in (e.src, e.dst))
        nodes = [{"node_id": i, "labels": [], "properties": {}, "kind": None} for i in ids]
    return edges, nodes


def _node_props(props: dict[str, Any], content: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, list) and len(v) > 16 and all(isinstance(x, float) for x in v):
            out[k + "_dim"] = len(v)  # an embedding: keep its size, not its numbers
        elif isinstance(v, str) and not content and k in ("text", "entity_text", "summary"):
            continue
        else:
            out[k] = v
    return out


async def _event_rows(
    storage: Any, namespace: str, after_seq: int, content: bool
) -> tuple[list[dict[str, Any]], int]:
    from memspine.store.events import EventKind

    rows: list[dict[str, Any]] = []
    last = after_seq
    while True:
        events = await storage.read_events(after_seq=last, limit=1000)
        if not events:
            break
        for ev in events:
            last = max(last, ev.seq or last)
            if ev.namespace != namespace:
                continue
            payload = dict(ev.payload or {})
            if ev.kind is EventKind.MARKER:
                marker = str(payload.get("marker", ""))
                if marker in ("stage_done", "stage_cleared"):
                    stage = str(payload.get("stage", ""))
                    status = "done" if marker == "stage_done" else "cleared"
                else:
                    stage, status = _MARKER_STAGE.get(marker, marker), "done"
                session_key = payload.get("session_key") or payload.get("session_id")
            elif ev.kind is EventKind.CONSOLIDATE:
                marker, stage, status = "consolidate", "consolidate", "done"
                session_key = payload.get("session_key")
            else:
                continue
            row: dict[str, Any] = {
                "stage": stage,
                "cycle": None,
                "session_key": session_key,
                "input_ids": _walk_ids(payload, _INPUT_KEYS),
                "output_ids": _walk_ids(payload, _OUTPUT_KEYS),
                "marker": marker,
                "calls": payload.get("calls"),
                "tokens": payload.get("tokens"),
                "status": status,
                "seq": ev.seq,
                "ts": ev.ts,
                "payload_keys": sorted(payload),
            }
            if content:
                row["payload"] = payload
            rows.append(row)
        if len(events) < 1000:
            break
    return rows, last


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


async def dump_snapshot(
    engine: Any,
    namespace: str,
    out_dir: str | Path,
    phase: str,
    origin_map: Mapping[str, str] | None = None,
    content: bool = True,
    *,
    run_id: str | None = None,
    item: str | None = None,
    cycle: int | None = None,
) -> dict[str, int]:
    """Append one snapshot of ``namespace`` to ``out_dir``; returns the rows written per file.

    ``origin_map`` is ``{record_id: dataset turn id}`` (the harness ``_origin``). ``content=False``
    leaves out record text, envelope text and marker payloads (ids and structure only).
    """
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    out = Path(out_dir)
    origin = dict(origin_map or {})
    storage = engine._require_started()
    common = {"run_id": run_id, "item": item, "namespace": namespace, "phase": phase}

    records = list(await storage.list_records(namespace))
    lineage_rows = await _lineage_rows(storage, namespace)
    parents: dict[str, list[str]] = {}
    for rec in records:
        parents[str(rec.record_id)] = [str(p) for p in rec.source.parents]
    for lr in lineage_rows:
        plist = parents.setdefault(str(lr["child_id"]), [])
        if str(lr["parent_id"]) not in plist:
            plist.append(str(lr["parent_id"]))

    rec_rows: list[dict[str, Any]] = []
    for rec in records:
        rid = str(rec.record_id)
        try:
            env_obj = await engine.get_envelope(rid, namespace)
            envelope = env_obj.model_dump(mode="json") if env_obj is not None else None
        except Exception:  # a scope-guarded or malformed envelope: fall back to the stored blob
            envelope = rec.fingerprint.envelope
        if envelope is not None and not content:
            envelope = _strip_text(envelope)
        status = getattr(rec.status, "value", rec.status)
        rec_rows.append(
            {
                **common,
                "record_id": rid,
                "turn_id": origin.get(rid),
                "memory_type": rec.memory_type,
                "layer": rec.layer,
                "channel": rec.source.channel,
                "role": rec.source.role,
                "content": rec.content if content else None,
                "content_fingerprint": rec.content_fingerprint,
                "tags": list(rec.tags),
                "entity": rec.entity,
                "attribute": rec.attribute,
                "value": _value_of(rec, envelope),
                "status": status,
                "valid_from": rec.valid_from,
                "valid_to": rec.valid_to,
                "recorded_at": rec.recorded_at,
                "superseded_at": rec.superseded_at,
                "invalid_at": rec.invalid_at,
                "source_at": rec.source_at,
                "evolve_to": rec.evolve_to,
                "version": rec.version,
                "group_id": rec.group_id,
                "trust": rec.trust,
                "quarantined": rec.quarantined,
                "tier": rec.tier,
                "speaker": rec.axes.speaker,
                "parents": parents.get(rid, []),
                "parent_turns": _turns_of(rid, parents, origin),
                "envelope": envelope,
            }
        )

    link_rows: list[dict[str, Any]] = []
    for lr in lineage_rows:
        deriv = lr.get("derivation")
        created_by = None
        if deriv:
            try:
                d = json.loads(deriv)
                created_by = d.get("derivation") or d.get("prompt_hash")
            except ValueError:
                created_by = None
        link_rows.append(
            {
                **common,
                "kind": "lineage",
                "src": lr["child_id"],
                "dst": lr["parent_id"],
                "rel": lr["role"],
                "props": {
                    "created_seq": lr["created_seq"],
                    "source_version": lr["source_version"],
                    "source_family": lr["source_family"],
                    "span": json.loads(lr["span"]) if lr.get("span") else None,
                },
                "created_by": created_by,
            }
        )
    for rec in records:
        for pid in rec.source.parents:
            link_rows.append(
                {
                    **common,
                    "kind": "derived_from",
                    "src": str(rec.record_id),
                    "dst": str(pid),
                    "rel": "parent",
                    "props": {},
                    "created_by": rec.source.channel,
                }
            )
    edges, nodes = await _graph_rows(engine, storage, namespace)
    for e in edges:
        props = dict(e.properties)
        link_rows.append(
            {
                **common,
                "kind": _edge_kind(e.rel_type, props),
                "src": e.src,
                "dst": e.dst,
                "rel": e.rel_type,
                "props": props,
                "created_by": props.get("reason") or props.get("created_by"),
            }
        )
    node_rows = [
        {**common, **n, "properties": _node_props(n["properties"], content)} for n in nodes
    ]

    state_path = out / STATE_FILE
    state: dict[str, Any] = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    after = int(state.get(namespace, 0))
    sleep_rows, last = await _event_rows(storage, namespace, after, content)
    for row in sleep_rows:
        row.update(common)
        row["cycle"] = cycle if cycle is not None else (1 if phase == "post_sleep" else 0)

    counts = {
        "records": _append(out / "records.jsonl", rec_rows),
        "links": _append(out / "links.jsonl", link_rows),
        "sleep": _append(out / "sleep.jsonl", sleep_rows),
        "graph_nodes": _append(out / "graph_nodes.jsonl", node_rows),
    }
    state[namespace] = last
    _write_state(state_path, state)
    return counts


async def _dump_store(args: argparse.Namespace) -> dict[str, int]:
    import memspine

    engine = await memspine.open(
        template=args.template,
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": args.store}}},
        model={"embedding": {"provider": "hash"}},
    )
    try:
        return await dump_snapshot(
            engine,
            args.namespace,
            args.out,
            args.phase,
            origin_map=_load_origin(args.origin),
            content=not args.no_content,
            run_id=args.run_id,
            item=args.item,
        )
    finally:
        await engine.stop()


def _load_origin(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    return {str(k): str(v) for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m memspine_evals.inspect.snapshot",
        description="TI-2: dump records, links and sleep markers of an on-disk memspine store "
        "to JSONL (read-only; no model call).",
    )
    p.add_argument("--store", required=True, help="path of the SQLite store (storage.path)")
    p.add_argument("--namespace", default="default")
    p.add_argument("--out", required=True, help="output directory (rows are appended)")
    p.add_argument("--phase", choices=PHASES, default="post_sleep")
    p.add_argument("--template", default="core")
    p.add_argument("--origin", help="JSON file {record_id: turn_id} to map parents to turns")
    p.add_argument("--run-id")
    p.add_argument("--item")
    p.add_argument("--no-content", action="store_true", help="leave out record and marker text")
    args = p.parse_args(argv)
    counts = asyncio.run(_dump_store(args))
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
