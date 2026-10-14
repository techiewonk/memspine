"""TI-1: inspect mode of the memspine adapter. Invented conversation (Bram, Ilse, Noor, Tomasz).

Inspect on: the tap writes jsonl into ``<forensics dir>/tap/``, each turn / query runs inside
``inspect_scope`` and ``forensics.jsonl`` keeps every FX key plus cuts and window.
Inspect off: nothing changes (no tap dir, no ``inspect`` block)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from memspine_evals.contracts import Turn
from memspine_evals.systems.memspine_system import MemspineSystem

_HASH = {"embedding": {"provider": "hash"}, "dotenv_path": None, "read": {"rerank": "off"}}


def _turns() -> list[Turn]:
    lines = [
        ("Bram", "I repotted the orchid on the balcony."),
        ("Ilse", "We booked a kayak tour along the northern coast."),
        ("Noor", "The violin recital starts at seven on Friday."),
        ("Tomasz", "My brother moved to a flat near the harbour."),
    ]
    return [
        Turn(
            turn_id=f"D1:{i + 1}",
            session_id="s1",
            speaker=who,
            text=text,
            timestamp=f"1:0{i} pm on {i + 1} May, 2023",
        )
        for i, (who, text) in enumerate(lines)
    ]


def _jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for p in sorted(path.glob("*.jsonl")):
        rows += [json.loads(line) for line in p.read_text("utf-8").splitlines()]
    return rows


def _events(tap_dir: Path) -> list[dict]:
    out: list[dict] = []

    def walk(t: dict) -> None:
        out.extend(t["events"])
        for c in t["children"]:
            walk(c)

    for trace in _jsonl(tap_dir):
        walk(trace)
    return out


async def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inspect: bool) -> tuple[Path, Path]:
    forensics = tmp_path / "fx"
    monkeypatch.setenv("MEMSPINE_FORENSICS_DIR", str(forensics))
    system = MemspineSystem(config=_HASH, inspect=inspect)
    system.begin_run("run-ti1")
    await system.reset("conv-1")
    for turn in _turns():
        await system.insert(turn)
    system.set_query_id("Q1")
    await system.query("Who repotted the orchid?", 800, 5)
    await system.close()
    return forensics, forensics / "tap"


async def test_inspect_on_writes_tap_and_full_forensics(monkeypatch, tmp_path) -> None:
    forensics, tap_dir = await _run(monkeypatch, tmp_path, True)
    events = _events(tap_dir)
    bound = [e["fields"] for e in events if e["stage"] == "write.turn.bound"]
    assert [b["turn_id"] for b in bound] == ["D1:1", "D1:2", "D1:3", "D1:4"]
    assert all(b["run_id"] == "run-ti1" and b["item"] == "conv-1" for b in bound)
    idx = [e["fields"] for e in events if e["stage"] == "store.index.record"]
    assert {i["record_id"] for i in idx} >= {b["record_id"] for b in bound}
    q = [e["fields"] for e in events if e["stage"] == "read.retrieve.writeback"]
    assert q and all(f["query_id"] == "Q1" for f in q)
    row = json.loads((forensics / "forensics.jsonl").read_text("utf-8").splitlines()[0])
    assert {"fx", "cuts", "window", "context_rids"} <= set(row["inspect"])
    assert "pool" in row["inspect"]["fx"] or "final" in row["inspect"]["fx"]


async def test_inspect_off_changes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MEMSPINE_INSPECT", raising=False)
    forensics, tap_dir = await _run(monkeypatch, tmp_path, False)
    assert not tap_dir.exists()
    row = json.loads((forensics / "forensics.jsonl").read_text("utf-8").splitlines()[0])
    assert "inspect" not in row


def test_env_switch(monkeypatch) -> None:
    monkeypatch.setenv("MEMSPINE_INSPECT", "1")
    assert MemspineSystem(config=_HASH)._inspect is True
    monkeypatch.delenv("MEMSPINE_INSPECT")
    assert MemspineSystem(config=_HASH)._inspect is False
