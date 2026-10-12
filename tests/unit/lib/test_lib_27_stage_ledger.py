import pytest
from pydantic import ValidationError

from memspine.observability.stage_ledger import (
    OTEL_ATTRIBUTE_KEYS,
    ContentGuardError,
    StageEntry,
    StageLedger,
    WriteTargets,
    information_per_token,
    percentile,
    scope_hash,
    to_otel_attributes,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.public import CostRecord


def mk(
    stage="extract", rid="req-1", cc="generative", model="m-1", pt=10, ct=5, calls=1, d=100.0, **kw
):
    return StageEntry(
        stage=stage,
        scope_hash="abc123",
        request_or_job_id=rid,
        cost_class=cc,
        model_id=model,
        prompt_tokens=pt,
        completion_tokens=ct,
        calls=calls,
        duration_ms=d,
        **kw,
    )


BAD_IDS = [
    "Ilse said she moved to Utrecht",
    "a b",
    "x" * 129,
    "line\nbreak",
    "café",
    "",
    "-lead",
    "tab\tx",
]


@pytest.mark.parametrize("bad", BAD_IDS)
@pytest.mark.parametrize("field", ["stage", "rid", "model"])
def test_content_guard_rejects_text(bad, field):
    with pytest.raises((ContentGuardError, ValidationError)):
        mk(**{field: bad})


def test_content_guard_error_is_value_error():
    assert issubclass(ContentGuardError, ValueError)


def test_content_guard_accepts_ids():
    e = mk(stage="write.extract", rid="01HZX:req/7", model="vendor/model-v1.2@2026")
    assert e.stage == "write.extract"
    assert mk(rid="x" * 128).request_or_job_id == "x" * 128


def test_extra_fields_forbidden_and_frozen():
    with pytest.raises(ValidationError):
        StageEntry(
            stage="s", scope_hash="h", request_or_job_id="r", cost_class="tool", text="hello"
        )
    with pytest.raises(ValidationError):
        mk().calls = 3


def test_bad_class_outcome_negative():
    with pytest.raises(ValidationError):
        mk(cc="vision")
    with pytest.raises(ValidationError):
        mk(outcome="weird")
    with pytest.raises(ValidationError):
        mk(pt=-1)
    with pytest.raises(ValidationError):
        mk(d=float("inf"))


def test_ledger_record_is_guarded():
    led = StageLedger()
    led.record(stage="s", scope_hash="h", request_or_job_id="r", cost_class="rerank")
    assert len(led) == 1
    with pytest.raises(ValidationError):
        led.record(stage="s", scope_hash="h", request_or_job_id="Noor likes tea", cost_class="tool")
    assert len(led) == 1


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([5], 50) == 5
    vals = [float(i) for i in range(1, 101)]
    assert percentile(vals, 50) == 50
    assert percentile(vals, 95) == 95
    assert percentile(vals, 100) == 100
    assert percentile([3, 1, 2], 0) == 1
    with pytest.raises(ValueError):
        percentile([1], 101)


def build():
    led = StageLedger()
    led.extend(
        [
            mk("extract", "r1", "generative", "g", 100, 20, 1, 80),
            mk("embed", "r1", "embedding", "e", 30, 0, 2, 20),
            mk("extract", "r2", "generative", "g", 50, 10, 1, 200, outcome="error"),
            mk("sleep", "j1", "generative", "g", 10, 5, 1, 1000, is_job=True),
        ]
    )
    return led


def test_aggregations():
    led = build()
    by_req = led.aggregate("request")
    assert list(by_req) == ["r1", "r2"]
    assert by_req["r1"].calls == 3 and by_req["r1"].total_tokens == 150
    assert by_req["r1"].duration_ms_total == 100
    assert list(led.aggregate("job")) == ["j1"]
    st = led.aggregate("stage")
    assert st["extract"].entries == 2 and st["extract"].failures == 1
    assert st["extract"].duration_ms_p50 == 80 and st["extract"].duration_ms_p95 == 200
    assert led.aggregate("class")["generative"].calls == 3
    assert led.aggregate("class")["embedding"].calls == 2
    assert led.aggregate("model")["g"].prompt_tokens == 160


def test_aggregate_empty():
    assert StageLedger().aggregate("stage") == {}


def test_calls_per_turn_counts_generative_apart():
    led = build()
    assert led.calls_per_turn(4) == 0.75
    assert led.calls_per_turn(4, cost_class="embedding") == 0.5
    assert led.calls_per_turn(4, cost_class=None) == 1.25
    assert led.calls_per_turn(0) is None


def test_write_targets():
    led = build()
    t = WriteTargets(
        write_stages=("extract", "embed"), window_stages=("extract",), sleep_stages=("sleep",)
    )
    v = {x.name: x for x in t.check(led, turns=10, windows=2)}
    assert v["write_p95_ms"].observed == 200 and v["write_p95_ms"].status == "met"
    assert v["generative_calls_per_window"].observed == 1.0
    assert v["generative_calls_per_window"].status == "met"
    assert v["sleep_calls_per_turn"].observed == 0.1 and v["sleep_calls_per_turn"].status == "met"
    tight = WriteTargets(
        write_p95_ms=150,
        generative_calls_per_window=0.5,
        sleep_calls_per_turn=0.05,
        write_stages=("extract", "embed"),
        sleep_stages=("sleep",),
    )
    vm = {x.name: x.status for x in tight.check(led, turns=10, windows=2)}
    assert set(vm.values()) == {"missed"}


def test_targets_not_measured_and_disabled():
    v = WriteTargets().check(StageLedger())
    assert [x.status for x in v] == ["not_measured"] * 3
    off = WriteTargets(
        write_p95_ms=None, generative_calls_per_window=None, sleep_calls_per_turn=None
    )
    assert off.check(StageLedger()) == []


def test_jobs_excluded_from_write_latency():
    assert set(build().request_durations()) == {"r1", "r2"}


def test_information_per_token():
    assert information_per_token(100, True, total_tokens=400) == 0.25
    assert information_per_token(100, False, total_tokens=400) == 0.0
    assert information_per_token(100, None, total_tokens=400) is None
    assert information_per_token(100, True) is None
    assert information_per_token(900, True, total_tokens=400) == 1.0
    with pytest.raises(ValueError):
        information_per_token(-1, True, total_tokens=10)


def test_otel_allowed_keys_only():
    attrs = to_otel_attributes(mk())
    assert set(attrs) <= set(OTEL_ATTRIBUTE_KEYS)
    assert "memspine.model_id" in attrs
    assert "memspine.model_id" not in to_otel_attributes(mk(model=None))
    assert all(isinstance(v, (str, int, float)) for v in attrs.values())


def test_from_cost_record():
    scope = OwnerScope(tenant="t-Ilse", owner="Bram")
    rec = CostRecord(
        cost_class="embedding",
        scope=scope,
        request_id="req-9",
        model_id="emb-1",
        calls=2,
        tokens_in=40,
        latency_ms=12.5,
    )
    e = StageEntry.from_cost_record(rec, stage="embed")
    assert e.scope_hash == scope_hash(scope) and "Ilse" not in e.scope_hash
    assert e.calls == 2 and e.prompt_tokens == 40 and not e.is_job
    job = CostRecord(cost_class="tool", job_id="job-3", tool_name="web.fetch")
    ej = StageEntry.from_cost_record(job, stage="sleep")
    assert ej.is_job and ej.scope_hash == "none" and ej.model_id == "web.fetch"


def test_determinism():
    a, b = build(), build()
    assert a.entries == b.entries
    assert a.aggregate("stage") == b.aggregate("stage")
    ra = [x.model_dump() for x in WriteTargets().check(a, turns=3, windows=1)]
    rb = [x.model_dump() for x in WriteTargets().check(b, turns=3, windows=1)]
    assert ra == rb
    s1 = scope_hash(OwnerScope(tenant="a", owner="b"))
    assert s1 == scope_hash(OwnerScope(tenant="a", owner="b"))
    assert s1 != scope_hash(OwnerScope(tenant="a", owner="c"))
