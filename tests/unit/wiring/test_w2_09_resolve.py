"""W2-09: online entity resolution with pending_merge (``write.resolver``; W-19, M-15).

Model-free: invented names (Noor, Ilse, Bram, Tomasz), the hash embedder or a scripted vector table,
a stub window LLM for the one end-to-end window."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import FakeWindowLLM, _send

from memspine import Engine
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.config.schema import MemspineConfig
from memspine.derive import resolve_stage as rs
from memspine.derive.compile import WindowLine
from memspine.derive.entity_registry import EntityRegistry
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.envelope_stages import EnvelopeStageContext
from memspine.derive.items import ItemValidator
from memspine.derive.resolve_calibration import calibrate, evaluate, synthetic_cases
from memspine.derive.resolve_stage import EntityResolveStage, ResolveSettings
from memspine.exceptions import StorageError
from memspine.ingest.windows import _envelope_stages
from memspine.observability import tap
from memspine.schemas.durable import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.reserved import reserved_hits
from memspine.services.embedding.hash import HashEmbedding
from memspine.services.llm.base import LLMRouter
from memspine.services.storage.projector import fold_records
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.rebuild import ReplayBuilder
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.registry_evidence import scrub_registry_marker

NOW = datetime(2023, 6, 1, 12, 0, tzinfo=UTC)
SCOPE_A = OwnerScope(tenant="t", owner="ilse")
SCOPE_B = OwnerScope(tenant="t", owner="bram")
KEY_A = ("t", "ilse")
HASH = HashEmbedding().embed


# --- helpers -------------------------------------------------------------------------------------
def _envs(scope: OwnerScope, *items: tuple[str, str | None, str]) -> list[Any]:
    """Compile one envelope per ``(subject, object, text)``."""
    lines = {
        n: WindowLine(
            turn_id=f"t{n}",
            source_id=f"t{n}",
            source_version=1,
            seg_index=n,
            role="user",
            speaker="Ilse",
            source_at=NOW,
            tz="UTC",
            content=text,
        )
        for n, (_s, _o, text) in enumerate(items, 1)
    }
    compiler = EnvelopeCompiler(clock=lambda: NOW)
    ctx = compiler.context(scope, "w1", lines)
    raw = [
        {
            "s": s,
            "r": "mentioned",
            "o": o,
            "text": text,
            "kind": "event",
            "mode": "asserted",
            "neg": False,
            "lines": [n],
            "span": text,
        }
        for n, (s, o, text) in enumerate(items, 1)
    ]
    out = ItemValidator(compiler).validate(raw, ctx)
    assert not out.invalid, out.invalid
    return list(out.envelopes)


def _ctx(ns: str = "t/ilse", envs: list[Any] | None = None) -> EnvelopeStageContext:
    sources = {
        (e.turn.source_id, e.turn.source_version)
        for env in envs or []
        for e in env.evidence
        if e.turn is not None
    }
    return EnvelopeStageContext(
        namespace=ns,
        window_id="w1",
        lines={},
        records=[
            SimpleNamespace(
                record_id=rid,
                version=version,
                namespace=ns,
                memory_type="episodic",
                status=RecordStatus.ACTIVATED,
                quarantined=False,
            )
            for rid, version in sorted(sources)
        ],
        compile_ctx=None,
    )


class Vectors:
    """A scripted embedder: names in the table get the given vector, anything else a hash vector."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table
        self.calls = 0

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        base = await HASH(texts)
        return [self.table.get(t.casefold(), v) for t, v in zip(texts, base, strict=True)]


def _stage(**kw: Any) -> EntityResolveStage:
    embed = kw.pop("embed", HASH)
    return EntityResolveStage(ResolveSettings(**kw), embed=embed)


async def _run(
    stage: EntityResolveStage, scope: OwnerScope, *items: tuple[str, str | None, str]
) -> list[Any]:
    envs = _envs(scope, *items)
    return await stage.run(envs, _ctx(f"{scope.tenant}/{scope.owner}", envs))


# --- config and wiring ---------------------------------------------------------------------------
def test_flag_is_off_by_default_and_stage_registered_only_when_on() -> None:
    cfg = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    )
    assert cfg.write.resolver is False
    assert (cfg.write.resolve_high, cfg.write.resolve_grey, cfg.write.resolve_cache) == (
        0.9,
        0.7,
        1024,
    )

    class Eng:
        _embedder = HashEmbedding()
        _storage = None

    assert _envelope_stages(Eng(), cfg) == ()
    on = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        },
        write={"window_extract": "facts", "resolver": True},
    )
    (stage,) = _envelope_stages(Eng(), on)
    assert stage.name == "resolve" and stage.order == 300


# --- rules: aliases merge, homonyms stay apart -----------------------------------------------------
async def test_alias_merges_to_the_canonical_id_and_records_the_alias() -> None:
    st = _stage()
    a, b = await _run(
        st,
        SCOPE_A,
        ("Tomasz Brandt", None, "Tomasz Brandt lives in Utrecht and bakes sourdough."),
        ("Tom Brandt", None, "Tom Brandt plays the cello near Utrecht."),
    )
    assert a.subject.entity_id == b.subject.entity_id
    assert b.extras["resolve"]["subject"]["verdict"] == "existing"
    (ent,) = st.registry.active(KEY_A)
    assert "Tom Brandt" in ent.aliases
    assert reserved_hits(b.extras) == []  # nothing like *_id inside the extras


async def test_a_later_alias_matches_through_the_alias_table() -> None:
    st = _stage()
    out = await _run(
        st,
        SCOPE_A,
        ("Tomasz Brandt", None, "Tomasz Brandt lives in Utrecht."),
        ("Tom Brandt", None, "Tom Brandt cycles in Utrecht."),
        ("TOM BRANDT", None, "TOM BRANDT keeps bees in Utrecht."),
    )
    assert len({e.subject.entity_id for e in out}) == 1


async def test_homonyms_with_different_places_stay_two_entities() -> None:
    st = _stage()
    a, b = await _run(
        st,
        SCOPE_A,
        ("Noor Haddad", None, "Noor Haddad lives in Utrecht and plays the cello."),
        (
            "Noor Haddad",
            None,
            "Last spring Noor Haddad moved to Delft; Noor Haddad restores clocks.",
        ),
    )
    assert a.subject.entity_id != b.subject.entity_id
    assert b.extras["resolve"]["subject"]["verdict"] == "new"
    assert len(st.registry.active(KEY_A)) == 2 and not st.registry.pending(KEY_A)


async def test_same_given_name_other_surname_is_a_new_entity() -> None:
    st = _stage()
    a, b = await _run(
        st,
        SCOPE_A,
        ("Noor Haddad", None, "Noor Haddad lives in Utrecht."),
        ("Noor Visser", None, "Noor Visser lives in Utrecht."),
    )
    assert a.subject.entity_id != b.subject.entity_id
    assert b.extras["resolve"]["subject"]["verdict"] == "new"


async def test_object_mentions_are_resolved_too() -> None:
    st = _stage()
    a, b = await _run(
        st,
        SCOPE_A,
        ("Ilse", "Bram Vos", "Ilse met Bram Vos in Leiden."),
        ("Ilse", "bram vos", "Ilse called bram vos from Leiden."),
    )
    assert a.object.entity_id == b.object.entity_id and a.subject.entity_id == b.subject.entity_id


