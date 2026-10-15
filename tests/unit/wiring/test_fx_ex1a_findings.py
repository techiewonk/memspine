"""FX-EX1A: fixes for the EX-1a milestone findings, model-free.

F1  a derived record in a multi-party namespace never widens its parents' visibility, with
    ``write.participants`` left at its default (off) once ``isolation.viewer_required`` is on
F4  ``Engine.verify_forget()`` is ``privacy.erasure_v2``-aware
F2  ``privacy.erasure_redact_hidden`` hard-forgets hidden dependants (no erased text on disk)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memspine import Engine
from memspine.config.schema import PrivacyConfig
from memspine.derive.write import write_derived
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.trust.visibility import restrict_to_parents, visibility_of, visible_to

NS = "acme/home"
T0 = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)
LANTERN = "Quillfeather"


def _engine(path: Path | None = None, **over: Any) -> Engine:
    cfg: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {"path": str(path / "m.db") if path else ":memory:"},
        "embedding": {"provider": "hash"},
        "read": {"hybrid": False, "record_access": False, "cache": {"enabled": True}},
        "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        "write": {"lineage_table": True},
    }
    for key, val in over.items():
        cfg[key] = {**cfg.get(key, {}), **val} if isinstance(val, dict) else val
    return Engine(**cfg)


def _isolated(path: Path | None = None, **over: Any) -> Engine:
    iso = {"scope_keys_v2": True, "viewer_required": True, **over.pop("isolation", {})}
    return _engine(path, isolation=iso, **over)


async def _private_turn(eng: Engine) -> MemoryRecord:
    """A private turn of ilse's in a two-person namespace (the way a multi-party app writes it)."""
    await eng.write(
        f"Ilse hides the {LANTERN} lantern in the cellar",
        namespace=NS,
        memory_type="episodic",
        participants=["ilse", "tomasz"],
        visibility="private",
    )
    (turn,) = await eng._require_started().list_records(NS, "episodic")
    return turn


async def _derive(
    eng: Engine, text: str, parents: list[str], *, tags: list[str] | None = None
) -> MemoryRecord:
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="assistant", channel="mining", parents=parents),
        tags=tags or [],
        valid_from=T0,
    )
    storage = eng._require_started()
    async with eng._write_locks.setdefault(NS, asyncio.Lock()):
        return await write_derived(eng, storage, NS, rec, "semantic", "system")


# ---------------------------------------------------------------------------------- F1


def test_restrict_to_parents_never_widens() -> None:
    parents = [["participant:ilse", "participant:tomasz", "visibility:private"]]
    assert restrict_to_parents([], parents) == [
        "participant:ilse",
        "participant:tomasz",
        "visibility:private",
    ]
    # an own participant the parents do not carry is dropped (it would widen the record)
    assert "participant:eve" not in restrict_to_parents(["participant:eve"], parents)
    assert restrict_to_parents(["participant:eve"], [["x"]]) == ["participant:eve"]
    # an own, broader visibility is raised to the strictest parent; an own stricter one is kept
    widened = restrict_to_parents(["topic", "visibility:owner_shared"], parents)
    assert visibility_of(widened) == "private" and "topic" in widened
    assert (
        visibility_of(restrict_to_parents(["visibility:private"], [["visibility:participants"]]))
        == "private"
    )
    # no tagged parent: unchanged
    assert restrict_to_parents(["x"], [["y"], []]) == ["x"]


async def test_f1_derived_fact_of_a_private_turn_is_not_served_to_a_non_participant() -> None:
    """The EX-1a finding: with write.participants=off the mined fact had no visibility tag."""
    eng = _isolated()
    await eng.start()
    try:
        assert eng.config.write.participants == "off"
        turn = await _private_turn(eng)
        fact = await _derive(eng, f"The {LANTERN} lantern is in the cellar", [turn.record_id])
        assert fact.tags and "visibility:private" in fact.tags  # inherited, not untagged
        assert [t for t in fact.tags if t.startswith("participant:")] == [
            "participant:ilse",
            "participant:tomasz",
        ]
        got = {
            r.record_id for r, _ in await eng.search(LANTERN, namespace=NS, viewer="eve", top_k=20)
        }
        assert fact.record_id not in got and turn.record_id not in got
        # the speaker still sees her own fact
        mine = {
            r.record_id for r, _ in await eng.search(LANTERN, namespace=NS, viewer="ilse", top_k=20)
        }
        assert fact.record_id in mine
    finally:
        await eng.stop()


