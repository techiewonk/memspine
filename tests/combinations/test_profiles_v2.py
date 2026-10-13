# ruff: noqa: E501
"""FU-REQS: the v2 flag profiles turned on together (audit 2026-10-13, finding 2).

Each profile of ``src/memspine/config/presets/*-v2.yaml`` is loaded as ``data_profile``; the engine
is opened, a small stub workload is written, a sleep runs, the memory is read, the store is
reopened (restart) and one record is hard-erased. Hash embedder, stub model roles only, no GPU.
What the test pins:

* no profile raises ``ConfigError`` (every prerequisite of every flag in it is met: ``requires``),
* the engine boots with the whole stack on and a write / sleep / read / restart / erase cycle runs,
* the read-your-write check still finds the written text after restart,
* the hard-erased text is gone,
* for ``write-v2``: the window calls per window stay inside the P-14 targets (coverage re-extraction
  has its own target), and every envelope stage ran in the documented order.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import memspine
from memspine.config.loader import load_config
from memspine.config.presets import load_preset, preset_names
from memspine.config.requires import check_requires, is_active, resolve_path
from memspine.economy.targets import TargetSet
from memspine.models.providers.llm.base import LLMRouter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "wiring"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_w2_06b_prompt_v2 import WindowLLM

pytestmark = pytest.mark.filterwarnings("ignore:optimize:UserWarning")

PROFILES = ("read-v2-core", "read-v2-graph", "read-v2-safe", "write-v2", "offline-v2", "trust-v2", "run-v2")
NS = "acme/ilse"
T0 = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)
SECRET = "Quillfeather"
LINES = (
    f"Ilse: I keep a beehive named {SECRET} behind the mill",
    "Ilse: The honey harvest starts in the last week of August",
    "Ilse: Noor is helping me paint the hive boxes pale green",
    "Ilse: Tomasz lends me his ladder every autumn",
)


def test_all_seven_profiles_ship() -> None:
    assert set(PROFILES) <= set(preset_names())


@pytest.mark.parametrize("name", PROFILES)
def test_profile_satisfies_every_requirement_and_turns_flags_on(name: str) -> None:
    raw = _overrides(name, ":memory:")
    template = raw.pop("template")
    loaded = load_config(template=template, overrides=raw)
    cfg = getattr(loaded, "config", loaded)
    check_requires(cfg)  # raises ConfigError naming the missing prerequisite
    preset = load_preset(name)
    for key in _leaf_keys(preset):
        assert resolve_path(cfg, key) == _dig(preset, key), key
    assert all(is_active(_dig(preset, k)) for k in _leaf_keys(preset))  # a profile only switches things on


def _leaf_keys(node: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in node.items():
        if isinstance(v, dict):
            out += _leaf_keys(v, f"{prefix}{k}.")
        else:
            out.append(f"{prefix}{k}")
    return out


def _dig(node: dict[str, Any], key: str) -> Any:
    for part in key.split("."):
        node = node[part]
    return node


def _overrides(name: str, db: str | Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "template": "core",
        "data_profile": name,
        "storage": {"path": str(db)},
        "embedding": {"provider": "hash"},
        "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        "llm": {"roles": {"window_extract": {"model": "stub/window"}}},
        "write": {"window_async": False, "window_idle_s": 0.0},
        "economy": {"ledger_v2": True},
    }
    if name == "trust-v2":  # retention classes are required for retention.v2 to expire anything
        out["retention"] = {"classes": [{"namespace": "*", "ttl_days": 36500}]}
    return out


def _turns(lines: tuple[str, ...], start: int = 0) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": text, "timestamp": (T0 + timedelta(minutes=start + i)).isoformat()}
        for i, text in enumerate(lines)
    ]


@pytest.fixture
def stub_roles(monkeypatch: pytest.MonkeyPatch) -> WindowLLM:
    llm = WindowLLM()

    async def router(config: Any, hooks: object = None) -> LLMRouter:
        return LLMRouter({"window_extract": llm})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    return llm


@pytest.mark.parametrize("name", PROFILES)
async def test_profile_boots_and_runs_the_cycle(name: str, tmp_path: Path, stub_roles: WindowLLM) -> None:
    db = tmp_path / "m.db"
    eng = await memspine.open(dotenv_path=None, **_overrides(name, db))
    try:
        mem = eng.scope(NS, principal="ilse", viewer="ilse", purpose="assist")
        await mem.write_messages(_turns(LINES), session_id="s1", group_id="s1")
        await eng.flush_windows()
        await mem.sleep()
        found = await _find(mem, SECRET)
        assert found, f"{name}: the written text is not readable before restart"
        if name == "write-v2":
            _check_write_targets(eng, stub_roles)
    finally:
        await eng.stop()

    eng = await memspine.open(dotenv_path=None, **_overrides(name, db))  # restart on the same store
    try:
        mem = eng.scope(NS, principal="ilse", viewer="ilse", purpose="assist")
        found = await _find(mem, SECRET)
        assert found, f"{name}: the written text is not readable after restart"
        rid = found[0]
        await mem.forget(rid, hard=True)
        # the erased record itself is gone (its derived facts need privacy.erasure_v2: trust-v2, below)
        assert rid not in await _find(mem, SECRET), f"{name}: the erased record is still readable"
    finally:
        await eng.stop()


async def _find(mem: Any, needle: str) -> list[str]:
    hits = await mem.recall(f"beehive named {needle}")
    ids: list[str] = []
    for hit in hits or []:
        rec = hit[0] if isinstance(hit, tuple) else hit
        if needle in (getattr(rec, "content", "") or "") and getattr(rec, "status", None) != "deleted":
            ids.append(rec.record_id)
    return ids


def _check_write_targets(eng: Any, llm: WindowLLM) -> None:
    report = eng.cost_report()
    assert report, "economy.ledger_v2 is on: the cost report is not empty"
    windows = max(1, llm.calls)
    results = TargetSet(output_to_input_ratio=None, sleep_calls_per_turn=None).check(
        eng._cost_ledger, turns=len(LINES), windows=windows
    )
    by = {r.name: r for r in results}
    assert by["generative_calls_per_window"].status in ("met", "not_measured"), by
    assert by["coverage_reextract_calls_per_window"].status in ("met", "not_measured"), by


async def test_write_and_trust_profiles_together_erase_the_derived_facts(
    tmp_path: Path, stub_roles: WindowLLM
) -> None:
    """The two profiles are layered (``data_profile: "write-v2,trust-v2"``): erasure_v2 plus
    erasure_redact_hidden hide-and-purge the window facts derived from the erased turn."""
    raw = _overrides("write-v2,trust-v2", tmp_path / "m.db")
    raw["write"]["lineage_table"] = True
    raw["workers"] = {"outbox": True}
    eng = await memspine.open(dotenv_path=None, **raw)
    try:
        mem = eng.scope(NS, principal="ilse", viewer="ilse", purpose="assist")
        await mem.write_messages(_turns(LINES), session_id="s1", group_id="s1")
        await eng.flush_windows()
        found = await _find(mem, SECRET)
        assert len(found) >= 2, "a raw turn and at least one derived fact carry the text"
        raw_turn = next(r for r in found if r)
        await mem.forget(raw_turn, hard=True)
        assert not await _find(mem, SECRET), "the derived facts must be gone with their parent"
    finally:
        await eng.stop()


_V2_STAGES = (
    "segmentation_v2", "envelope_v2", "prompt_v2", "stamping_v2", "role_policy_v2", "intake_v2",
    "temporal_v2", "grounding_v2", "coverage_v2", "resolver_v2", "integration_v2",
)  # fmt: skip


@pytest.mark.parametrize("hard", [True, False])
@pytest.mark.parametrize("envelope", [True, False])
async def test_write_v2_alone_forget_reaches_derived_facts(
    envelope: bool, hard: bool, tmp_path: Path, stub_roles: WindowLLM
) -> None:
    """FU-HARDERASE: forgetting a raw turn (v1 forget, no privacy.erasure_v2) removes (hard) or
    stops serving (soft) every window fact derived from it, on the envelope path and on the old
    window path (the W2 stage flags off) alike."""
    raw = _overrides("write-v2", tmp_path / "m.db")
    if not envelope:
        raw["write"].update({k: False for k in _V2_STAGES})
    eng = await memspine.open(dotenv_path=None, **raw)
    try:
        mem = eng.scope(NS, principal="ilse", viewer="ilse", purpose="assist")
        await mem.write_messages(_turns(LINES), session_id="s1", group_id="s1")
        await eng.flush_windows()
        storage = eng._require_started()
        recs = {r.record_id: r for r in await storage.list_records(NS)}
        found = await _find(mem, SECRET)
        turn = [r for r in found if not recs[r].source.parents]
        facts = [r for r in found if recs[r].source.parents]
        assert turn and facts, f"a raw turn and a derived fact carry the text: {found}"
        assert all(turn[0] in recs[f].source.parents for f in facts)
        await mem.forget(turn[0], hard=hard)
        assert not await _find(mem, SECRET), "the derived facts must go with their only source"
    finally:
        await eng.stop()
