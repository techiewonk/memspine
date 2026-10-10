"""I17: knowledge updates. Read-side ``read.latest_wins`` on raw episodic turns, and the
supersede / coexist / contest operators on the keyed fact path (``extract_graph.cardinality``,
the conflict ladder). Synthetic conversations only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.latest_wins import apply_latest_wins, mark_latest
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import extract_graph

T0 = datetime(2023, 1, 1, tzinfo=UTC)


def _turn(text: str, day: int, role: str = "user", **kw: Any) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=text,
        valid_from=T0 + timedelta(days=day),
        source=SourceInfo(role=role),
        **kw,
    )


# ---- pure marking ---------------------------------------------------------------


def test_default_is_off() -> None:
    assert ReadConfig().latest_wins == "off"
    records = [_turn("Ana: I live in Chicago", 0), _turn("Ana: I moved, I live in Boston", 40)]
    out, marked = apply_latest_wins(records, "off")
    assert out == records and marked == set()


def test_update_marks_newer_latest_and_keeps_older() -> None:
    old = _turn("Ana: I live in Chicago these days", 0)
    new = _turn("Ana: I moved, now I live in Boston these days", 40)
    marks = mark_latest([old, new])
    assert marks[old.record_id].kind == "earlier"
    assert marks[old.record_id].newest == new.valid_from
    assert marks[new.record_id].kind == "latest"
    out, marked = apply_latest_wins([old, new], "annotate")
    assert marked == {old.record_id, new.record_id}
    assert "Chicago" in out[0].content and out[0].content.startswith("[earlier statement")
    assert "2023-02-10" in out[0].content
    assert out[1].content.startswith("[latest]") and "Boston" in out[1].content
    assert old.content == "Ana: I live in Chicago these days"  # the stored record is untouched


def test_coexist_distinct_facts_are_not_marked() -> None:
    dog = _turn("Ana: my dog is called Rex and he is brown", 0)
    cat = _turn("Ana: I adopted a cat named Tom last week", 30)
    assert mark_latest([dog, cat]) == {}


def test_control_unrelated_turns_and_other_speaker() -> None:
    a = _turn("Ana: I live in Chicago these days", 0)
    b = _turn("Ben: I live in Boston these days", 40)  # another speaker: not an update of Ana
    c = _turn("Ana: the weather turned cold and rainy", 50)
    assert mark_latest([a, b, c]) == {}


def test_equal_event_time_is_unordered() -> None:
    a = _turn("Ana: I live in Chicago these days", 0)
    b = _turn("Ana: I live in Boston these days", 0)
    assert mark_latest([a, b]) == {}


def test_keyed_records_match_on_key_only() -> None:
    old = _turn("Pat works at Acme", 0, entity="Pat", attribute="employer")
    new = _turn("Pat joined Globex", 90, entity="Pat", attribute="employer")
    other = _turn("Pat works at weekends", 95, entity="Pat", attribute="schedule")
    marks = mark_latest([old, new, other])
    assert marks[old.record_id].kind == "earlier" and marks[new.record_id].kind == "latest"
    assert other.record_id not in marks


def test_disputed_records_get_no_ordering_claim() -> None:
    a = _turn("Pat's sister is ten", 0, entity="Pat", attribute="sister_age", tags=["disputed"])
    b = _turn("Pat's sister is twelve", 5, entity="Pat", attribute="sister_age", tags=["disputed"])
    out, _ = apply_latest_wins([a, b], "annotate")
    assert all(r.content.startswith("[disputed") for r in out)


def test_recent_first_gathers_restated_records_newest_first() -> None:
    old = _turn("Ana: I live in Chicago these days", 0)
    mid = _turn("Ana: I went hiking in the hills", 10)
    new = _turn("Ana: I moved, now I live in Boston these days", 40)
    from memspine.core.latest_wins import recent_first

    out, marked = apply_latest_wins([old, mid, new], "annotate_recent_first")
    ordered = recent_first(out, marked)
    assert [r.record_id for r in ordered] == [new.record_id, old.record_id, mid.record_id]


# ---- read path, end to end ------------------------------------------------------

ROWS = [
    ("Ana: I live in Chicago these days", 0),
    ("Ana: the garden needs more water in summer", 10),
    ("Ana: I moved, now I live in Boston these days", 40),
]


async def _read(mode: str) -> list[str]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, "latest_wins": mode},
    )
    await eng.start()
    try:
        for text, day in ROWS:
            await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                valid_from=T0 + timedelta(days=day),
            )
        out = await eng.read("Where does Ana live now?", namespace="a", mode="retrieve", top_k=5)
        return [r.content for r in out.context.records]
    finally:
        await eng.stop()


async def test_read_default_unchanged_and_opt_in_marks() -> None:
    off = await _read("off")
    assert not any("[latest]" in c or "[earlier" in c for c in off)
    on = await _read("annotate")
    assert any("[latest]" in c and "Boston" in c for c in on)
    assert any("[earlier" in c and "Chicago" in c for c in on)  # the old value is kept
    assert len(on) == len(off)
    first = await _read("annotate_recent_first")
    restated = [c for c in first if "[latest]" in c or "[earlier" in c]
    assert "[latest]" in restated[0]


# ---- keyed write path: supersede / coexist / contest ---------------------------


def _engine(**policies: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True, "policies": policies},
            "episodic": {"enabled": True},
        },
    )


async def _facts(eng: Engine) -> list[MemoryRecord]:
    storage = eng._require_started()
    return [
        r
        for r in await storage.list_records("a", "semantic")
        if r.source.channel == "extract_graph" and "retract" not in r.tags
    ]


async def _graph(eng: Engine, turns: dict[str, ExtractedEdge]) -> None:
    async def fake(content: str, _context: object = None) -> list[ExtractedEdge]:
        return [turns[content]] if content in turns else []

    eng._extract_edges = fake
    for day, text in enumerate(turns):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(days=day * 30)
        )
        await extract_graph(eng._pipeline_ctx())


def _edge(rel: str, dst: str, kind: str = "state") -> ExtractedEdge:
    return ExtractedEdge(
        src_entity="Ana", rel=rel, dst_entity=dst, fact=f"Ana {rel} {dst}", kind=kind
    )  # type: ignore[arg-type]


async def test_state_edge_supersedes_and_keeps_history() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _graph(
            eng,
            {
                "Ana: I live in Chicago": _edge("lives_in", "Chicago"),
                "Ana: I moved to Boston": _edge("lives_in", "Boston"),
            },
        )
        facts = {f.tags[2]: f for f in await _facts(eng)}
    finally:
        await eng.stop()
    assert facts["dst:Boston"].valid_to is None
    assert facts["dst:Chicago"].valid_to is not None  # retired as history, not deleted


async def test_declared_many_relation_coexists() -> None:
    turns = {
        "Ana: I have a dog named Rex": _edge("has_pet", "Rex"),
        "Ana: I also got a cat named Tom": _edge("has_pet", "Tom"),
    }
    # control: undeclared, the extractor's "state" makes the second pet replace the first
    eng = _engine(extract_graph={})
    await eng.start()
    try:
        await _graph(eng, turns)
        plain = await _facts(eng)
    finally:
        await eng.stop()
    assert sum(f.valid_to is None for f in plain) == 1

    eng = _engine(extract_graph={"cardinality": {"has_pet": "many"}})
    await eng.start()
    try:
        await _graph(eng, turns)
        many = await _facts(eng)
    finally:
        await eng.stop()
    assert len(many) == 2 and all(f.valid_to is None for f in many)


async def test_declared_one_relation_supersedes_an_event_edge() -> None:
    turns = {
        "Ana: I work at Acme": _edge("works_at", "Acme", kind="event"),
        "Ana: I now work at Globex": _edge("works_at", "Globex", kind="event"),
    }
    eng = _engine(extract_graph={"cardinality": {"works_at": "one"}})
    await eng.start()
    try:
        await _graph(eng, turns)
        facts = await _facts(eng)
    finally:
        await eng.stop()
    live = [f for f in facts if f.valid_to is None]
    assert len(facts) == 2 and len(live) == 1 and "Globex" in live[0].content


async def test_cardinality_option_ignores_junk() -> None:
    from memspine.memories.semantic.write_pipeline import cardinality_map

    assert cardinality_map({"Has_Pet": "MANY", "x": "few"}) == {"has_pet": "many"}
    assert cardinality_map(["has_pet"]) == {}


async def test_contest_keeps_both_values_unresolved() -> None:
    eng = _engine(conflict={"contest_ties": True, "contest_window_seconds": 3600})
    await eng.start()
    try:
        for text, who in (("Pat's sister is ten", "ten"), ("Pat's sister is twelve", "twelve")):
            await eng.write(
                text,
                namespace="a",
                memory_type="semantic",
                entity="Pat",
                attribute="sister_age",
                valid_from=T0,
                tags=[who],
            )
        storage = eng._require_started()
        facts = [r for r in await storage.list_records("a", "semantic") if r.attribute]
    finally:
        await eng.stop()
    assert len(facts) == 2
    assert all("disputed" in f.tags for f in facts)