async def test_f1_derived_never_wider_than_the_strictest_parent() -> None:
    eng = _isolated()
    await eng.start()
    try:
        turn = await _private_turn(eng)
        fact = await _derive(
            eng,
            "A cheerful fact",
            [turn.record_id],
            tags=["visibility:owner_shared", "participant:eve"],
        )
        assert visibility_of(fact.tags) == "private"  # not widened to owner_shared
        assert not visible_to(fact.tags, "eve")  # her own tag does not unlock a private parent
        assert visible_to(fact.tags, "ilse")
    finally:
        await eng.stop()


async def test_f1_raw_turns_in_a_declared_multi_party_namespace_carry_their_speakers() -> None:
    eng = _isolated(isolation={"multi_party_namespaces": [NS]})
    await eng.start()
    try:
        await eng.write_messages(
            [
                {"role": "user", "content": "Ilse: the lantern is in the cellar", "name": "ilse"},
                {"role": "user", "content": "Tomasz: I will not tell anyone", "name": "tomasz"},
            ],
            namespace=NS,
            session_id="s1",
        )
        turns = await eng._require_started().list_records(NS, "episodic")
        assert turns
        for turn in turns:  # participants=session behaviour, without the setting
            assert {"participant:ilse", "participant:tomasz"} <= {
                t.lower() for t in turn.tags if t.startswith("participant:")
            } or any(t.startswith("participant:") for t in turn.tags)
    finally:
        await eng.stop()


async def test_f1_flags_off_change_nothing() -> None:
    eng = _engine()  # isolation.viewer_required off
    await eng.start()
    try:
        turn = await _private_turn(eng)
        fact = await _derive(eng, f"The {LANTERN} lantern is in the cellar", [turn.record_id])
        assert not any(t.startswith(("participant:", "visibility:")) for t in fact.tags)
    finally:
        await eng.stop()


# ---------------------------------------------------------------------------------- F4 / F2


async def _turns(eng: Engine) -> tuple[MemoryRecord, MemoryRecord]:
    await eng.write_messages(
        [
            {"role": "user", "content": f"I keep a hive named {LANTERN} behind the mill"},
            {"role": "user", "content": "my sister plays the cello"},
        ],
        namespace="u",
        session_id="s1",
        valid_from=T0,
    )
    recs = {r.content: r for r in await eng._require_started().list_records("u", "episodic")}
    return recs[f"I keep a hive named {LANTERN} behind the mill"], recs["my sister plays the cello"]


async def _derive_u(eng: Engine, text: str, parents: list[str]) -> MemoryRecord:
    rec = MemoryRecord(
        namespace="u",
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="assistant", channel="mining", parents=parents),
        valid_from=T0,
    )
    async with eng._write_locks.setdefault("u", asyncio.Lock()):
        return await write_derived(eng, eng._require_started(), "u", rec, "semantic", "system")


def test_redact_flag_defaults_off() -> None:
    assert PrivacyConfig().erasure_redact_hidden is False


async def test_f4_verify_forget_is_erasure_v2_aware() -> None:
    eng = _engine(privacy={"erasure_v2": True})
    await eng.start()
    try:
        a, b = await _turns(eng)
        only_a = await _derive_u(eng, f"User keeps a hive named {LANTERN}", [a.record_id])
        both = await _derive_u(eng, "User and sister talk", [a.record_id, b.record_id])
        await eng.forget(a.record_id, namespace="u", hard=True)
        verdict = await eng.verify_forget(a.record_id, "u")
        assert verdict["erasure_v2"] is True
        assert verdict["descendants_hidden_pending_rederive"] == [only_a.record_id]
        assert verdict["descendants_kept"] == [both.record_id]
        assert verdict["descendants_leaked"] == []
        assert verdict["clean"] is True, verdict
    finally:
        await eng.stop()


