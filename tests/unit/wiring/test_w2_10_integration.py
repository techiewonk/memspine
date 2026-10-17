"""W2-10: the integration ladder as an envelope stage (``write.integration_v2``, order 400). Model-free:
envelopes are built directly, and one end-to-end window runs with a stub window LLM and the hash embedder."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.derive.envelope_stages import EnvelopeStageContext
from memspine.derive.integrate import CardinalityRegistry
from memspine.derive.integration_stage import (
    ConflictLadder,
    DefaultVerdict,
    deposit_tags,
    integration_stage,
)
from memspine.models.providers.llm.base import LLMRouter
from memspine.observability import tap
from memspine.prompts.structured import STRUCTURED_SYSTEM
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes
from memspine.store.records import MemoryRecord, SourceInfo

T0 = datetime(2023, 3, 1, 9, 0, tzinfo=UTC)
NS = "default"


def _ref(at: datetime) -> TemporalRef:
    return TemporalRef(at=at, precision="instant", basis="explicit")


def env(
    value: str,
    day: int,
    *,
    attr: str = "lives_in",
    subject: str = "Ilse",
    trust: float = 0.8,
    modality: str = "asserted",
    binding: str = "bound",
    until: int | None = None,
    kind: str = "state",
    change_hint: bool = False,
    extras: dict[str, Any] | None = None,
    entity_id: str | None = None,
    occ: str | None = None,
) -> AssertionEnvelope:
    at = T0 + timedelta(days=day)
    time = TimeAxes(
        event=Interval(
            start=_ref(at), end=_ref(T0 + timedelta(days=until)) if until is not None else None
        ),
        source_time=at,
    )
    oid = occ or f"occ-{value}-{day}"
    return AssertionEnvelope(
        write_id=f"w-{oid}",
        assertion_key=AssertionKey(src=subject, rel=attr, dst="", kind=attr),
        occurrence_id=oid,
        scope=OwnerScope(tenant=NS, owner="a"),
        text=f"{subject} {attr} {value}",
        kind=kind,
        subject=Mention(surface=subject, mention_id=f"m-{oid}", entity_id=entity_id),
        attribute=attr,
        change_hint=change_hint,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=f"t-{oid}", source_version=1, seg_index=0),
                source_family="Ilse",
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            binding=binding,  # type: ignore[arg-type]
            modality=modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,  # type: ignore[arg-type]
        ),
        src_class="user_stated",
        extras=extras or {},
    )


def record(
    value: str, day: int, *, attr: str = "lives_in", entity: str = "Ilse", trust: float = 0.5
) -> MemoryRecord:
    return MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=f"{entity} {attr} {value}",
        source=SourceInfo(role="user", channel="mining"),
        entity=entity,
        attribute=attr,
        valid_from=T0 + timedelta(days=day),
        trust=trust,
        tags=["atomic_fact"],
    )


def ctx() -> EnvelopeStageContext:
    return EnvelopeStageContext(
        namespace=NS, window_id="w1", lines={}, records=[], compile_ctx=None
    )


async def run(
    envs: list[AssertionEnvelope],
    *,
    stored: list[MemoryRecord] | None = None,
    card: dict[str, str] | None = None,
    default: DefaultVerdict | None = None,
) -> tuple[list[AssertionEnvelope], EnvelopeStageContext]:
    async def live(_ns: str) -> list[MemoryRecord]:
        return list(stored or [])

    c = ctx()
    stage = integration_stage(
        live, cardinality=card if card is not None else {"lives_in": "one"}, default=default
    )
    return await stage.run(envs, c), c


def ops(out: list[AssertionEnvelope]) -> list[str | None]:
    return [e.verdict.op if e.verdict else None for e in out]


# --- flag and config --------------------------------------------------------------------------
def test_flag_off_by_default() -> None:
    w = MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}}).write
    assert (
        w.integration_v2 is False
        and w.integration_cardinality == {}
        and w.integration_decide_unhinted is False
    )


def test_stage_order_is_400() -> None:
    async def live(_n: str) -> list[MemoryRecord]:
        return []

    s = integration_stage(live)
    assert s.order == 400 and s.name == "integration"


# --- the ladder -------------------------------------------------------------------------------
async def test_first_value_appends_and_events_have_no_key() -> None:
    out, c = await run([env("Oslo", 0), env("a trip", 1, kind="event", attr="trip")])
    assert ops(out) == ["append", "append"]
    assert out[1].verdict.rung == "no_key"  # type: ignore[union-attr]
    assert c.notes["integrate_append"] == 2


async def test_cardinality_one_newer_value_supersedes_stored() -> None:
    stored = [record("Oslo", 0)]
    out, _ = await run([env("Rome", 5)], stored=stored)
    v = out[0].verdict
    assert v is not None and v.op == "supersede" and v.rung == "trust_tier"
    assert out[0].extras["integrate_target"]  # names the record it retires as history


async def test_equal_tier_supersedes() -> None:
    out, _ = await run([env("Rome", 5, trust=0.8)], stored=[record("Oslo", 0, trust=0.5)])
    assert ops(out) == ["supersede"]


async def test_lower_tier_does_not_supersede() -> None:
    out, _ = await run([env("Rome", 5, trust=0.3)], stored=[record("Oslo", 0, trust=0.5)])
    assert ops(out) == ["contest"]  # both kept, neither supersedes
    assert "disputed" in deposit_tags(out[0])


async def test_closed_past_interval_coexists() -> None:
    out, _ = await run([env("Rome", 1, until=3)], stored=[record("Oslo", 5)])
    v = out[0].verdict
    assert v is not None and v.op == "coexist" and v.rung == "time"


async def test_bound_negation_retires() -> None:
    out, _ = await run([env("Oslo", 6, modality="negated")], stored=[record("Oslo", 0)])
    v = out[0].verdict
    assert v is not None and v.op == "supersede" and v.rung == "negation"
    assert out[0].extras["integrate_outcome"] == "retire"
    assert "retract" in deposit_tags(out[0])


async def test_cardinality_many_coexists() -> None:
    out, _ = await run(
        [env("Biscuit", 4, attr="has_pet")],
        stored=[record("Mochi", 0, attr="has_pet")],
        card={"has_pet": "many"},
    )
    v = out[0].verdict
    assert v is not None and v.op == "coexist" and v.rung == "cardinality"
    assert "integ:coexist" in deposit_tags(out[0])


async def test_contest_is_recorded_for_a_marked_transition() -> None:
    # undeclared attribute + an extraction hint that the value changed: contest, not a silent supersede (W-22)
    out, c = await run([env("Rome", 5, change_hint=True)], stored=[record("Oslo", 0)], card={})
    v = out[0].verdict
    assert v is not None and v.op == "contest" and v.hint == "change_hint"
    tags = deposit_tags(out[0])
    assert "integ:contest" in tags and "disputed" in tags
    assert c.notes["integrate_contest"] == 1


async def test_no_hint_defers_to_kc_and_counts() -> None:
    out, c = await run([env("Rome", 5)], stored=[record("Oslo", 0)], card={})
    v = out[0].verdict
    assert v is not None and v.op == "defer_kc" and v.rung == "no_hint"
    assert c.notes["integrate_defer_kc"] == 1
    # deferral leaves the old door behaviour: no coexist/contest tag
    assert not {"integ:coexist", "integ:contest"} & set(deposit_tags(out[0]))


async def test_defer_can_be_switched_off() -> None:
    out, _ = await run(
        [env("Rome", 5)],
        stored=[record("Oslo", 0)],
        card={},
        default=DefaultVerdict(defer_without_hints=False),
    )
    assert ops(out) == ["coexist"]  # M-12: undeclared means many


async def test_resolved_entity_id_groups_aliases() -> None:
    # W2-09 resolved "Ilse M." to the same entity id as the stored "Ilse"
    out, _ = await run(
        [env("Rome", 5, subject="Ilse M.", entity_id="ilse")], stored=[record("Oslo", 0)]
    )
    assert ops(out) == ["supersede"]
    # without the id the name is its own key: nothing to conflict with
    out, _ = await run([env("Rome", 5, subject="Ilse M.")], stored=[record("Oslo", 0)])
    assert ops(out) == ["append"]


async def test_other_subject_does_not_conflict() -> None:
    out, _ = await run([env("Rome", 5, subject="Bram")], stored=[record("Oslo", 0)])
    assert ops(out) == ["append"]


# --- A-B-A and order swap ---------------------------------------------------------------------
def final_live(envs: list[AssertionEnvelope]) -> set[str]:
    """Replay the ladder over the whole set, arrival order given, and return the live value texts."""
    from memspine.derive.integrate import replay

    reg = CardinalityRegistry()
    reg.declare(NS, "lives_in", "one")
    state = replay(envs, reg)
    return {v for _a, v in state.live}


async def test_aba_keeps_history_and_current_value() -> None:
    a1, b, a2 = env("Oslo", 0, occ="a1"), env("Rome", 4, occ="b"), env("Oslo", 8, occ="a2")
    out, _ = await run([a1, b, a2])
    # in-window: B supersedes A1, A2 supersedes B, nothing is dropped
    assert ops(out) == ["append", "supersede", "supersede"]
    assert out[1].extras["integrate_target"] == "a1" and out[2].extras["integrate_target"] == "b"
    assert (
        len(out) == 3
    )  # the history is intact: every value is kept for the door to retire as history
    assert any("oslo" in t for t in final_live([a1, b, a2]))
    assert not any("rome" in t for t in final_live([a1, b, a2]))


async def test_order_swap_gives_same_final_state() -> None:
    a, b, c = env("Oslo", 0, occ="a"), env("Rome", 4, occ="b"), env("Lima", 9, occ="c")
    fwd = final_live([a, b, c])
    for order in ([c, b, a], [b, c, a], [a, c, b], [c, a, b]):
        assert final_live(order) == fwd
    # the stage verdict for the late-arriving older value is history, not a supersede of the newer value
    out, _ = await run([c, a])
    assert out[1].verdict is not None and out[1].verdict.op == "supersede"
    assert (
        out[1].extras["integrate_target"] == "occ-Lima-9"
        or out[1].extras["integrate_target"] == "c"
    )


def test_ladder_is_pure_and_names_the_rung() -> None:
    reg = CardinalityRegistry()
    reg.declare(NS, "lives_in", "one")
    ladder = ConflictLadder(reg)
    cur = [env("Oslo", 0, occ="cur")]
    verdict = ladder.decide(env("Rome", 5, occ="inc"), cur)
    assert (verdict.op, verdict.rung, verdict.target) == ("supersede", "trust_tier", "cur")
    assert cur[0].status == "active"  # untouched


# --- taps -------------------------------------------------------------------------------------
async def test_taps_fire_under_write_integrate() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    real = tap.event

    def spy(name: str, **kw: Any) -> None:
        seen.append((name, kw))
        real(name, **kw)

    tap.event = spy  # type: ignore[assignment]
    try:
        await run([env("Rome", 5)], stored=[record("Oslo", 0)])
    finally:
        tap.event = real  # type: ignore[assignment]
    names = [n for n, _ in seen]
    assert "write.integrate.decision" in names
    d = next(kw for n, kw in seen if n == "write.integrate.decision")
    assert d["op"] == "supersede" and d["rung"] == "trust_tier"


# --- end to end: a window with the stub LLM ---------------------------------------------------
LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class CityLLM:
    """One ``state`` item per transcript line ``Ilse: I live in <City>``; the span is the city."""

    provider_id = "fake:city"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        assert messages[0]["content"] == STRUCTURED_SYSTEM
        self.calls += 1
        items = []
        for n, text in LINE.findall(messages[-1]["content"]):
            who, said = text.split(": ", 1)
            city = said.rsplit(" ", 1)[1]
            items.append(
                {
                    "s": who,
                    "r": "lives_in",
                    "o": city,
                    "text": f"{who} lives in {city}",
                    "kind": "state",
                    "mode": "asserted",
                    "neg": False,
                    "when": None,
                    "until": None,
                    "lines": [int(n)],
                    "span": city,
                    "speaker": who,
                }
            )
        return json.dumps(items)


async def engine(monkeypatch: pytest.MonkeyPatch, llm: Any, **write: Any) -> Engine:
    async def router(config: MemspineConfig, hooks: object = None, wiring: object = None) -> LLMRouter:
        return LLMRouter({"window_extract": llm})

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': ":memory:", 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            "envelope_v2": True,
            **write,
        },
        llm={"roles": {"window_extract": {"provider": "litellm", "settings": {"model": "fake/window", "timeout_seconds": 60.0}}}},
    )
    await eng.start()
    return eng


async def say(eng: Engine, session: str, city: str, day: int) -> None:
    msg = [
        {
            "role": "user",
            "content": f"Ilse: I live in {city}",
            "timestamp": (T0 + timedelta(days=day)).isoformat(),
        }
    ]
    await eng.ingest_messages(msg, namespace="a", session_id=session, group_id=session)
    await eng.flush_windows()


async def cities(eng: Engine) -> tuple[set[str], set[str]]:
    rows = [
        r
        for r in await eng._require_started().list_records("a", "semantic")
        if "window_fact" in r.tags
    ]
    live = {
        r.content.rsplit(" ", 1)[1]
        for r in rows
        if r.status.value == "activated" and r.valid_to is None
    }
    every = {r.content.rsplit(" ", 1)[1] for r in rows}
    return live, every


def test_stage_registered_only_with_the_flag() -> None:
    from memspine.ingest.windows import _envelope_stages

    class E:
        _storage = None

    assert _envelope_stages(E(), MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}})) == ()
    cfg = MemspineConfig(
        runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}},
        write={"window_extract": "facts", "envelope_v2": True, "integration_v2": True},
    )
    assert [s.order for s in _envelope_stages(E(), cfg)] == [400]


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0)])
async def test_e2e_aba_and_order_swap(
    monkeypatch: pytest.MonkeyPatch, order: tuple[int, ...]
) -> None:
    plan = [("s0", "Oslo", 0), ("s1", "Rome", 4), ("s2", "Oslo", 8)]
    eng = await engine(
        monkeypatch, CityLLM(), integration_v2=True, integration_cardinality={"lives_in": "one"}
    )
    try:
        for i in order:
            s, city, day = plan[i]
            await say(eng, s, city, day)
        live, every = await cities(eng)
        assert live == {
            "Oslo"
        }  # the current value is right whichever way round the sessions arrived
        assert every == {"Oslo", "Rome"}  # and Rome is kept as history
        rome = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if r.content.endswith("Rome")
        ]
        assert [r.status.value for r in rome] == ["archived"]  # retired, not deleted
        facts = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if "window_fact" in r.tags
        ]
        assert any(t.startswith("integ:") for r in facts for t in r.tags)

    finally:
        await eng.stop()


async def test_e2e_flag_off_has_no_integ_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await engine(monkeypatch, CityLLM())
    try:
        await say(eng, "s0", "Oslo", 0)
        facts = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if "window_fact" in r.tags
        ]
        assert facts and not any(t.startswith("integ") for r in facts for t in r.tags)
    finally:
        await eng.stop()


async def test_e2e_cardinality_many_keeps_both_live(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await engine(
        monkeypatch, CityLLM(), integration_v2=True, integration_cardinality={"lives_in": "many"}
    )
    try:
        await say(eng, "s0", "Oslo", 0)
        await say(eng, "s1", "Rome", 4)
        live, _ = await cities(eng)
        assert live == {"Oslo", "Rome"}
    finally:
        await eng.stop()


def test_contest_verdict_tag_makes_the_door_contest() -> None:
    from memspine.core.policies.conflict import ConflictPolicy, ConflictVerdict

    old = record("Oslo", 0)
    new = record("Rome", 5)
    new = new.model_copy(update={"tags": [*new.tags, "integ:contest"]})
    verdict, rule = ConflictPolicy.bind().decide(new, old)
    assert verdict is ConflictVerdict.CONTEST and rule == "W2-10_integration_contest"


def test_behavioural_inv_order_swap_passes_with_the_flag_and_fails_without(tmp_path: Path) -> None:
    """The model-free behavioural case (evals/): a reverse-order ingest ends in the same live state."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "evals"))
    from memspine_evals.behavioural import Status, run_behavioural

    def factory(on: bool):  # type: ignore[no-untyped-def]
        def build(d: Path) -> Engine:
            return Engine(
                template="core",
                dotenv_path=None,
                storage={'provider': "sqlite", 'settings': {'path': str(d / "b.db"), 'busy_timeout_s': 5.0}},
                embedding={'provider': "hash", 'settings': {}},
                read={"rerank": "off", "record_access": False},
                memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
                write=(
                    {
                        "integration_v2": True,
                        "envelope_v2": True,
                        "window_extract": "facts",
                        "online_extractor_v2": True,
                    }
                    if on
                    else {}
                ),
            )

        return build

    only = {"INV-ORDER-SWAP"}
    assert run_behavioural(factory(True), only=only).results[0].status is Status.PASS
    assert (
        run_behavioural(factory(False), only=only).results[0].status is Status.FAIL
    )  # the old door