# --- grey zone: pending_merge, never merged --------------------------------------------------------
async def test_rule_grey_zone_attaches_pending_merge_and_does_not_merge() -> None:
    st = _stage()
    a, b = await _run(
        st,
        SCOPE_A,
        ("Noor Haddad", None, "Noor Haddad plays chess."),
        ("Noor", None, "Noor bakes bread."),
    )
    info = b.extras["resolve"]["subject"]
    assert info["verdict"] == "pending_merge" and info["pending_with"] == [a.subject.entity_id]
    assert b.subject.entity_id != a.subject.entity_id  # provisional id, separate entity
    assert len(st.registry.active(KEY_A)) == 2
    (pending,) = st.registry.pending(KEY_A)
    assert pending.entity_id == b.subject.entity_id and pending.pending_with == [
        a.subject.entity_id
    ]


async def test_embedding_bands_merge_pending_new_and_context_vetoes() -> None:
    vec = Vectors(
        {
            "nyc": [1.0, 0.0, 0.0],
            "new york city": [0.95, 0.31, 0.0],  # cos ~0.95: merge
            "big apple": [0.75, 0.0, 0.661],  # cos 0.75 / 0.71: grey
            "tokyo": [0.0, 0.0, 1.0],  # cos 0: new
        }
    )
    st = _stage(embed=vec, high=0.9, grey=0.7)
    out = await _run(
        st,
        SCOPE_A,
        ("NYC", None, "NYC hosts the marathon."),
        ("New York City", None, "New York City hosts the marathon."),
        ("Big Apple", None, "Big Apple hosts the marathon."),
        ("Tokyo", None, "Tokyo hosts the marathon."),
    )
    verdicts = [e.extras["resolve"]["subject"] for e in out]
    assert [v["verdict"] for v in verdicts] == ["new", "existing", "pending_merge", "new"]
    assert verdicts[1]["method"] == "embed" and verdicts[2]["method"] == "embed_grey"
    assert out[1].subject.entity_id == out[0].subject.entity_id
    assert verdicts[2]["pending_with"] == [out[0].subject.entity_id]
    assert out[2].subject.entity_id != out[0].subject.entity_id

    # a high-cosine pair whose contexts name different towns is a homonym: the veto keeps it apart
    st2 = _stage(embed=Vectors({"haddad": [1.0, 0.0], "haddad bakery": [1.0, 0.0]}))
    a, b = await _run(
        st2,
        SCOPE_A,
        ("Haddad", None, "Haddad runs the bakery in Utrecht."),
        ("Haddad Bakery", None, "Haddad Bakery opened a shop in Delft."),
    )
    assert a.subject.entity_id != b.subject.entity_id
    assert b.extras["resolve"]["subject"]["verdict"] != "existing"


async def test_without_an_embedder_only_the_rules_decide() -> None:
    st = _stage(embed=None)
    a, b = await _run(
        st,
        SCOPE_A,
        ("NYC", None, "NYC hosts it."),
        ("New York City", None, "New York City hosts it."),
    )
    assert a.subject.entity_id != b.subject.entity_id


# --- MERGE and SPLIT -------------------------------------------------------------------------------
async def test_split_reverts_a_merge_with_one_event() -> None:
    st = _stage()
    a, b = await _run(
        st,
        SCOPE_A,
        ("Noor Haddad", None, "Noor Haddad plays chess."),
        ("Noor", None, "Noor bakes bread."),
    )
    reg = st.registry
    before = reg.snapshot()
    keep, drop = a.subject.entity_id, b.subject.entity_id
    mid = reg.merge(KEY_A, keep, drop, reason="sleep: same person")
    assert reg.get(KEY_A, drop).merged_into == keep
    assert reg.canonical(KEY_A, drop) == keep and [e.entity_id for e in reg.active(KEY_A)] == [keep]
    assert "Noor" in reg.get(KEY_A, keep).aliases
    # a mention of the same name now sees only the survivor (the dropped entity is no candidate)
    (c,) = await _run(st, SCOPE_A, ("Noor", None, "Noor repairs bicycles."))
    info = c.extras["resolve"]["subject"]
    assert drop not in info["pending_with"] and (
        info["verdict"] == "existing" or info["pending_with"] == [keep]
    )
    reg.drain()
    assert reg.split(mid) is True
    payloads = reg.drain()
    assert [p["marker"] for p in payloads] == ["entity.split"]
    assert reg.get(KEY_A, drop).merged_into is None
    assert "Noor" not in reg.get(KEY_A, keep).aliases
    assert reg.split(mid) is False  # reverting twice does nothing
    after = {k: dict(v) for k, v in reg.snapshot().items()}
    assert after[KEY_A][keep][0] == before[KEY_A][keep][0] and after[KEY_A][drop][4] is None


def test_merge_refuses_cross_scope_and_dead_entities() -> None:
    reg = EntityRegistry()
    reg.create(KEY_A, "e1", "Ilse Brandt")
    reg.create(("t", "bram"), "e2", "Ilse Brandt")
    reg.create(KEY_A, "e3", "I. Brandt")
    with pytest.raises(ValueError):
        reg.merge(KEY_A, "e1", "e2")  # e2 is another owner's entity
    reg.merge(KEY_A, "e1", "e3")
    with pytest.raises(ValueError):
        reg.merge(KEY_A, "e1", "e3")  # already merged
    with pytest.raises(ValueError):
        reg.merge(KEY_A, "e1", "e1")