async def test_f4_a_visible_dependant_with_no_other_parent_is_a_leak() -> None:
    eng = _engine(privacy={"erasure_v2": True})
    await eng.start()
    try:
        a, _ = await _turns(eng)
        only_a = await _derive_u(eng, f"User keeps a hive named {LANTERN}", [a.record_id])
        await eng.forget(a.record_id, namespace="u", hard=True)
        # simulate a release that made the hidden dependant visible without re-deriving it
        await eng.erasure_release([only_a.record_id], "u")
        verdict = await eng.verify_forget(a.record_id, "u")
        assert verdict["descendants_leaked"] == [only_a.record_id]
        assert verdict["clean"] is False
    finally:
        await eng.stop()


async def test_f4_v1_unchanged_when_erasure_v2_is_off() -> None:
    eng = _engine()
    await eng.start()
    try:
        a, _ = await _turns(eng)
        await eng.forget(a.record_id, namespace="u", hard=True)
        verdict = await eng.verify_forget(a.record_id, "u")
        assert verdict["erasure_v2"] is False
        assert verdict["descendants_leaked"] is None and verdict["descendants_kept"] is None
    finally:
        await eng.stop()


def _on_disk(db: Path, needle: str) -> list[str]:
    raw = needle.encode()
    return [
        p.name for p in (db, db.with_name(db.name + "-wal")) if p.exists() and raw in p.read_bytes()
    ]


async def test_f2_redact_hidden_removes_every_trace_of_the_erased_text(tmp_path: Path) -> None:
    eng = _engine(tmp_path, privacy={"erasure_v2": True, "erasure_redact_hidden": True})
    await eng.start()
    try:
        a, b = await _turns(eng)
        only_a = await _derive_u(eng, f"User keeps a hive named {LANTERN}", [a.record_id])
        both = await _derive_u(eng, "User and sister talk", [a.record_id, b.record_id])
        await eng.forget(a.record_id, namespace="u", hard=True)
        storage = eng._require_started()
        assert await storage.get_record(only_a.record_id) is None  # hard-forgotten, not kept
        assert await storage.get_record(both.record_id) is not None  # still supported: kept
        assert eng.erasure_queue() == []  # nothing for the offline runner to re-derive
        (entry,) = eng.erasure_queue(include_dropped=True)
        assert entry["record_id"] == only_a.record_id
        status = eng.erasure_status()
        assert status["queued"] == 0 and status["dropped_no_rederive"] == 1
        verdict = await eng.verify_forget(a.record_id, "u")
        assert verdict["clean"] is True and verdict["descendants_hidden_pending_rederive"] == []
        assert entry["state"] == "dropped_no_rederive"
    finally:
        await eng.stop()
    assert _on_disk(tmp_path / "m.db", LANTERN) == []


async def test_f2_off_keeps_the_hidden_row_and_its_text(tmp_path: Path) -> None:
    eng = _engine(tmp_path, privacy={"erasure_v2": True})
    await eng.start()
    try:
        a, _ = await _turns(eng)
        only_a = await _derive_u(eng, f"User keeps a hive named {LANTERN}", [a.record_id])
        await eng.forget(a.record_id, namespace="u", hard=True)
        assert await eng._require_started().get_record(only_a.record_id) is not None
        assert [i["record_id"] for i in eng.erasure_queue()] == [only_a.record_id]
        assert eng.erasure_status()["dropped_no_rederive"] == 0
    finally:
        await eng.stop()
    assert _on_disk(tmp_path / "m.db", LANTERN)  # the negative control: the scan can see it
