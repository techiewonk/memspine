"""A02: ``read.event_ledger`` (read-time event ledger): pure validation / linking, and the engine
path with a fake ``extract`` LLM. No network, no real model."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.event_ledger import (
    event_interval,
    is_event_question,
    link_mentions,
    normalise_status,
    render_ledger,
    validate_events,
)
from memspine.engine import search_forensics
from memspine.services.llm import structured
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def ev(**kw: Any) -> SimpleNamespace:
    base = {
        "actor": "Ana",
        "action": "visit Rome",
        "object": "",
        "status": "planned",
        "line": 1,
        "span": "",
        "when": "",
        "same_as": 0,
        "identity": "",
    }
    return SimpleNamespace(**{**base, **kw})


LINES = [
    "Ana: I am planning to visit Rome next Friday",
    "Ana: We finally visited Rome last weekend, the same trip I planned",
    "Ana: The Rome trip got cancelled again",
    "Bob: I visited Rome last year",
]
DATES = [date(2023, 5, 1), date(2023, 5, 20), date(2023, 6, 1), date(2023, 6, 2)]


# -- pure ---------------------------------------------------------------------------------------


def test_status_aliases_and_unknown() -> None:
    assert [normalise_status(x) for x in ("Planned", "completed", "Canceled", "called off")] == [
        "planned",
        "done",
        "cancelled",
        "cancelled",
    ]
    assert normalise_status("maybe") is None and normalise_status("") is None


def test_event_question_cues() -> None:
    assert is_event_question("When did Ana visit Rome?", "date")
    assert is_event_question("Was the trip cancelled?")
    assert is_event_question("How many times did she go?")
    assert not is_event_question("What is Ana's favourite colour?", "description")


def test_event_interval_resolves_the_phrase_not_the_mention_date() -> None:
    assert event_interval("next Friday", date(2023, 5, 1)) == (date(2023, 5, 5), date(2023, 5, 5))
    assert event_interval("on 3 May 2023", date(2023, 6, 1)) == (date(2023, 5, 3), date(2023, 5, 3))
    assert event_interval("2023-07-04", None) == (date(2023, 7, 4), date(2023, 7, 4))
    assert event_interval("", date(2023, 5, 1)) is None  # no phrase: unknown, not the mention date
    assert event_interval("sometime", date(2023, 5, 1)) is None


def test_validation_drops_off_line_spans_and_bad_status() -> None:
    raw = [
        ev(line=1, span="planning to visit Rome", when="next Friday"),
        ev(line=1, span="was never said anywhere"),
        ev(line=9, span="x"),
        ev(line=1, span="planning to visit Rome", status="maybe"),
        ev(line=1, span="planning to visit Rome", when="a phrase not on the line"),
    ]
    kept, dropped = validate_events(raw, LINES, DATES)
    assert [m.index for m in kept] == [1, 5]
    assert [d["reason"] for d in dropped] == ["span_not_on_line", "no_such_line", "status"]
    assert kept[0].start == date(2023, 5, 5) and kept[0].mention == date(2023, 5, 1)
    assert kept[1].start is None  # a time phrase that is not on the line is not used


def _mentions(extra: list[SimpleNamespace]) -> tuple[list[Any], list[Any]]:
    kept, _ = validate_events(extra, LINES, DATES)
    return link_mentions(kept, LINES)


PLAN = ev(line=1, span="planning to visit Rome", when="next Friday")
DONE = ev(
    line=2,
    span="finally visited Rome",
    status="done",
    when="last weekend",
    same_as=1,
    identity="the same trip",
)


def test_linking_needs_identity_evidence_and_updates_the_state() -> None:
    events, refused = _mentions([PLAN, DONE])
    assert len(events) == 1 and not refused
    e = events[0]
    assert [m.status for m in e.mentions] == ["planned", "done"] and e.status == "done"
    assert e.linked_on == ["2->1"]


def test_same_words_without_a_link_stay_two_events() -> None:
    events, _ = _mentions([PLAN, ev(**{**DONE.__dict__, "same_as": 0, "identity": ""})])
    assert len(events) == 2


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"identity": ""}, "no_identity_cue"),  # the span carries "finally" but identity is empty
        ({"identity": "a phrase that is not on the line"}, "no_identity_cue"),
        ({"actor": "Bob"}, "actor"),
        ({"action": "buy shoes"}, "no_overlap"),
    ],
)
def test_refused_links_say_why(patch: dict[str, Any], reason: str) -> None:
    done = ev(**{**DONE.__dict__, **patch})
    if reason == "no_identity_cue":
        # the span of the later mention holds no cue either
        done = ev(**{**done.__dict__, "span": "visited Rome last weekend"})
    events, refused = _mentions([PLAN, done])
    assert len(events) == 2 and refused[0]["reason"] == reason


def test_two_done_reports_on_distant_days_are_two_events() -> None:
    first = ev(line=1, span="planning to visit Rome", status="done", when="2023-05-05")
    again = ev(**{**DONE.__dict__, "when": "2023-09-30"})
    lines = list(LINES)
    lines[0] = "Ana: I visited Rome on 2023-05-05, planning to visit Rome"
    lines[1] = "Ana: We finally visited Rome on 2023-09-30, the same trip I planned"
    kept, _ = validate_events([first, again], lines, DATES)
    events, refused = link_mentions(kept, lines)
    assert len(events) == 2 and refused[0]["reason"] == "interval_conflict"


def test_a_slipped_plan_still_links_on_a_cue() -> None:
    plan = ev(line=1, span="planning to visit Rome", when="2023-05-05")
    done = ev(**{**DONE.__dict__, "when": "2023-09-30"})
    lines = list(LINES)
    lines[0] = "Ana: I am planning to visit Rome on 2023-05-05"
    lines[1] = "Ana: We finally visited Rome on 2023-09-30, the same trip I planned"
    kept, _ = validate_events([plan, done], lines, DATES)
    events, refused = link_mentions(kept, lines)
    assert len(events) == 1 and not refused and events[0].status == "done"


def test_other_object_does_not_link() -> None:
    a = ev(line=1, span="planning to visit Rome", object="the Colosseum")
    b = ev(**{**DONE.__dict__, "object": "the Vatican"})
    events, refused = _mentions([a, b])
    assert len(events) == 2 and refused[0]["reason"] == "other_object"


def test_render_is_compact_and_bounded() -> None:
    events, _ = _mentions([PLAN, DONE, ev(line=3, span="Rome trip got cancelled", status="cancelled")])
    text, shown = render_ledger(events, heading="LEDGER:", max_events=12)
    assert text.splitlines()[0] == "LEDGER:" and len(shown) == len(events) == 2
    assert "E1 [done] Ana visit Rome; event time: 2023-05-13..2023-05-14" in text
    assert "mentions: 2023-05-01 planned (L1), 2023-05-20 done (L2)" in text
    assert "E2 [cancelled]" in text and "time unknown" in text
    short, kept = render_ledger(events, heading="LEDGER:", max_events=12, fits=lambda t: len(t) < 150)
    assert len(kept) < len(events) and len(short) < 150
    assert render_ledger(events, heading="x", max_events=12, fits=lambda t: False) == ("", [])


# -- engine -------------------------------------------------------------------------------------

SEED = [
    "Ana: I am planning to visit Rome next Friday",
    "Ana: We finally visited Rome last weekend, the same trip I planned",
    "Ana: My sister bakes bread on Sundays",
]


class _EventsStub:
    """An ``extract`` stub: emits the ``rules`` entries for the numbered lines that hold the
    needle, as YAML, in the order of the lines."""

    def __init__(self, rules: list[tuple[str, dict[str, Any]]]) -> None:
        self.rules = rules
        self.prompts: list[str] = []

    @property
    def provider_id(self) -> str:
        return "stub:events"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        text = messages[-1]["content"]
        self.prompts.append(text)
        out = ["events:"]
        for n, line in re.findall(r"^\[(\d+)\] (.*)$", text, flags=re.M):
            for needle, fields in self.rules:
                if needle in line:
                    body = {**fields, "line": int(n)}
                    out.append(
                        "  - " + "\n    ".join(f"{k}: {v!r}" for k, v in body.items()) if False else
                        "  - " + "\n    ".join(f"{k}: {str(v)!r}" for k, v in body.items())
                    )
        return "\n".join(out) if len(out) > 1 else "events: []"


RULES = [
    (
        "planning to visit",
        {
            "actor": "Ana",
            "action": "visit Rome",
            "status": "planned",
            "span": "planning to visit Rome",
            "when": "next Friday",
            "same_as": 0,
            "identity": "",
        },
    ),
    (
        "finally visited",
        {
            "actor": "Ana",
            "action": "visit Rome",
            "status": "done",
            "span": "finally visited Rome",
            "when": "last weekend",
            "same_as": 1,
            "identity": "the same trip",
        },
    ),
]


def _with_roles(monkeypatch: pytest.MonkeyPatch, stubs: dict[str, LLMService]) -> None:
    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _seed(eng: Engine) -> list[str]:
    ids = []
    for i, text in enumerate(SEED):
        rec = await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(days=i * 19)
        )
        ids.append(rec.record_id)
    return ids


QUESTION = "When did Ana visit Rome?"


async def _read(eng: Engine, query: str = QUESTION) -> Any:
    return await eng.read(query, namespace="a", mode="retrieve", top_k=5, budget_tokens=2000)


@pytest.fixture(autouse=True)
def _structured_defaults() -> Iterator[None]:
    yield
    structured.configure(None)


async def test_off_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _EventsStub(RULES)
    _with_roles(monkeypatch, {"extract": stub})
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        assert stub.prompts == [] and "event_ledger" not in stages
    finally:
        await eng.stop()


async def test_a_ledger_block_carries_status_event_time_and_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _EventsStub(RULES)
    _with_roles(monkeypatch, {"extract": stub})
    base, eng = _engine(), _engine(event_ledger="on")
    await base.start()
    await eng.start()
    try:
        await _seed(base)
        ids = await _seed(eng)
        want = [r.content for r in (await _read(base)).context.records]
        before = len(await eng._require_started().list_records("a"))
        with search_forensics() as stages:
            out = await _read(eng)
        info = stages["event_ledger"]
        assert info["fired"] and info["llm_calls"] == 1 and info["stop"] == "built"
        assert info["mentions"] == 2 and info["valid"] == 2 and info["events"] == 1
        block = out.context.records[-1]
        assert "event_ledger" in block.tags
        assert "E1 [done] Ana visit Rome" in block.content
        assert "planned (L1)" in block.content and "done (L2)" in block.content
        # the event time is the resolved phrase, not the mention date
        assert "event time: 2023-05-13..2023-05-14" in block.content
        assert set(block.source.parents) <= set(ids) and block.source.parents
        # raw turns stay authoritative and nothing derived is stored
        assert [r.content for r in out.context.records[:-1]] == want
        assert len(await eng._require_started().list_records("a")) == before
        assert await eng._require_started().get_record(block.record_id) is None
    finally:
        await base.stop()
        await eng.stop()


async def test_a_repeat_without_identity_evidence_is_two_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = [RULES[0], (RULES[1][0], {**RULES[1][1], "same_as": 0, "identity": ""})]
    _with_roles(monkeypatch, {"extract": _EventsStub(rules)})
    eng = _engine(event_ledger="on")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await _read(eng)
        assert stages["event_ledger"]["events"] == 2
        assert "E2 [done]" in out.context.records[-1].content
    finally:
        await eng.stop()


async def test_an_invented_span_is_dropped_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = [RULES[0], (RULES[1][0], {**RULES[1][1], "span": "she flew to Paris"})]
    _with_roles(monkeypatch, {"extract": _EventsStub(rules)})
    eng = _engine(event_ledger="on")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        info = stages["event_ledger"]
        assert info["mentions"] == 2 and info["valid"] == 1
        assert info["dropped"][0]["reason"] == "span_not_on_line"
    finally:
        await eng.stop()


async def test_not_an_event_question_does_not_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _EventsStub(RULES)
    _with_roles(monkeypatch, {"extract": stub})
    eng = _engine(event_ledger="on")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng, "What does Ana's sister bake?")
        assert stub.prompts == [] and stages["event_ledger"]["stop"] == "not_triggered"
        eng._config().read.event_ledger_trigger = "always"
        with search_forensics() as stages:
            await _read(eng, "What does Ana's sister bake?")
        assert len(stub.prompts) == 1
    finally:
        await eng.stop()


async def test_unbound_role_and_failure_keep_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        provider_id = "stub:boom"

        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            raise RuntimeError("down")

    for stubs, stop in (({}, "unbound"), ({"extract": _Boom()}, "error")):
        _with_roles(monkeypatch, stubs)  # type: ignore[arg-type]
        base, eng = _engine(), _engine(event_ledger="on")
        await base.start()
        await eng.start()
        try:
            await _seed(base)
            await _seed(eng)
            want = [r.content for r in (await _read(base)).context.records]
            with search_forensics() as stages:
                out = await _read(eng)
            assert [r.content for r in out.context.records] == want
            assert stages["event_ledger"]["stop"] == stop
        finally:
            await base.stop()
            await eng.stop()