# --- scope isolation -------------------------------------------------------------------------------
async def test_entities_never_cross_owners() -> None:
    st = _stage()
    (a,) = await _run(st, SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt lives in Leiden."))
    (b,) = await _run(st, SCOPE_B, ("Ilse Brandt", None, "Ilse Brandt lives in Leiden."))
    assert a.subject.entity_id != b.subject.entity_id  # same name and context, other owner
    assert b.extras["resolve"]["subject"]["verdict"] == "new"
    assert len(st.registry.active(KEY_A)) == 1 and len(st.registry.active(("t", "bram"))) == 1
    # the session is provenance only: a second session of the same owner shares the entity
    (c,) = await _run(st, SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt lives in Leiden too."))
    assert c.subject.entity_id == a.subject.entity_id


async def test_registry_always_uses_typed_owner_scope() -> None:
    stage = _stage()
    envs = _envs(SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt lives in Leiden."))
    await stage.run(envs, _ctx("ns-one", envs))
    assert len(stage.registry.active(KEY_A)) == 1
    assert not stage.registry.active(("ns-one",))


# --- replay ----------------------------------------------------------------------------------------
async def test_registry_replays_from_payloads_and_through_replay(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "src.db")))
    await client.open()
    src = SQLiteStorage(client)
    await src.start()
    tclient = SQLiteClient(SQLiteSpec(str(tmp_path / "dst.db")))
    await tclient.open()
    dst = SQLiteStorage(tclient)
    await dst.start()
    try:
        for n in range(1, 5):
            await src.upsert_record(
                MemoryRecord(
                    record_id=f"t{n}", namespace="t/ilse", memory_type="episodic", content="source"
                )
            )
        st = EntityResolveStage(ResolveSettings(), embed=HASH, storage=lambda: src)
        out = await _run(
            st,
            SCOPE_A,
            ("Tomasz Brandt", None, "Tomasz Brandt lives in Utrecht."),
            ("Tom Brandt", None, "Tom Brandt cycles in Utrecht."),
            ("Noor Haddad", None, "Noor Haddad plays chess."),
            ("Noor", None, "Noor bakes bread."),
        )
        keep, drop = out[2].subject.entity_id, out[3].subject.entity_id
        mid = st.registry.merge(
            KEY_A, keep, drop, reason="private merge context", namespace="t/ilse"
        )
        await st.flush("t/ilse")
        st.registry.split(mid, namespace="t/ilse")
        st.registry.merge(KEY_A, keep, drop, namespace="t/ilse")
        await st.flush("t/ilse")
        events = await src.read_events(after_seq=0, limit=1000)
        assert "private merge context" not in json.dumps([e.payload for e in events])
        markers = [e.payload["marker"] for e in events if e.kind is EventKind.MARKER]
        assert "entity.merge" in markers and "entity.split" in markers
        rebuilt = EntityRegistry.replay(e.payload for e in events if e.kind is EventKind.MARKER)
        assert rebuilt.snapshot() == st.registry.snapshot()

        # F-16b: the markers travel through the migration replay unchanged
        report = await ReplayBuilder(src, dst).run()
        assert report.by_kind[EventKind.MARKER.value] == len(markers)
        moved = await dst.read_events(after_seq=0, limit=1000)
        again = EntityRegistry.replay(e.payload for e in moved if e.kind is EventKind.MARKER)
        assert again.snapshot() == st.registry.snapshot()

        # a restarted stage reloads the registry lazily and keeps resolving to the same ids
        fresh = EntityResolveStage(ResolveSettings(), embed=HASH, storage=lambda: src)
        (c,) = await _run(
            fresh, SCOPE_A, ("TOMASZ BRANDT", None, "TOMASZ BRANDT keeps bees in Utrecht.")
        )
        assert c.subject.entity_id == out[0].subject.entity_id
    finally:
        await client.close()
        await tclient.close()


@pytest.mark.parametrize("fence_timing", ["before", "during", "missed_cursor"])
async def test_replay_sanitizes_interrupted_registry_erasure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fence_timing: str
) -> None:
    source_client = SQLiteClient(SQLiteSpec(str(tmp_path / "replay_source.db")))
    target_client = SQLiteClient(SQLiteSpec(str(tmp_path / "replay_target.db")))
    await source_client.open()
    await target_client.open()
    source = SQLiteStorage(source_client)
    target = SQLiteStorage(target_client)
    await source.start()
    await target.start()
    try:
        record = MemoryRecord(
            record_id="s", namespace="t/ilse", memory_type="episodic", content="source"
        )
        await source.upsert_record(record)
        marker = MemoryEvent(
            kind=EventKind.MARKER,
            namespace="t/ilse",
            actor="resolver",
            payload={
                "marker": "entity.create",
                "scope": ["t", "ilse"],
                "entity": "old",
                "name": "Erased Name",
                "source_ids": ["s"],
                "source_versions": [1],
            },
        )
        await source.append_registry_marker(marker)
        # A historical control marker may contain copied text from the old
        # schema. Replay must scrub it even if the source erase stopped after
        # committing the fence and before historical marker redaction.
        async with source_client.engine.begin() as conn:
            await conn.execute(
                source._event_insert(
                    MemoryEvent(
                        kind=EventKind.MARKER,
                        namespace="t/ilse",
                        actor="legacy",
                        payload={
                            "marker": "entity.merge",
                            "scope": ["t", "ilse"],
                            "merge": "old_merge",
                            "keep": "old",
                            "drop": "other",
                            "reason": "Erased Reason",
                            "added_aliases": ["Erased Alias"],
                        },
                    )
                )
            )
        if fence_timing != "before":
            append = target._append_replayed_event

            async def fence_after_marker(event: MemoryEvent) -> MemoryEvent:
                copied = await append(event)
                if event.payload.get("name") == "Erased Name":
                    await source.begin_registry_erasure("t/ilse", ["s"])
                return copied

            monkeypatch.setattr(target, "_append_replayed_event", fence_after_marker)
            if fence_timing == "missed_cursor":
                read = source.read_events
                skipped = False

                async def read_with_lower_seq_gap(
                    after_seq: int = 0, limit: int = 1000
                ) -> list[MemoryEvent]:
                    nonlocal skipped
                    if after_seq >= 1 and not skipped:
                        skipped = True
                        return []  # model an uncommitted lower-seq PG fence
                    return await read(after_seq=after_seq, limit=limit)

                monkeypatch.setattr(source, "read_events", read_with_lower_seq_gap)
        else:
            await source.begin_registry_erasure("t/ilse", ["s"])
        await ReplayBuilder(source, target, batch_size=1).run()
        events = await target.read_events(after_seq=0, limit=1000)
        assert not any(e.payload.get("name") == "Erased Name" for e in events)
        assert "Erased Reason" not in json.dumps([e.payload for e in events])
        assert "Erased Alias" not in json.dumps([e.payload for e in events])
        assert any(e.kind is EventKind.REGISTRY_ERASURE_FENCE for e in events)
        stage = EntityResolveStage(storage=lambda: target)
        await stage._load()
        assert not stage.registry.active(KEY_A)
        await target.upsert_record(record)  # row remains after interrupted erase
        with pytest.raises(StorageError, match="being erased"):
            await target.append_registry_marker(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace="t/ilse",
                    actor="resolver",
                    payload=dict(marker.payload),
                )
            )
    finally:
        await source_client.close()
        await target_client.close()


# --- bounded caches and prefilter --------------------------------------------------------------------
async def test_caches_are_bounded() -> None:
    st = _stage(max_cache=8)
    items = [
        (f"Ilse{chr(97 + i)} Brandt", None, f"Ilse{chr(97 + i)} Brandt lives in Leiden.")
        for i in range(20)
    ]
    await _run(st, SCOPE_A, *items)
    assert len(st._vec) <= 8 and len(st._rules) <= 8  # type: ignore[arg-type]


async def test_prefilter_keeps_the_alias_candidate_in_a_big_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rs, "PREFILTER_ABOVE", 10)
    st = _stage()
    filler = [(f"Person{i} Zzyzx{i}", None, f"Person{i} Zzyzx{i} waves.") for i in range(30)]
    first = await _run(
        st, SCOPE_A, ("Tomasz Brandt", None, "Tomasz Brandt lives in Utrecht."), *filler
    )
    (b,) = await _run(st, SCOPE_A, ("Tom Brandt", None, "Tom Brandt cycles in Utrecht."))
    assert b.subject.entity_id == first[0].subject.entity_id


# --- taps --------------------------------------------------------------------------------------------
async def test_taps_report_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "enabled", lambda: True)
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    st = _stage()
    await _run(
        st,
        SCOPE_A,
        ("Noor Haddad", None, "Noor Haddad plays chess."),
        ("Noor", None, "Noor bakes bread."),
    )
    stages = [s for s, _ in seen]
    assert stages.count("write.resolve.mention") == 2 and "write.resolve.window" in stages
    verdicts = [f["verdict"] for s, f in seen if s == "write.resolve.mention"]
    assert verdicts == ["new", "pending_merge"]
    assert all(s.startswith("write.resolve.") for s in stages)


# --- calibration gate (W-19: no homonym merges above the stated rate) ---------------------------------
async def test_shipped_thresholds_meet_the_homonym_gate_on_both_halves() -> None:
    cfg = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    ).write
    cases = synthetic_cases()
    assert {c.cls for c in cases} == {"alias", "homonym", "unrelated"}
    for half in (0, 1):
        conf = await evaluate(cases, cfg.resolve_high, cfg.resolve_grey, HASH, half=half)
        assert conf.n("homonym") >= 90
        assert conf.homonym_merge_rate <= 0.01, conf.table()
        assert conf.unrelated_merge_rate <= 0.01, conf.table()
        assert conf.alias_recall >= 0.3
    # the gate is not vacuous: a low `high` merges homonyms (the family name alone vs the full name)
    loose = await evaluate(cases, 0.6, 0.5, HASH)
    assert loose.homonym_merge_rate > 0.05


