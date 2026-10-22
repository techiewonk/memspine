"""W2-09: online entity resolution with pending_merge (``write.resolver_v2``; W-19, M-15).

Model-free: invented names (Noor, Ilse, Bram, Tomasz), the hash embedder or a scripted vector table,
a stub window LLM for the one end-to-end window."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
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
from memspine.ingest.windows import _envelope_stages
from memspine.observability import tap
from memspine.schemas.durable import OwnerScope
from memspine.schemas.reserved import reserved_hits
from memspine.services.embedding.hash import HashEmbedding
from memspine.services.llm.base import LLMRouter
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind
from memspine.store.migrate import ReplayBuilder

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


def _ctx(ns: str = "t/ilse") -> EnvelopeStageContext:
    return EnvelopeStageContext(
        namespace=ns, window_id="w1", lines={}, records=[], compile_ctx=None
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
    return await stage.run(_envs(scope, *items), _ctx(f"{scope.tenant}/{scope.owner}"))


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
    assert cfg.write.resolver_v2 is False
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
        write={"window_extract": "facts", "envelope_v2": True, "resolver_v2": True},
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


async def test_unscoped_keys_use_the_namespace() -> None:
    st = _stage(scoped=False)
    await st.run(
        _envs(SCOPE_A, ("Ilse Brandt", None, "Ilse Brandt lives in Leiden.")), _ctx("ns-one")
    )
    assert len(st.registry.active(("ns-one",))) == 1 and not st.registry.active(KEY_A)


# --- replay ----------------------------------------------------------------------------------------
async def test_registry_replays_from_payloads_and_through_migrate(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "src.db")))
    await client.open()
    src = SQLiteStorage(client)
    await src.start()
    tclient = SQLiteClient(SQLiteSpec(str(tmp_path / "dst.db")))
    await tclient.open()
    dst = SQLiteStorage(tclient)
    await dst.start()
    try:
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
        mid = st.registry.merge(KEY_A, keep, drop)
        await st.flush("t/ilse")
        st.registry.split(mid)
        st.registry.merge(KEY_A, keep, drop)
        await st.flush("t/ilse")
        events = await src.read_events(after_seq=0, limit=1000)
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


# --- end to end: one window, write.envelope_v2 + write.resolver_v2 -----------------------------------
LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class WhoLLM:
    """Answers the structured extraction: one item per line, subject = the speaker, object = a named
    person when the line says ``with <Name>``."""

    provider_id = "fake:who"

    def __init__(self) -> None:
        self.legacy = FakeWindowLLM()

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        from memspine.prompts.structured import STRUCTURED_SYSTEM

        if messages[0]["content"] != STRUCTURED_SYSTEM:
            return await self.legacy.chat(messages, **options)
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
        return json.dumps(items)


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
        storage={"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            "envelope_v2": True,
            **write,
        },
        llm={
            "roles": {
                "window_extract": {
                    "provider": "litellm",
                    "settings": {"model": "fake/window", "timeout_seconds": 60.0},
                }
            }
        },
    )
    await eng.start()
    return eng


def _msgs(*lines: str) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": text, "timestamp": f"2023-06-01T10:0{i}:00+00:00"}
        for i, text in enumerate(lines)
    ]


async def test_end_to_end_window_resolves_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, tmp_path / "on.db", resolver_v2=True)
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
        ents = stage.registry.active(("t", "ilse")) or stage.registry.active(("t/ilse",))
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
