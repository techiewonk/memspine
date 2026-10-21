"""TI-7: every pipeline stage has a registered trace event, and every registered event is emitted.

``docs/v2/data/stage_trace_registry.json`` maps each stage of ``docs/v2/PIPELINE_STAGE_INVENTORY.md``
(A1.., B0.., C1..) to the tap events that trace it. A new stage (an inventory row, an envelope stage,
a sleep stage) without a registry entry fails here. A stage still short of events is listed with
``"gap": true`` and its F-number, so the list stays visible."""

from __future__ import annotations

import json
import re
from pathlib import Path

from memspine.workers import schedule

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "memspine"
REGISTRY = ROOT / "docs" / "v2" / "data" / "stage_trace_registry.json"
INVENTORY = ROOT / "docs" / "v2" / "PIPELINE_STAGE_INVENTORY.md"

_EMIT = re.compile(r"""\b_?tap\s*\.\s*(?:event|span|request)\(\s*"([A-Za-z0-9_.]+)\"""")
_ID = re.compile(r"^\|\s*([ABC]\d+[a-z]?)\s*\|", re.M)


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _emitted() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        names.update(_EMIT.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return names


def test_registry_matches_the_inventory() -> None:
    ids = set(_ID.findall(INVENTORY.read_text(encoding="utf-8")))
    reg = set(_registry()["stages"])
    assert ids, "no stage rows found in the inventory"
    assert not ids - reg, f"inventory stages without a registry entry: {sorted(ids - reg)}"
    assert not reg - ids, f"registry entries not in the inventory: {sorted(reg - ids)}"


def test_every_registered_event_is_emitted_in_src() -> None:
    emitted = _emitted()
    missing = {
        f"{sid}:{ev}"
        for sid, entry in _registry()["stages"].items()
        for ev in entry["events"]
        if ev not in emitted
    }
    assert not missing, f"registered events that no tap.event/span emits: {sorted(missing)}"


def test_every_stage_has_events_or_is_a_listed_gap() -> None:
    bad = []
    for sid, entry in _registry()["stages"].items():
        if entry.get("harness"):
            continue
        if not entry["events"] and not entry["gap"]:
            bad.append(sid)
        if entry["gap"] and not entry.get("gap_ref"):
            bad.append(f"{sid} (gap without an F-number)")
    assert not bad, f"stages with no trace event and no gap listing: {bad}"


def test_gap_refs_exist_in_the_inventory() -> None:
    fs = set(re.findall(r"^\|\s*(F\d+)\s*\|", INVENTORY.read_text(encoding="utf-8"), re.M))
    for sid, entry in _registry()["stages"].items():
        ref = entry.get("gap_ref")
        if ref and re.fullmatch(r"F\d+", ref):
            assert ref in fs, f"{sid}: {ref} is not a gap in the inventory"


def _envelope_stage_names() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"EnvelopeStage\(\s*(?:name=)?(\"[a-z_]+\"|[A-Z_]+)", text):
            arg = m.group(1)
            if arg.startswith('"'):
                names.add(arg.strip('"'))
            else:
                const = re.search(rf"^{arg}\s*=\s*\"([a-z_]+)\"", text, re.M)
                assert const, f"cannot resolve envelope stage name {arg} in {path}"
                names.add(const.group(1))
    return names


def test_every_envelope_stage_is_registered() -> None:
    reg = _registry()
    names = _envelope_stage_names()
    assert len(names) >= 6, names  # temporal, grounding, coverage, resolve, integration, prepass
    assert not names - set(reg["envelope_stages"]), sorted(names - set(reg["envelope_stages"]))
    for name, sid in reg["envelope_stages"].items():
        assert sid in reg["stages"], f"envelope stage {name} -> unknown stage {sid}"


def test_every_sleep_stage_is_registered() -> None:
    names = set(schedule.SLEEP_CYCLE_ORDER) | {
        v for k, v in vars(schedule).items() if k.endswith("_STAGE") and isinstance(v, str)
    }
    reg = _registry()
    assert not names - set(reg["sleep_stages"]), sorted(names - set(reg["sleep_stages"]))
    for name, sid in reg["sleep_stages"].items():
        assert sid in reg["stages"], f"sleep stage {name} -> unknown stage {sid}"


def test_the_gap_list_is_visible() -> None:
    gaps = {sid: e["gap_ref"] for sid, e in _registry()["stages"].items() if e["gap"]}
    assert gaps  # the known gaps are listed, not hidden; shrink this by adding events