async def test_calibrate_picks_a_pair_that_holds_on_the_held_out_half() -> None:
    cases = synthetic_cases()
    high, grey, dev = await calibrate(cases, HASH, max_rate=0.01)
    assert grey <= high and dev.homonym_merge_rate <= 0.01
    test = await evaluate(cases, high, grey, HASH, half=1)
    assert test.homonym_merge_rate <= 0.01 and test.unrelated_merge_rate <= 0.01


# --- end to end: one window, write.envelope + write.resolver -----------------------------------
LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class WhoLLM:
    """Answers the structured extraction: one item per line, subject = the speaker, object = a named
    person when the line says ``with <Name>``."""

    provider_id = "fake:who"

    def __init__(self) -> None:
        self.sections = FakeWindowLLM()

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        items = []
        for n, text in LINE.findall(messages[-1]["content"]):
            who, said = text.split(": ", 1)
            m = re.search(r"with (\w+ \w+)", said)
            items.append(
                {
                    "s": who,
                    "r": "said",
                    "o": m.group(1) if m else None,
                    "text": f"{who} said {said}",
                    "kind": "event",
                    "mode": "asserted",
                    "neg": False,
                    "when": None,
                    "until": None,
                    "lines": [int(n)],
                    "span": said,
                    "speaker": who,
                }
            )
        return json.dumps({"facts": items})


async def _engine(monkeypatch: pytest.MonkeyPatch, path: Path, **write: Any) -> Engine:
    llm = WhoLLM()

    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": llm})

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": "test-deployment"},  # RM-155: shared LLM
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "window_extract": {
                    "provider": "openai_compatible",
                    "timeout_s": 60.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "fake/window"},
                }
            },
        },
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            **write,
        },
    )
    await eng.start()
    return eng


def _msgs(*lines: str) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": text, "created_at": f"2023-06-01T10:0{i}:00+00:00"}
        for i, text in enumerate(lines)
    ]


async def test_end_to_end_window_resolves_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "on.db", resolver=True)
    try:
        await _send(
            eng,
            "t/ilse",
            "s1",
            _msgs(
                "Noor: I live in Utrecht.",
                "Noor: I teach chemistry with Bram Vos.",
                "Noor: Last spring I moved to Delft.",
                "Noor: I paint murals with bram vos.",
            ),
        )
        stats = await eng.flush_windows()
        stage = eng.__dict__["_entity_resolve_stage"]
        ents = stage.registry.active(("t", "ilse"))
        names = Counter(e.name for e in ents)
        assert names["Bram Vos"] + names["bram vos"] == 1  # the object resolved to one entity
        assert sum(n for name, n in names.items() if name == "Noor") >= 2  # homonym kept apart
        events = await eng._require_started().read_events(after_seq=0, limit=1000)
        creates = [
            e
            for e in events
            if e.kind is EventKind.MARKER and e.payload.get("marker") == "entity.create"
        ]
        assert len(creates) == len(ents)
        assert stats
    finally:
        await eng.stop()


