"""R3-04: core retrieval legs behind one registry (read.legs_v2). Model-free: hash embedder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.read import legs as lib
from memspine.read.legs import core as legs_core
from memspine.read.legs.lexical import UnicodeTokeniser, make_scorer
from memspine.read.registry import leg_family
from memspine.store.records import MemoryRecord

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


def _rec(rid: str, content: str, at: datetime = T0, **kw: Any) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid, namespace="a", memory_type="episodic", content=content, valid_from=at, **kw
    )


def _ctx(records: list[MemoryRecord], query: str, **kw: Any) -> lib.LegContext:
    return lib.LegContext(query=query, namespace="a", fetch_k=10, records=records, **kw)


# ------------------------------------------------------------------ registry / config


def test_flag_defaults_off_and_bm25_is_the_scorer() -> None:
    cfg = ReadConfig()
    assert cfg.legs_v2 is False
    assert cfg.lexical_scorer == "bm25"
    assert cfg.lexical_language == "en"
    assert cfg.lexical_namespace_languages == {}


def test_registry_has_the_three_core_families() -> None:
    assert lib.REGISTRY.names() == ("vector", "lexical", "temporal")
    assert [lib.REGISTRY.family_of(n) for n in lib.REGISTRY.names()] == [
        "dense",
        "lexical",
        "structured",
    ]
    # fusion votes them in the registry's source families
    assert leg_family("vector") == "semantic"
    assert leg_family("lexical") == "lexical"
    assert leg_family("temporal") == "temporal"


def test_registering_a_leg_names_its_fusion_family() -> None:
    reg = lib.LegRegistry()
    reg.register(lib.LexicalLeg(name="lexical_facts_r3_04", layers=("L1",)))
    assert reg.family_of("lexical_facts_r3_04") == "lexical"
    assert leg_family("lexical_facts_r3_04") == "lexical"
    with pytest.raises(ValueError):
        reg.register(lib.LexicalLeg(name="lexical_facts_r3_04"))


# ------------------------------------------------------------------ each leg returns its ids


async def test_lexical_leg_ranks_by_bm25() -> None:
    recs = [
        _rec("r1", "Alice adopted a grey cat called Pepper"),
        _rec("r2", "Bob repaired the garden fence"),
        _rec("r3", "The cat sat on the mat while the cat slept"),
    ]
    out = await lib.REGISTRY.get("lexical").run(_ctx(recs, "which cat did Alice adopt"))
    ids = [h.record_id for h in out.hits]
    assert ids[0] == "r1" and "r2" not in ids and set(ids) == {"r1", "r3"}
    assert all(h.score > 0 for h in out.hits)


async def test_vector_leg_wraps_the_search_and_reports_no_embedder() -> None:
    async def search(k: int) -> list[lib.Hit]:
        return [lib.Hit("v1", 0.9), lib.Hit("v2", 0.5)][:k]

    ok = await lib.REGISTRY.get("vector").run(_ctx([], "q", vector_search=search))
    assert [h.record_id for h in ok.hits] == ["v1", "v2"]
    none = await lib.REGISTRY.get("vector").run(_ctx([], "q"))
    assert none.hits == [] and none.empty_reason == "no_embedder"


async def test_temporal_leg_filters_by_the_window() -> None:
    recs = [
        _rec("in", "went camping", datetime(2023, 5, 7, 18, 0, tzinfo=UTC)),
        _rec("also", "bought a tent", datetime(2023, 5, 7, 9, 0, tzinfo=UTC)),
        _rec("before", "planned the trip", datetime(2023, 5, 6, 23, 59, tzinfo=UTC)),
        _rec("after", "came back", datetime(2023, 5, 8, 0, 0, tzinfo=UTC)),
    ]
    out = await lib.REGISTRY.get("temporal").run(_ctx(recs, "what happened on 7 May 2023?"))
    assert {h.record_id for h in out.hits} == {"in", "also"}
    none = await lib.REGISTRY.get("temporal").run(_ctx(recs, "what happened ever?"))
    assert none.hits == [] and none.empty_reason == "no_window"


async def test_temporal_leg_matches_event_intervals_by_overlap() -> None:
    spans = _rec(
        "span", "lived in Paris", T0 - timedelta(days=200), valid_to=T0 + timedelta(days=5)
    )
    ended = _rec(
        "ended", "lived in Rome", T0 - timedelta(days=400), valid_to=T0 - timedelta(days=100)
    )
    happened = _rec(
        "happened", "ran a marathon", T0 + timedelta(days=90), tags=["happened:2023-05-09"]
    )
    out = await lib.REGISTRY.get("temporal").run(
        _ctx([spans, ended, happened], "what was true in May 2023?")
    )
    assert {h.record_id for h in out.hits} == {"span", "happened"}


async def test_temporal_window_anchors_relative_phrases_to_the_reference_time() -> None:
    recs = [
        _rec("last_week", "went hiking", datetime(2023, 5, 3, tzinfo=UTC)),
        _rec("old", "went swimming", datetime(2023, 1, 3, tzinfo=UTC)),
    ]
    ref = datetime(2023, 5, 10, tzinfo=UTC)
    out = await lib.REGISTRY.get("temporal").run(
        _ctx(recs, "what did I do last week", reference_time=ref)
    )
    assert [h.record_id for h in out.hits] == ["last_week"]
    unanchored = await lib.REGISTRY.get("temporal").run(_ctx(recs, "what did I do last week"))
    assert unanchored.empty_reason == "no_window"


# ------------------------------------------------------------------ tokeniser: stemming, CJK


def test_german_stemming_and_stopwords() -> None:
    tok = UnicodeTokeniser("de")
    assert tok.tokens("Die Häuser") == tok.tokens("Haus") == ["haus"]
    assert tok.tokens("Kindern") == tok.tokens("Kinder") == ["kind"]


def test_french_stemming_and_stopwords() -> None:
    tok = UnicodeTokeniser("fr")
    assert tok.tokens("les chevaux") == ["cheval"]
    assert tok.tokens("nationales") == tok.tokens("national")


def test_english_unchanged_by_language_and_unknown_language_does_not_stem() -> None:
    assert UnicodeTokeniser("en").tokens("running runs") == ["run", "run"]
    assert UnicodeTokeniser("xx").tokens("Running Runs") == ["running", "runs"]


def test_cjk_falls_back_to_character_bigrams() -> None:
    tok = UnicodeTokeniser("en")
    assert tok.tokens("我喜欢东京") == ["我喜", "喜欢", "欢东", "东京"]
    assert tok.tokens("猫") == ["猫"]
    assert tok.tokens("Tokyo 東京タワー") == ["tokyo", "東京", "京タ", "タワ", "ワー"]


def test_literal_codes_are_kept_whole_and_not_stemmed() -> None:
    toks = UnicodeTokeniser("en").tokens("ticket AB-123 for v2.4.1 running")
    assert "ab-123" in toks and "v2.4.1" in toks and "ticket" in toks and "run" in toks


async def test_lexical_leg_finds_german_inflections_and_cjk() -> None:
    recs = [
        _rec("de1", "Wir haben zwei Häuser am See gekauft"),
        _rec("de2", "Das Wetter war gestern schlecht"),
        _rec("zh1", "我昨天去了东京旅行"),
        _rec("zh2", "他在北京工作"),
    ]
    leg = lib.REGISTRY.get("lexical")
    de = await leg.run(_ctx(recs, "Haus kaufen", language="de"))
    assert [h.record_id for h in de.hits][:1] == ["de1"]
    zh = await leg.run(_ctx(recs, "东京", language="en"))
    assert [h.record_id for h in zh.hits] == ["zh1"]
    # per-namespace override beats the default language
    over = await leg.run(_ctx(recs, "Haus kaufen", namespace_languages={"a": "de"}))
    assert [h.record_id for h in over.hits][:1] == ["de1"]


# ------------------------------------------------------------------ scorer port


def test_unknown_scorer_is_a_clear_error_and_a_scorer_can_be_swapped_in() -> None:
    with pytest.raises(ValueError, match="unknown lexical scorer"):
        make_scorer("word2vec")

    class Overlap:
        name = "overlap_r3_04"

        def fit(self, docs: Any) -> None:
            self.docs = {k: set(v) for k, v in docs.items()}

        def search(self, terms: Any, k: int) -> list[tuple[str, float]]:
            q = set(terms)
            scored = [(rid, float(len(q & d))) for rid, d in self.docs.items() if q & d]
            return sorted(scored, key=lambda kv: (-kv[1], kv[0]))[:k]

    lib.register_scorer("overlap_r3_04", Overlap)
    try:
        import asyncio

        recs = [_rec("a", "red apple"), _rec("b", "red red car")]
        out = asyncio.run(
            lib.LexicalLeg().run(_ctx(recs, "red apple", lexical_scorer="overlap_r3_04"))
        )
        assert [h.record_id for h in out.hits] == ["a", "b"]
    finally:
        from memspine.read.legs.lexical import SCORERS

        SCORERS.pop("overlap_r3_04", None)


# ------------------------------------------------------------------ empty legs, not errors


async def test_l1_legs_return_empty_on_the_no_model_template() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": True, "record_access": False},
    )
    await eng.start()
    try:
        await eng.write("Alice adopted a cat", namespace="a", memory_type="episodic", valid_from=T0)
        records = await eng._records("a")
    finally:
        await eng.stop()
    assert records and all(r.layer == "L0" for r in records)  # no model, so no derived facts
    reg = lib.LegRegistry()
    reg.register(lib.LexicalLeg(name="lexical_l1", layers=("L1",)))
    reg.register(lib.TemporalLeg(name="temporal_l1", layers=("L1",)))
    out = await lib.run_legs(
        reg, ["lexical_l1", "temporal_l1"], _ctx(records, "cat on 7 May 2023", reference_time=T0)
    )
    for name in ("lexical_l1", "temporal_l1"):
        assert out[name].hits == []
        assert out[name].empty_reason == "no_records:L1"
    # and the L0 legs still answer on the same records
    l0 = await lib.run_legs(
        lib.REGISTRY, ["lexical", "temporal"], _ctx(records, "Alice cat on 7 May 2023")
    )
    assert l0["lexical"].hits and l0["temporal"].hits


async def test_a_failing_optional_leg_degrades_to_empty_but_a_required_one_raises() -> None:
    class Boom:
        name = "boom"
        family = "structured"
        layers = ("L0",)

        async def run(self, ctx: lib.LegContext) -> lib.LegOutput:
            raise RuntimeError("x")

    reg = lib.LegRegistry()
    reg.register(Boom())
    out = await lib.run_legs(reg, ["boom"], _ctx([], "q"))
    assert out["boom"].hits == [] and out["boom"].empty_reason == "error:RuntimeError"
    reg.register(Boom(), required=True, replace=True)
    with pytest.raises(RuntimeError):
        await lib.run_legs(reg, ["boom"], _ctx([], "q"))


async def test_read_leg_tap_event_carries_family_count_latency_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(legs_core.tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    recs = [_rec("r1", "Alice adopted a cat")]
    await lib.run_legs(lib.REGISTRY, ["lexical", "temporal"], _ctx(recs, "Alice cat"))
    by_leg = {f["leg"]: f for stage, f in seen if stage == "read.leg"}
    assert by_leg["lexical"]["family"] == "lexical" and by_leg["lexical"]["count"] == 1
    assert by_leg["lexical"]["empty_reason"] is None
    assert by_leg["temporal"]["family"] == "structured" and by_leg["temporal"]["count"] == 0
    assert by_leg["temporal"]["empty_reason"] == "no_window"
    assert isinstance(by_leg["temporal"]["latency_ms"], float)


# ------------------------------------------------------------------ the wiring (engine)


async def _engine(**read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": True, "record_access": False, **read},
    )
    await eng.start()
    await eng.write(
        "Alice adopted a grey cat called Pepper",
        namespace="a",
        memory_type="episodic",
        valid_from=datetime(2023, 5, 7, 12, 0, tzinfo=UTC),
    )
    await eng.write(
        "Bob repaired the garden fence",
        namespace="a",
        memory_type="episodic",
        valid_from=datetime(2023, 1, 2, 12, 0, tzinfo=UTC),
    )
    await eng.write(
        "Carol bought a red bicycle",
        namespace="a",
        memory_type="episodic",
        valid_from=datetime(2023, 3, 9, 12, 0, tzinfo=UTC),
    )
    return eng


async def _ids(eng: Engine, q: str) -> list[str]:
    return [r.content for r, _ in await eng.search(q, namespace="a", top_k=3)]


async def test_flag_off_is_unchanged_and_never_touches_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    real = legs_core.run_legs
    monkeypatch.setattr(lib, "run_legs", lambda *a, **k: called.append("x") or real(*a, **k))
    default = await _engine()
    explicit = await _engine(legs_v2=False)
    try:
        q = "which cat did Alice adopt"
        assert await _ids(default, q) == await _ids(explicit, q)
    finally:
        await default.stop()
        await explicit.stop()
    assert called == []


async def test_flag_on_routes_candidates_through_the_registry_and_finds_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        legs_core.tap,
        "event",
        lambda stage, /, **f: seen.append(f["leg"]) if stage == "read.leg" else None,
    )
    eng = await _engine(legs_v2=True, temporal_leg=True)
    try:
        hits = await _ids(eng, "which cat did Alice adopt")
        dated = await _ids(eng, "what happened on 7 May 2023")
    finally:
        await eng.stop()
    assert hits[0].startswith("Alice adopted")
    assert dated[0].startswith("Alice adopted")  # the temporal leg lifts the record in the window
    assert {"vector", "lexical", "temporal"} <= set(seen)


async def test_flag_on_with_hybrid_off_runs_vector_and_temporal_only() -> None:
    eng = await _engine(legs_v2=True, hybrid=False, temporal_leg=True)
    try:
        hits = await _ids(eng, "what happened on 7 May 2023")
    finally:
        await eng.stop()
    assert hits and hits[0].startswith("Alice adopted")
