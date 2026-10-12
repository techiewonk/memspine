"""LIB-02: score the integration ladder against evals/labelled/contradictions.v1.jsonl (X-05)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from labelled.loader import Assertion, ContradictionItem, load_contradictions

from memspine.derive.integrate import CardinalityRegistry, apply_decision, integrate
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes

TRUST = {"default": 0.9, "low": 0.3}  # the coarse two-tier stand-in of SCHEMA.md
PAST_PREDICATES = {"lived_in"}  # extraction would resolve a past-tense phrase to a closed interval
SCORES: dict[str, dict[str, float]] = {}


def _day(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _envelope(
    item: ContradictionItem, a: Assertion, n: int, event_end: datetime | None
) -> AssertionEnvelope:
    event = None
    if event_end is not None:
        event = Interval(end=TemporalRef(at=event_end, precision="day", basis="explicit"))
    time = TimeAxes(event=event, source_time=_day(a.source_time), recorded_at=_day(a.source_time))
    return AssertionEnvelope(
        write_id=f"w-{item.id}-{a.id}",
        assertion_key=AssertionKey(
            src=a.subject, rel=a.predicate, dst=a.value, kind=item.attribute
        ),
        occurrence_id=f"{item.id}-{a.id}",
        scope=OwnerScope(tenant="lab", owner="lab"),
        text=a.text,
        kind=item.attribute,
        subject=Mention(surface=a.subject, mention_id=f"m-{a.subject}"),
        attribute=item.attribute,
        value=a.value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="lab", source_version=1, seg_index=n),
                source_family=a.source_family,
            )
        ],
        assessment=Assessment(
            source_trust=TRUST[a.source_trust],
            binding=a.binding,  # type: ignore[arg-type]
            modality=a.modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,
        ),
        src_class="user_stated",
    )


def decide(item: ContradictionItem) -> str:
    reg = CardinalityRegistry()
    reg.declare("lab", item.attribute, item.cardinality)  # type: ignore[arg-type]
    store: list[AssertionEnvelope] = []
    for n, a in enumerate(item.existing):
        env = _envelope(item, a, n, None)
        store = apply_decision(integrate(env, store, reg), env, store)
    end = None
    if item.incoming.predicate in PAST_PREDICATES:
        end = min(_day(a.source_time) for a in item.existing) - timedelta(days=1)
    inc = _envelope(item, item.incoming, 99, end)
    return integrate(inc, store, reg).outcome


def test_score_against_labelled_contradictions():
    data = load_contradictions()
    assert len(data) == 44
    per_label: dict[str, Counter[str]] = defaultdict(Counter)
    wrong: list[tuple[str, str, str]] = []
    for item in data.items:
        got = decide(item)
        per_label[item.expected]["total"] += 1
        if got == item.expected:
            per_label[item.expected]["ok"] += 1
        else:
            wrong.append((item.id, item.expected, got))
    for label, c in sorted(per_label.items()):
        SCORES[label] = {"ok": c["ok"], "total": c["total"], "accuracy": c["ok"] / c["total"]}
        print(f"{label:10s} {c['ok']:2d}/{c['total']:2d} = {c['ok'] / c['total']:.2f}")
    overall = sum(c["ok"] for c in per_label.values()) / len(data)
    print(f"overall    {overall:.3f}; disagreements: {wrong}")
    assert set(SCORES) == {"supersede", "coexist", "contest", "tentative", "retire"}
    assert not wrong, wrong