async def test_resolver_registry_markers_cite_the_source_record() -> None:
    env = _envs(SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt lives in Leiden."))[0]
    source_id = env.evidence[0].turn.source_id  # type: ignore[union-attr]

    class CaptureStorage:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def append_event(self, event: Any) -> None:
            self.events.append(event)

        async def append_registry_marker(self, event: Any) -> None:
            self.events.append(event)

        async def registry_erasure_fences(self, after_seq: int = 0) -> list[Any]:
            del after_seq
            return []

        async def read_events(self, *, after_seq: int, limit: int) -> list[Any]:
            del after_seq, limit
            return []

        async def get_records(self, record_ids: list[str]) -> dict[str, Any]:
            return {
                r.record_id: r for r in _ctx("t/ilse", [env]).records if r.record_id in record_ids
            }

    storage = CaptureStorage()
    stage = EntityResolveStage(storage=lambda: storage)  # type: ignore[arg-type]
    await stage.run([env], _ctx("t/ilse", [env]))
    markers = [event.payload for event in storage.events]
    assert markers and all(source_id in marker.get("source_ids", []) for marker in markers)


async def test_resolver_context_citation_is_conjunctive_for_erasure() -> None:
    original = _envs(SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt lives in Leiden."))[0]
    env = original.model_copy(
        update={
            "text": "Ilse Brandt carries a violet journal.",
            "evidence": [
                *original.evidence,
                Evidence(
                    role="context",
                    turn=TurnRef(source_id="t2", source_version=1, seg_index=0),
                    source_family="user",
                ),
            ],
        }
    )
    stage = EntityResolveStage()
    await stage.run([env], _ctx("t/ilse", [env]))
    marker = stage.registry._history[0]
    assert marker["source_ids"] == ["t1", "t2"]
    assert marker["source_versions"] == [1, 1]
    assert scrub_registry_marker(marker, {"t2"})
    assert not marker["name"] and not marker["context"]
    stage.registry.forget_sources(["t2"])
    assert not stage.registry.active(KEY_A)


def test_split_preserves_independently_supported_merge_alias_after_erasure() -> None:
    registry = EntityRegistry()
    registry.create(KEY_A, "keep", "Ilse Brandt", source_refs=(("source-a", 1),))
    registry.create(KEY_A, "drop", "Amber Novik", source_refs=(("source-b", 1),))
    merge_id = registry.merge(KEY_A, "keep", "drop")
    registry.add_alias(KEY_A, "keep", "Amber Novik", source_refs=(("source-c", 1),))
    assert registry.split(merge_id)
    assert "Amber Novik" in registry.get(KEY_A, "keep").aliases  # type: ignore[union-attr]
    registry.forget_sources(["source-b"])
    replayed = EntityRegistry.replay(registry._history)
    assert registry.snapshot() == replayed.snapshot()
    assert "Amber Novik" in registry.get(KEY_A, "keep").aliases  # type: ignore[union-attr]


async def test_durable_flush_discards_source_free_direct_registry_text() -> None:
    class CaptureStorage:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def append_event(self, event: Any) -> None:
            self.events.append(event)

        async def append_registry_marker(self, event: Any) -> None:
            self.events.append(event)

        async def registry_erasure_fences(self, after_seq: int = 0) -> list[Any]:
            del after_seq
            return []

    storage = CaptureStorage()
    stage = EntityResolveStage(storage=lambda: storage)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="raw source citations"):
        await stage.resolve_surface("Amber Novik", scope=SCOPE_A, key=KEY_A)
    stage.registry.create(
        KEY_A, "unattributed", "Amber Novik", context="violet journal", namespace="t/ilse"
    )
    await stage.flush("t/ilse")
    assert "Amber Novik" not in json.dumps([e.payload for e in storage.events])
    assert not stage.registry.active(KEY_A)


async def test_durable_flush_rejects_untagged_merge_reason() -> None:
    class CaptureStorage:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def append_event(self, event: Any) -> None:
            self.events.append(event)

        async def registry_erasure_fences(self, after_seq: int = 0) -> list[Any]:
            del after_seq
            return []

    storage = CaptureStorage()
    stage = EntityResolveStage(storage=lambda: storage)  # type: ignore[arg-type]
    stage.registry.create(KEY_A, "keep", "Ilse Brandt")
    stage.registry.create(KEY_A, "drop", "Amber Novik")
    stage.registry.merge(KEY_A, "keep", "drop", reason="private merge context")
    with pytest.raises(ValueError, match="event namespace"):
        await stage.flush("other/ns")
    assert not storage.events


def test_hard_erase_blanks_unattributed_legacy_registry_marker() -> None:
    create = {
        "marker": "entity.create",
        "scope": ["t", "ilse"],
        "entity": "opaque-id",
        "name": "Amber Novik",
        "context": "violet journal",
    }
    alias = {
        "marker": "entity.alias",
        "scope": ["t", "ilse"],
        "entity": "opaque-id",
        "alias": "Amber",
        "context": "violet journal",
    }
    for payload in (create, alias):
        assert scrub_registry_marker(payload, {"erased-turn"})
        assert not any(payload.get(field) for field in ("name", "alias", "surface", "context"))
    for malformed in (None, "erased-turn"):
        payload = {"marker": "entity.create", "name": "Violet Novik", "source_ids": malformed}
        assert scrub_registry_marker(payload, {"erased-turn"})
        assert not payload["name"]


async def test_stale_queued_source_keeps_earlier_committed_registry_marker(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "stale_registry.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        record = MemoryRecord(
            record_id="t1", namespace="t/ilse", memory_type="episodic", content="source"
        )
        await storage.upsert_record(record)
        stage = EntityResolveStage(storage=lambda: storage)
        stage.registry.create(
            KEY_A, "opaque", "Ilse Brandt", source_refs=(("t1", 1),), namespace="t/ilse"
        )
        await stage.flush("t/ilse")
        await storage.upsert_record(record.model_copy(update={"version": 2}))
        stage.registry.add_alias(
            KEY_A, "opaque", "Amber Novik", source_refs=(("t1", 1),), namespace="t/ilse"
        )
        await stage.flush("t/ilse")
        events = await storage.read_events(after_seq=0, limit=1000)
        markers = [
            e.payload
            for e in events
            if e.payload.get("marker") in {"entity.create", "entity.alias"}
        ]
        assert markers[0]["name"] == "Ilse Brandt"
        assert not markers[1]["alias"] and not markers[1]["surface"]
        assert stage.registry.snapshot() == EntityRegistry.replay(markers).snapshot()
    finally:
        await client.close()


async def test_registry_marker_rejects_second_connection_erase_between_check_and_append(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "registry_native_race.db")
    first = SQLiteClient(SQLiteSpec(path))
    second = SQLiteClient(SQLiteSpec(path))
    await first.open()
    await second.open()
    writer = SQLiteStorage(first)
    eraser = SQLiteStorage(second)
    await writer.start()
    await eraser.start()

    class RaceProxy:
        async def registry_erasure_fences(self, after_seq: int = 0) -> list[Any]:
            return await writer.registry_erasure_fences(after_seq)

        async def get_records(self, record_ids: list[str]) -> dict[str, MemoryRecord]:
            return await writer.get_records(record_ids)

        async def append_registry_marker(self, event: MemoryEvent) -> MemoryEvent:
            await eraser.begin_registry_erasure(event.namespace, ["t1"])
            await eraser.scrub_marker_payloads(
                "entity.create",
                lambda p: scrub_registry_marker(p, {"t1"}),
                namespace=event.namespace,
            )
            return await writer.append_registry_marker(event)

        async def append_event(self, event: MemoryEvent) -> MemoryEvent:
            await eraser.scrub_marker_payloads(
                "entity.create",
                lambda p: scrub_registry_marker(p, {"t1"}),
                namespace=event.namespace,
            )
            return await writer.append_event(event)

    try:
        await writer.upsert_record(
            MemoryRecord(
                record_id="t1", namespace="t/ilse", memory_type="episodic", content="source"
            )
        )
        stage = EntityResolveStage(storage=lambda: RaceProxy())  # type: ignore[arg-type]
        stage.registry.create(
            KEY_A, "opaque", "Ilse Brandt", source_refs=(("t1", 1),), namespace="t/ilse"
        )
        with pytest.raises(StorageError, match="being erased"):
            await stage.flush("t/ilse")
        events = await writer.read_events(after_seq=0, limit=1000)
        assert not any(e.payload.get("name") == "Ilse Brandt" for e in events)
        assert not stage.registry.active(KEY_A)
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize(
    ("change", "version"),
    [
        ("missing", 1),
        ("version", 2),
        ("deleted", 1),
        ("quarantined", 1),
        ("foreign", 1),
        ("erased", 1),
        ("derived", 1),
    ],
)
async def test_registry_native_append_rejects_any_invalid_pinned_source(
    tmp_path: Path, change: str, version: int
) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "native_sources.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        await storage.upsert_record(
            MemoryRecord(
                record_id="t1", namespace="t/ilse", memory_type="episodic", content="source"
            )
        )
        if change != "missing":
            await storage.upsert_record(
                MemoryRecord(
                    record_id="t2",
                    namespace="foreign/ns" if change == "foreign" else "t/ilse",
                    memory_type="semantic" if change == "derived" else "episodic",
                    content="source",
                    version=version,
                    status=RecordStatus.DELETED if change == "deleted" else RecordStatus.ARCHIVED,
                    quarantined=change == "quarantined",
                )
            )
        if change == "erased":
            await storage.begin_registry_erasure("t/ilse", ["t2"])
        event = MemoryEvent(
            kind=EventKind.MARKER,
            namespace="t/ilse",
            actor="resolver",
            payload={
                "marker": "entity.create",
                "scope": ["t", "ilse"],
                "entity": "opaque",
                "name": "Ilse Brandt",
                "context": "private context",
                "source_ids": ["t1", "t2"],
                "source_versions": [1, 1],
            },
        )
        with pytest.raises(StorageError):
            await storage.append_registry_marker(event)
        events = await storage.read_events(after_seq=0, limit=1000)
        assert not any(e.payload.get("name") == "Ilse Brandt" for e in events)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "change",
    ["missing", "foreign", "stale", "fenced", "deleted", "quarantined", "derived", "hidden"],
)
async def test_direct_resolver_rejects_invalid_source_before_warm_mutation(
    tmp_path: Path, change: str
) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "direct_preflight.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        await storage.upsert_record(
            MemoryRecord(record_id="good", namespace="t/ilse", memory_type="episodic", content="ok")
        )
        if change != "missing":
            await storage.upsert_record(
                MemoryRecord(
                    record_id="bad",
                    namespace="foreign/ns" if change == "foreign" else "t/ilse",
                    memory_type="semantic" if change == "derived" else "episodic",
                    content="source",
                    version=2 if change == "stale" else 1,
                    quarantined=change == "quarantined",
                )
            )
        if change == "deleted":
            await storage.delete_record("bad")
        if change == "fenced":
            await storage.begin_registry_erasure("t/ilse", ["bad"])
        stage = EntityResolveStage(
            storage=lambda: storage,
            source_visible=(lambda rid: rid != "bad") if change == "hidden" else None,
        )
        with pytest.raises(ValueError, match="source unavailable or erased"):
            await stage.resolve_surface(
                "Private Name",
                scope=SCOPE_A,
                key=KEY_A,
                text="Private context",
                source_refs=(("good", 1), ("bad", 1)),
                namespace="t/ilse",
            )
        assert not stage.registry.active(KEY_A)
        assert not stage.registry.queued()
        assert "Private Name" not in repr(stage.registry.snapshot())
    finally:
        await client.close()


