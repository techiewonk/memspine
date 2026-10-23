"""TI-2: the store snapshot dumper, on the invented demo conversation (hash embedder, stub LLM)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.inspect import snapshot
from memspine_evals.inspect.snapshot import dump_snapshot

from memspine import Engine
from memspine.prompts.models import ExtractedEdge

DEMO = Path(snapshot.__file__).with_name("demo_conversation.json")
NS = "demo"
T0 = datetime(2024, 3, 2, tzinfo=UTC)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _edges(table: dict[str, list[ExtractedEdge]]) -> Any:
    async def fake(content: str, _context: object = None) -> list[ExtractedEdge]:
        return list(table.get(content, []))

    return fake


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        model={"embedding": {"provider": "hash"}},
        write={"lineage_table": True},
        memories={
            "semantic": {"enabled": True, "policies": {"extract_graph": {"resolve": "rules"}}},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
    )


async def _ingest(eng: Engine) -> tuple[dict[str, str], dict[str, str]]:
    """Write the demo turns; return ({turn_id: rid}, {rid: turn_id})."""
    doc = json.loads(DEMO.read_text(encoding="utf-8"))
    by_turn: dict[str, str] = {}
    n = 0
    for session in doc["sessions"]:
        for turn in session["turns"]:
            rec = await eng.write(
                f"{turn['speaker']}: {turn['text']}",
                namespace=NS,
                memory_type="episodic",
                valid_from=T0 + timedelta(hours=n),
            )
            by_turn[turn["turn_id"]] = rec.record_id
            n += 1
    return by_turn, {v: k for k, v in by_turn.items()}


async def _facts(eng: Engine, by_turn: dict[str, str]) -> dict[str, str]:
    """Two facts and a supersede (stub extraction): job bakery (D1:3), then cycle shop (D3:5)."""
    out: dict[str, str] = {}
    specs = [
        ("lives", "Bram lives in Utrecht", "Bram", "residence", "D1:1", 0),
        ("bakery", "Bram works at the bakery Korenbloem", "Bram", "employer", "D1:3", 1),
        ("cycle", "Bram works at the cycle shop Spaakwerk", "Bram", "employer", "D3:5", 60),
    ]
    for key, text, entity, attr, turn, day in specs:
        rec = await eng.write(
            text,
            namespace=NS,
            entity=entity,
            attribute=attr,
            derived_from=[by_turn[turn]],
            valid_from=T0 + timedelta(days=day),
        )
        out[key] = rec.record_id
    return out


@pytest.fixture
async def seeded() -> Any:
    eng = _engine()
    await eng.start()
    by_turn, origin = await _ingest(eng)
    facts = await _facts(eng, by_turn)
    yield eng, by_turn, origin, facts
    await eng.stop()


async def test_demo_fixture_is_the_shared_shape() -> None:
    doc = json.loads(DEMO.read_text(encoding="utf-8"))
    turns = [t for s in doc["sessions"] for t in s["turns"]]
    assert len(doc["sessions"]) == 3 and len(turns) == 30 and len(doc["questions"]) == 8
    ids = {t["turn_id"] for t in turns}
    assert all(g in ids for q in doc["questions"] for g in q["gold_turns"])


async def test_post_ingest_rows(seeded: Any, tmp_path: Path) -> None:
    eng, by_turn, origin, facts = seeded
    counts = await dump_snapshot(
        eng, NS, tmp_path, "post_ingest", origin_map=origin, run_id="r", item="demo-1"
    )
    rows = {r["record_id"]: r for r in _read(tmp_path / "records.jsonl")}
    assert counts["records"] == len(rows) == 33
    assert all(
        r["phase"] == "post_ingest" and r["run_id"] == "r" and r["item"] == "demo-1"
        for r in rows.values()
    )
    turn = rows[by_turn["D1:1"]]
    assert turn["turn_id"] == "D1:1" and "Utrecht" in turn["content"] and turn["parents"] == []
    # the superseded value stays in the dump, with its status, and the new one points at its turn
    bakery, cycle = rows[facts["bakery"]], rows[facts["cycle"]]
    assert bakery["status"] != cycle["status"] or bakery["valid_to"] is not None
    assert bakery["parents"] == [by_turn["D1:3"]] and bakery["parent_turns"] == ["D1:3"]
    assert cycle["parent_turns"] == ["D3:5"] and cycle["entity"] == "Bram"
    assert set(rows[facts["lives"]]) >= {
        "memory_type",
        "layer",
        "channel",
        "content",
        "tags",
        "entity",
        "attribute",
        "value",
        "status",
        "valid_from",
        "valid_to",
        "evolve_to",
        "trust",
        "parents",
        "parent_turns",
        "envelope",
    }
    links = _read(tmp_path / "links.jsonl")
    kinds = {k["kind"] for k in links}
    assert "derived_from" in kinds
    dfs = [k for k in links if k["kind"] == "derived_from" and k["src"] == facts["lives"]]
    assert dfs and dfs[0]["dst"] == by_turn["D1:1"]
    assert all(k["phase"] == "post_ingest" for k in links)


async def test_content_off_drops_text(seeded: Any, tmp_path: Path) -> None:
    eng, _, origin, _ = seeded
    await dump_snapshot(eng, NS, tmp_path, "post_ingest", origin_map=origin, content=False)
    assert all(r["content"] is None for r in _read(tmp_path / "records.jsonl"))
    assert "payload" not in json.dumps(_read(tmp_path / "sleep.jsonl"))


async def test_sleep_markers_graph_and_append_safety(seeded: Any, tmp_path: Path) -> None:
    eng, _, origin, _ = seeded
    await dump_snapshot(eng, NS, tmp_path, "post_ingest", origin_map=origin)
    before = len(_read(tmp_path / "sleep.jsonl"))
    eng._extract_edges = _edges(
        {
            "Noor: Hi both, Nono here with news: I adopted a greyhound called Pepper.": [
                ExtractedEdge(
                    src_entity="Noor",
                    rel="adopted",
                    dst_entity="Pepper",
                    kind="event",
                    fact="Noor adopted a greyhound called Pepper",
                    confidence=0.9,
                )
            ]
        }
    )
    await eng.sleep()
    counts = await dump_snapshot(eng, NS, tmp_path, "post_sleep", origin_map=origin)
    records = _read(tmp_path / "records.jsonl")
    post_ingest = [r for r in records if r["phase"] == "post_ingest"]
    post_sleep = [r for r in records if r["phase"] == "post_sleep"]
    assert len(post_ingest) == 33 and len(post_sleep) == counts["records"] >= 33
    derived = [r for r in post_sleep if r["channel"] == "extract_graph"]
    assert derived and derived[0]["parent_turns"] == ["D2:1"]
    sleep = _read(tmp_path / "sleep.jsonl")
    new = sleep[before:]
    assert new and all(r["phase"] == "post_sleep" for r in new)
    markers = {r["marker"] for r in sleep}
    assert "graph_extracted" in markers
    ext = next(r for r in sleep if r["marker"] == "graph_extracted")
    assert ext["stage"] == "extract_graph" and ext["input_ids"] and ext["status"] == "done"
    # the derived-write door also filled the lineage table: child fact -> parent turn
    lin = [k for k in _read(tmp_path / "links.jsonl") if k["kind"] == "lineage"]
    assert lin and all(k["phase"] == "post_sleep" for k in lin)
    assert {k["dst"] for k in lin} <= {r["record_id"] for r in post_sleep}
    # graph edges (mentions) land in links.jsonl, their endpoints in graph_nodes.jsonl
    graph = [
        k
        for k in _read(tmp_path / "links.jsonl")
        if k["phase"] == "post_sleep" and k["kind"] in ("mentions", "graph", "typed")
    ]
    assert any(k["kind"] == "mentions" for k in graph)
    node_ids = {
        n["node_id"] for n in _read(tmp_path / "graph_nodes.jsonl") if n["phase"] == "post_sleep"
    }
    assert {k["dst"] for k in graph if k["kind"] == "mentions"} <= node_ids
    # a second dump with no new events adds no sleep rows (each event is written once)
    n_sleep = len(sleep)
    await dump_snapshot(eng, NS, tmp_path, "post_sleep", origin_map=origin)
    assert len(_read(tmp_path / "sleep.jsonl")) == n_sleep


async def test_every_marker_kind_becomes_a_sleep_row(seeded: Any, tmp_path: Path) -> None:
    from memspine.store.events import EventKind, MemoryEvent

    eng, by_turn, origin, _ = seeded
    a, b = by_turn["D1:1"], by_turn["D1:2"]
    payloads = [
        {"marker": "stage_done", "stage": "mine_facts", "session_key": "D1", "members_fp": "x"},
        {"marker": "graph_extracted", "sources": [[a, "fp1"], [b, "fp2"]]},
        {"marker": "entity_resolved", "decisions": [{"record_id": a, "group": 1}]},
        {
            "marker": "entity_summarized",
            "entities": [{"entity": "ent:demo:bram", "content_fingerprint": "f"}],
        },
        {"marker": "community_partition", "anchors": {"ent:demo:bram": "ent:demo:bram"}},
        {"marker": "profile_card_v2", "card": {"entries": []}},
        {"marker": "session_closed", "session_id": "D2", "windows": 3},
    ]
    for payload in payloads:
        await eng._storage.append_event(
            MemoryEvent(kind=EventKind.MARKER, namespace=NS, actor="system", payload=payload)
        )
    # another tenant's marker is not ours
    await eng._storage.append_event(
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace="other",
            actor="system",
            payload={"marker": "session_closed", "session_id": "Z"},
        )
    )
    await dump_snapshot(eng, NS, tmp_path, "post_sleep", origin_map=origin)
    rows = {r["marker"]: r for r in _read(tmp_path / "sleep.jsonl") if r["marker"] != "consolidate"}
    assert set(rows) == {p["marker"] for p in payloads}
    assert rows["stage_done"]["stage"] == "mine_facts" and rows["stage_done"]["session_key"] == "D1"
    assert rows["graph_extracted"]["input_ids"] == [a, b]
    assert rows["entity_resolved"]["input_ids"] == [a]
    assert (
        rows["session_closed"]["session_key"] == "D2"
        and rows["session_closed"]["stage"] == "session_close"
    )
    assert rows["profile_card_v2"]["stage"] == "reflect_profile"
    assert all(
        {
            "stage",
            "cycle",
            "session_key",
            "input_ids",
            "output_ids",
            "marker",
            "calls",
            "tokens",
            "status",
        }
        <= set(r)
        for r in rows.values()
    )


async def test_bad_phase_is_refused(seeded: Any, tmp_path: Path) -> None:
    eng, *_ = seeded
    with pytest.raises(ValueError):
        await dump_snapshot(eng, NS, tmp_path, "later")


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        snapshot.main(["--help"])
    assert exc.value.code == 0
    assert "--store" in capsys.readouterr().out


async def test_harness_dumps_after_ingest_and_sleep_only_when_inspecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memspine_evals.contracts import Turn
    from memspine_evals.systems.memspine_system import MemspineSystem

    doc = json.loads(DEMO.read_text(encoding="utf-8"))
    turns = [
        Turn(
            turn_id=t["turn_id"],
            session_id=s["session_id"],
            speaker=t["speaker"],
            text=t["text"],
            timestamp=None,
        )
        for s in doc["sessions"][:1]
        for t in s["turns"]
    ]

    async def run(directory: Path) -> None:
        monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(directory))
        system = MemspineSystem(
            build_sleep=True,
            config={"model": {"embedding": {"provider": "hash"}}, "dotenv_path": None},
        )
        system.begin_run("r--memspine")
        await system.reset("demo-1")
        for turn in turns:
            await system.insert(turn)
        await system.build()
        await system.close()

    monkeypatch.delenv("MEMSPINE_INSPECT", raising=False)
    await run(tmp_path / "off")
    assert not (tmp_path / "off" / "inspect").exists()

    monkeypatch.setenv("MEMSPINE_INSPECT", "1")
    await run(tmp_path / "on")
    out = tmp_path / "on" / "inspect"
    rows = _read(out / "records.jsonl")
    assert {r["phase"] for r in rows} == {"post_ingest", "post_sleep"}
    ingest = [r for r in rows if r["phase"] == "post_ingest"]
    assert len(ingest) == 10 and {r["turn_id"] for r in ingest} == {t.turn_id for t in turns}
    assert all(r["item"] == "demo-1" and r["run_id"] == "r--memspine" for r in rows)