async def test_direct_resolver_accepts_visible_pinned_sources(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "direct_valid.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        for rid in ("good", "context"):
            await storage.upsert_record(
                MemoryRecord(
                    record_id=rid, namespace="t/ilse", memory_type="episodic", content="ok"
                )
            )
        stage = EntityResolveStage(storage=lambda: storage)
        result = await stage.resolve_surface(
            "Ilse Brandt",
            scope=SCOPE_A,
            key=KEY_A,
            source_refs=(("good", 1), ("context", 1)),
            namespace="t/ilse",
        )
        assert stage.registry.get(KEY_A, result["entity"]).name == "Ilse Brandt"  # type: ignore[union-attr]
        assert await stage.flush("t/ilse") == 1
        events = await storage.read_events(after_seq=0, limit=1000)
        assert events[0].payload["source_ids"] == ["good", "context"]
    finally:
        await client.close()


async def test_envelope_resolver_skips_fenced_source_before_warm_mutation(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "envelope_fenced.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        env = _envs(SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt visited Leiden."))[0]
        await storage.upsert_record(
            MemoryRecord(
                record_id="t1", namespace="t/ilse", memory_type="episodic", content="source"
            )
        )
        await storage.begin_registry_erasure("t/ilse", ["t1"])
        stage = EntityResolveStage(storage=lambda: storage)
        out = await stage.run([env], _ctx("t/ilse", [env]))
        assert out == [env]
        assert not stage.registry.active(KEY_A)
        assert not stage.registry.queued()
        assert "Ilse Brandt" not in repr(stage.registry.snapshot())
    finally:
        await client.close()


async def test_registry_accepts_pinned_resource_turn_source(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "registry_resource.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        env = _envs(SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt appears in this resource."))[0]
        source_id = env.evidence[0].turn.source_id  # type: ignore[union-attr]
        await storage.upsert_record(
            MemoryRecord(
                record_id=source_id, namespace="t/ilse", memory_type="resource", content="source"
            )
        )
        ctx = _ctx("t/ilse", [env])
        ctx.records[0].memory_type = "resource"
        stage = EntityResolveStage(storage=lambda: storage)
        await stage.run([env], ctx)
        events = await storage.read_events(after_seq=0, limit=1000)
        assert any(
            e.kind is EventKind.MARKER and e.payload.get("name") == "Ilse Brandt" for e in events
        )
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("source_ids", "source_versions"),
    [(["s", "s"], [2, 1]), (["s"], [True])],
)
async def test_registry_native_append_rejects_malformed_pins(
    tmp_path: Path, source_ids: list[str], source_versions: list[int]
) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "native_malformed.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        await storage.upsert_record(
            MemoryRecord(
                record_id="s", namespace="t/ilse", memory_type="episodic", content="source"
            )
        )
        with pytest.raises(StorageError, match="pinned raw source"):
            await storage.append_registry_marker(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace="t/ilse",
                    actor="resolver",
                    payload={
                        "marker": "entity.create",
                        "scope": ["t", "ilse"],
                        "entity": "opaque",
                        "name": "Ilse Brandt",
                        "source_ids": source_ids,
                        "source_versions": source_versions,
                    },
                )
            )
        assert not await storage.read_events(after_seq=0, limit=1000)
    finally:
        await client.close()


async def test_generic_event_doors_reject_unfenced_registry_text_and_fence(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "generic_registry.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        unsafe = MemoryEvent(
            kind=EventKind.MARKER,
            namespace="t/ilse",
            actor="resolver",
            payload={"marker": "entity.create", "entity": "opaque", "name": "Private Name"},
        )
        fence = MemoryEvent(
            kind=EventKind.REGISTRY_ERASURE_FENCE,
            namespace="t/ilse",
            actor="registry_erasure",
            payload={"source_ids": ["s"]},
        )
        unsafe_merge = MemoryEvent(
            kind=EventKind.MARKER,
            namespace="t/ilse",
            actor="resolver",
            payload={"marker": "entity.merge", "reason": "Private Name"},
        )
        sanitized = MemoryEvent(
            kind=EventKind.MARKER,
            namespace="t/ilse",
            actor="resolver",
            payload={"marker": "entity.create", "entity": "opaque", "name": ""},
        )
        for event in (unsafe, unsafe_merge, sanitized, fence):
            with pytest.raises(StorageError, match="guarded append"):
                await storage.append_event(event)
            with pytest.raises(StorageError, match="guarded append"):
                await storage.append_with_outbox(event, [])
        assert not await storage.read_events(after_seq=0, limit=1000)
        await storage.append_registry_marker(sanitized)
        await storage.append_registry_marker(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="t/ilse",
                actor="resolver",
                payload={
                    "marker": "entity.pending",
                    "scope": ["t", "ilse"],
                    "entity": "opaque",
                    "pending_with": [],
                },
            )
        )
        with pytest.raises(StorageError, match="unpinned text"):
            await storage.append_registry_marker(unsafe_merge)
        assert len(await storage.read_events(after_seq=0, limit=1000)) == 2
    finally:
        await client.close()


@pytest.mark.parametrize("marker", ["entity.create", "entity.alias"])
@pytest.mark.parametrize(
    "field,value", [("reason", "Erased Name"), ("added_aliases", ["Erased Name"])]
)
@pytest.mark.parametrize("pinned", [False, True])
async def test_registry_door_rejects_known_cross_marker_text(
    tmp_path: Path, marker: str, field: str, value: Any, pinned: bool
) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "cross_marker.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        if pinned:
            await storage.upsert_record(
                MemoryRecord(
                    record_id="s", namespace="t/ilse", memory_type="episodic", content="source"
                )
            )
        payload: dict[str, Any] = {
            "marker": marker,
            "scope": ["t", "ilse"],
            "entity": "opaque",
            "name" if marker == "entity.create" else "alias": "Pinned Name" if pinned else "",
            field: value,
        }
        if pinned:
            payload.update(source_ids=["s"], source_versions=[1])
        with pytest.raises(StorageError, match="unpinned text"):
            await storage.append_registry_marker(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace="t/ilse",
                    actor="resolver",
                    payload=payload,
                )
            )
        assert not await storage.read_events(after_seq=0, limit=1000)
    finally:
        await client.close()


@pytest.mark.parametrize("marker", ["entity.create", "entity.alias"])
def test_historical_registry_scrub_removes_known_cross_marker_text(marker: str) -> None:
    payload = {
        "marker": marker,
        "source_ids": ["s"],
        "source_versions": [1],
        "name": "Erased Name",
        "alias": "Erased Alias",
        "reason": "Erased Reason",
        "added_aliases": ["Erased Copy"],
    }
    assert scrub_registry_marker(payload, {"s"})
    assert not any(payload.get(field) for field in ("name", "alias", "reason", "added_aliases"))


async def test_ephemeral_registry_erasure_never_persists_a_fence(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "ephemeral_registry.db")))
    await client.open()
    storage = SQLiteStorage(client, mode=EventLogMode.EPHEMERAL)
    await storage.start()
    try:
        await storage.begin_registry_erasure("t/ilse", ["s"])
        with pytest.raises(StorageError, match="persisted event log"):
            await storage.append_registry_marker(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace="t/ilse",
                    actor="resolver",
                    payload={
                        "marker": "entity.create",
                        "entity": "opaque",
                        "name": "Private Name",
                        "source_ids": ["s"],
                        "source_versions": [1],
                    },
                )
            )
        assert not await SQLiteStorage(client).read_events(after_seq=0, limit=1000)
    finally:
        await client.close()


async def test_ephemeral_resolver_flush_keeps_live_registry_without_event(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "ephemeral_resolver.db")))
    await client.open()
    storage = SQLiteStorage(client, mode=EventLogMode.EPHEMERAL)
    await storage.start()
    try:
        await storage.upsert_record(
            MemoryRecord(
                record_id="s", namespace="t/ilse", memory_type="episodic", content="source"
            )
        )
        stage = EntityResolveStage(storage=lambda: storage)
        resolved = await stage.resolve_surface(
            "Ilse Brandt", scope=SCOPE_A, key=KEY_A, source_refs=(("s", 1),), namespace="t/ilse"
        )
        assert await stage.flush("t/ilse") == 1
        assert stage.registry.get(KEY_A, resolved["entity"]).name == "Ilse Brandt"  # type: ignore[union-attr]
        assert not await storage.read_events(after_seq=0, limit=1000)
    finally:
        await client.close()


async def test_registry_native_append_before_erase_is_scrubbed(tmp_path: Path) -> None:
    path = str(tmp_path / "registry_order.db")
    first = SQLiteClient(SQLiteSpec(path))
    second = SQLiteClient(SQLiteSpec(path))
    await first.open()
    await second.open()
    writer = SQLiteStorage(first)
    eraser = SQLiteStorage(second)
    await writer.start()
    await eraser.start()
    try:
        await writer.upsert_record(
            MemoryRecord(
                record_id="t1", namespace="t/ilse", memory_type="episodic", content="source"
            )
        )
        await writer.append_registry_marker(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="t/ilse",
                actor="resolver",
                payload={
                    "marker": "entity.create",
                    "scope": ["t", "ilse"],
                    "entity": "opaque",
                    "name": "Ilse Brandt",
                    "source_ids": ["t1"],
                    "source_versions": [1],
                },
            )
        )
        await eraser.begin_registry_erasure("t/ilse", ["t1"])
        await eraser.scrub_marker_payloads(
            "entity.create",
            lambda p: scrub_registry_marker(p, {"t1"}),
            namespace="t/ilse",
        )
        events = await writer.read_events(after_seq=0, limit=1000)
        assert not any(e.payload.get("name") == "Ilse Brandt" for e in events)
    finally:
        await first.close()
        await second.close()


async def test_registry_erasure_fence_survives_rolling_prune_and_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "registry_rolling.db")
    first = SQLiteClient(SQLiteSpec(path))
    await first.open()
    storage = SQLiteStorage(first, mode=EventLogMode.ROLLING)
    await storage.start()
    try:
        await storage.upsert_record(
            MemoryRecord(
                record_id="t1", namespace="t/ilse", memory_type="episodic", content="source"
            )
        )
        await storage.begin_registry_erasure("t/ilse", ["t1"])
        await storage.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="t/ilse",
                actor="registry_erasure",
                payload={"marker": "unrelated"},
            )
        )
        events = await storage.read_events(after_seq=0, limit=1000)
        assert len(events) == 2 and events[-1].seq is not None
        tail = events[-1].seq
        advanced = await storage.advance_records(tail, len(events), fold_records)
        assert (advanced.cursor, advanced.applied_count, advanced.complete) == (tail, 2, True)
        assert await storage.get_offset("records") == tail
        assert await storage.prune_events(datetime.max.replace(tzinfo=UTC)) == 1
        retained = await storage.read_events(after_seq=0, limit=1000)
        assert len(retained) == 1 and retained[0].kind is EventKind.REGISTRY_ERASURE_FENCE
    finally:
        await first.close()
    second = SQLiteClient(SQLiteSpec(path))
    await second.open()
    reopened = SQLiteStorage(second, mode=EventLogMode.ROLLING)
    await reopened.start()
    try:
        with pytest.raises(StorageError, match="being erased"):
            await reopened.append_registry_marker(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace="t/ilse",
                    actor="resolver",
                    payload={
                        "marker": "entity.create",
                        "scope": ["t", "ilse"],
                        "entity": "opaque",
                        "name": "Ilse Brandt",
                        "source_ids": ["t1"],
                        "source_versions": [1],
                    },
                )
            )
    finally:
        await second.close()


async def test_second_engine_fence_invalidates_loaded_registry_before_unrelated_flush(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "registry_warm_fence.db")
    first = SQLiteClient(SQLiteSpec(path))
    second = SQLiteClient(SQLiteSpec(path))
    await first.open()
    await second.open()
    writer = SQLiteStorage(first)
    eraser = SQLiteStorage(second)
    await writer.start()
    await eraser.start()
    try:
        for rid in ("s", "t"):
            await writer.upsert_record(
                MemoryRecord(
                    record_id=rid, namespace="t/ilse", memory_type="episodic", content="source"
                )
            )
        await writer.append_registry_marker(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="t/ilse",
                actor="resolver",
                payload={
                    "marker": "entity.create",
                    "scope": ["t", "ilse"],
                    "entity": "old",
                    "name": "Erased Name",
                    "source_ids": ["s"],
                    "source_versions": [1],
                },
            )
        )
        # A legacy marker is already in the log; the modern generic append door
        # rejects fresh name-bearing markers without native source checks.
        async with first.engine.begin() as conn:
            await conn.execute(
                writer._event_insert(
                    MemoryEvent(
                        kind=EventKind.MARKER,
                        namespace="foreign/ns",
                        actor="legacy",
                        payload={
                            "marker": "entity.create",
                            "scope": ["foreign", "ns"],
                            "entity": "foreign",
                            "name": "Foreign Name",
                            "source_ids": ["s"],
                            "source_versions": [1],
                        },
                    )
                )
            )
        stage = EntityResolveStage(storage=lambda: writer)
        await stage._load()
        assert stage.registry.get(KEY_A, "old").name == "Erased Name"  # type: ignore[union-attr]
        await eraser.begin_registry_erasure("t/ilse", ["s"])
        # The second engine has not yet reached its historical scrub. The first
        # engine must still evict its loaded label before using the registry again.
        await stage.resolve_surface(
            "Current Name",
            scope=SCOPE_A,
            key=KEY_A,
            text="Current Name is present.",
            source_refs=(("t", 1),),
            namespace="t/ilse",
        )
        await stage.flush("t/ilse")
        assert [e.name for e in stage.registry.active(KEY_A)] == ["Current Name"]
        assert stage.registry.get(("foreign", "ns"), "foreign").name == "Foreign Name"  # type: ignore[union-attr]
        await eraser.scrub_marker_payloads(
            "entity.create", lambda p: scrub_registry_marker(p, {"s"}), namespace="t/ilse"
        )
        events = await writer.read_events(after_seq=0, limit=1000)
        assert not any(e.payload.get("name") == "Erased Name" for e in events)
    finally:
        await first.close()
        await second.close()


async def test_direct_pending_namespace_survives_foreign_run_and_scoped_stale_flush(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "scoped_pending.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        source_a = MemoryRecord(
            record_id="source-a",
            namespace="ns/a",
            memory_type="episodic",
            content="Amber Novik speaks",
        )
        source_b = MemoryRecord(
            record_id="t1", namespace="ns/b", memory_type="episodic", content="Ilse Brandt speaks"
        )
        await storage.upsert_record(source_a)
        await storage.upsert_record(source_b)
        stage = EntityResolveStage(storage=lambda: storage)
        scope_a = OwnerScope(tenant="default", owner="ns/a")
        key_a = ("default", "ns/a")
        await stage.resolve_surface(
            "Amber Novik",
            scope=scope_a,
            key=key_a,
            text="Amber Novik speaks",
            source_refs=(("source-a", 1),),
            namespace="ns/a",
        )
        envs_b = _envs(
            OwnerScope(tenant="default", owner="ns/b"), ("Ilse Brandt", None, "Ilse Brandt speaks.")
        )
        await stage.run(envs_b, _ctx("ns/b", envs_b))
        events = await storage.read_events(after_seq=0, limit=1000)
        assert all(e.namespace == "ns/b" for e in events)
        assert any(p.get("name") == "Amber Novik" for p in stage.registry.queued("ns/a"))
        await stage.flush("ns/a")
        events = await storage.read_events(after_seq=0, limit=1000)
        assert any(e.namespace == "ns/a" and e.payload.get("name") == "Amber Novik" for e in events)
        stage.registry.add_alias(
            key_a,
            stage.registry.active(key_a)[0].entity_id,
            "Amber",
            source_refs=(("source-a", 1),),
            namespace="ns/a",
        )
        await storage.upsert_record(source_a.model_copy(update={"version": 2}))
        await stage.flush("ns/a")
        events = await storage.read_events(after_seq=0, limit=1000)
        assert any(e.namespace == "ns/b" and e.payload.get("name") == "Ilse Brandt" for e in events)
        assert (
            stage.registry.snapshot()
            == EntityRegistry.replay(
                e.payload for e in events if e.kind is EventKind.MARKER
            ).snapshot()
        )
    finally:
        await client.close()


async def test_registry_reload_failure_clears_warm_text_then_retries(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "reload_failure.db")))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()

    class FailingReader:
        fail = False

        def __getattr__(self, name: str) -> Any:
            return getattr(storage, name)

        async def read_events(self, *, after_seq: int, limit: int) -> list[Any]:
            if self.fail:
                raise RuntimeError("injected registry reload failure")
            return await storage.read_events(after_seq=after_seq, limit=limit)

    proxy = FailingReader()
    try:
        for rid in ("t1", "t2"):
            await storage.upsert_record(
                MemoryRecord(
                    record_id=rid, namespace="t/ilse", memory_type="episodic", content="source"
                )
            )
        stage = EntityResolveStage(storage=lambda: proxy)  # type: ignore[arg-type]
        stage.registry.create(
            KEY_A, "erased", "Amber Novik", source_refs=(("t1", 1),), namespace="t/ilse"
        )
        stage.registry.create(
            KEY_A, "survivor", "Ilse Brandt", source_refs=(("t2", 1),), namespace="t/ilse"
        )
        await stage.flush("t/ilse")
        await storage.scrub_marker_payloads(
            "entity.create", lambda p: scrub_registry_marker(p, {"t1"}), namespace="t/ilse"
        )
        proxy.fail = True
        with pytest.raises(RuntimeError, match="injected registry reload failure"):
            await stage.forget_sources(["t1"])
        assert not stage.registry.active(KEY_A)
        proxy.fail = False
        await stage._load()
        assert [e.name for e in stage.registry.active(KEY_A)] == ["Ilse Brandt"]
        events = await storage.read_events(after_seq=0, limit=1000)
        assert (
            stage.registry.snapshot()
            == EntityRegistry.replay(
                e.payload for e in events if e.kind is EventKind.MARKER
            ).snapshot()
        )
    finally:
        await client.close()


async def test_hard_forget_prunes_registry_markers_and_preserves_second_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "resolver_erase.db", resolver=True)
    try:
        await _send(
            eng,
            "t/ilse",
            "s1",
            _msgs("Ilse Brandt: I collect amber marbles.", "Ilse Brandt: I collect blue marbles."),
        )
        await eng.flush_windows()
        storage = eng._require_started()
        stage = eng.__dict__["_entity_resolve_stage"]
        events = await storage.read_events(after_seq=0, limit=1000)
        markers = [
            e.payload
            for e in events
            if e.payload.get("marker") in {"entity.create", "entity.alias"}
        ]
        sources = list(
            dict.fromkeys(rid for marker in markers for rid in marker.get("source_ids", ()))
        )
        assert len(sources) == 2
        erased = sources[0]
        foreign_marker = {
            "marker": "entity.create",
            "scope": ["default", "other/ns"],
            "entity": "foreign-opaque",
            "name": "Foreign Novik",
            "context": "foreign context",
        }
        local_legacy = {
            "marker": "entity.create",
            "scope": ["default", "t/ilse"],
            "entity": "local-legacy",
            "name": "Local Novik",
            "context": "local context",
        }
        for namespace, payload in (("other/ns", foreign_marker), ("t/ilse", local_legacy)):
            await storage._append_replayed_event(
                MemoryEvent(
                    kind=EventKind.MARKER, namespace=namespace, actor="resolver", payload=payload
                )
            )
            stage.registry.apply(payload, namespace=namespace)
        entity_id = next(m["entity"] for m in markers if erased in m.get("source_ids", ()))
        stage.registry.add_alias(
            KEY_A,
            entity_id,
            "Amber Novik",
            context="Amber Novik keeps a violet journal.",
            source_refs=((erased, 1),),
            namespace="t/ilse",
        )
        await stage.flush("t/ilse")
        await stage.resolve_surface(
            "Violet Novik",
            scope=SCOPE_A,
            key=KEY_A,
            text="Violet Novik keeps a secret journal.",
            source_refs=((erased, 1),),
            namespace="t/ilse",
        )
        await eng.forget(erased, namespace="t/ilse", hard=True, cascade=False)
        await stage.flush("t/ilse")
        events = await storage.read_events(after_seq=0, limit=1000)
        markers = [
            e.payload
            for e in events
            if e.payload.get("marker") in {"entity.create", "entity.alias"}
        ]
        assert all(
            not (m.get("name") or m.get("surface") or m.get("context"))
            for m in markers
            if erased in m.get("source_ids", ())
        )
        assert any(
            m.get("name") or m.get("surface")
            for m in markers
            if sources[1] in m.get("source_ids", ())
        )
        assert "Amber Novik" not in json.dumps(markers)
        assert "Violet Novik" not in json.dumps(markers)
        assert all(
            "Amber Novik" not in " ".join(e.names()) + e.context
            for e in stage.registry.active(KEY_A)
        )
        assert any("Ilse Brandt" in e.names() for e in stage.registry.active(KEY_A))
        assert "Foreign Novik" in [e.name for e in stage.registry.active(("default", "other/ns"))]
        assert "Local Novik" not in [e.name for e in stage.registry.active(("default", "t/ilse"))]
        foreign_events = [e.payload for e in events if e.namespace == "other/ns"]
        assert any(e.get("name") == "Foreign Novik" for e in foreign_events)
        replayed = EntityRegistry.replay(markers)
        assert stage.registry.snapshot() == replayed.snapshot()
    finally:
        await eng.stop()


async def test_end_to_end_flag_off_writes_no_entities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "off.db")
    try:
        await _send(
            eng, "t/ilse", "s1", _msgs("Noor: I live in Utrecht.", "Noor: I teach chemistry.")
        )
        await eng.flush_windows()
        assert "_entity_resolve_stage" not in eng.__dict__
        events = await eng._require_started().read_events(after_seq=0, limit=1000)
        assert not [e for e in events if str(e.payload.get("marker", "")).startswith("entity.")]
    finally:
        await eng.stop()
