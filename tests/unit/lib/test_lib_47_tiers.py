"""LIB-47: depth tiers, ReadRequest normalisation, evidence unit. Invented names only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memspine.read.cache import ReadCacheKey
from memspine.read.contract_rules import TimeView, TypedContract
from memspine.read.router_rules import LayerState, ServiceState
from memspine.read.tiers import (
    DEFAULT_TIER_TABLE,
    ReadConfig,
    ReadConfigError,
    ReadRequestError,
    Tier,
    choose_tier,
    count_units,
    fit_units,
    normalise_request,
    prepare_read,
    unit_of,
    unit_tokens,
)
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.public.read import EvidenceBundle, ReadRequest

SCOPE = {"tenant": "acme", "owner": "ilse"}
GROUP = {"tenant": "acme", "owner": "household", "owner_kind": "group"}
FIX = Path(__file__).parents[1] / "schemas" / "fixtures" / "durable" / "assertion_envelope.v1.json"

WITH_WORKER = LayerState(services=ServiceState(worker_model=True))
NO_WORKER = LayerState(services=ServiceState(worker_model=False))


def req(**kw):
    base = {"query": "Where does Bram live?", "scope": SCOPE}
    base.update(kw)
    return base


def contract(shape="single", view="current"):
    return TypedContract(shape=shape, time_view=TimeView(kind=view))


# ------------------------------------------------------------------ budgets


def test_tier_default_budgets():
    f, s, d = (DEFAULT_TIER_TABLE[n] for n in ("fast", "standard", "deep"))
    assert (f.max_generative_calls, s.max_generative_calls, d.max_generative_calls) == (0, 0, 2)
    assert (f.slo_p95_ms, f.slo_p95_ms_rerank) == (150, 350)
    assert s.slo_p95_ms == 500 and d.slo_p95_ms == 3000
    assert all(t.max_rerank_pool <= 64 for t in DEFAULT_TIER_TABLE.values())
    assert f.max_rerank_pool < s.max_rerank_pool < d.max_rerank_pool
    assert f.evidence_budget(None) == int(2048 * f.token_share)
    assert f.evidence_budget(1000) == int(1000 * f.token_share)


def test_tier_validation():
    with pytest.raises(ValueError):
        Tier(
            name="fast",
            slo_p95_ms=300,
            slo_p95_ms_rerank=100,
            max_generative_calls=0,
            max_rerank_pool=8,
            token_share=0.5,
        )
    with pytest.raises(ValueError):
        Tier(
            name="fast",
            slo_p95_ms=1,
            slo_p95_ms_rerank=1,
            max_generative_calls=0,
            max_rerank_pool=65,
            token_share=0.5,
        )


def test_config_validation_and_digest():
    with pytest.raises(ValueError):
        ReadConfig(allowed_depths=())
    with pytest.raises(ValueError):
        ReadConfig(default_depth="deep", allowed_depths=("fast",))
    with pytest.raises(ValueError):
        ReadConfig(tiers={"fast": DEFAULT_TIER_TABLE["fast"]})
    assert ReadConfig().digest() == ReadConfig().digest()
    assert ReadConfig().digest() != ReadConfig(default_depth="standard").digest()


# ------------------------------------------------------------------ normalise_request


def test_defaults_filled():
    r = normalise_request(req(), "ilse")
    assert isinstance(r, ReadRequest)
    assert r.depth == "fast" and r.budget_tokens == 2048
    assert r.principal == "ilse" and r.viewer is None and r.multi_party is False


def test_default_depth_from_config_and_auto_stays_none():
    assert (
        normalise_request(req(), "ilse", ReadConfig(default_depth="standard")).depth == "standard"
    )
    assert normalise_request(req(), "ilse", ReadConfig(default_depth="auto")).depth is None


def test_viewer_bound_to_principal():
    r = normalise_request(req(viewer="ilse"), "ilse")
    assert r.viewer == r.principal == "ilse"
    with pytest.raises(ReadRequestError, match="viewer must be"):
        normalise_request(req(viewer="noor"), "ilse")
    with pytest.raises(ReadRequestError, match="authenticated principal"):
        normalise_request(req(viewer="ilse"), None)


def test_payload_principal_cannot_override():
    with pytest.raises(ReadRequestError, match="principal"):
        normalise_request(req(principal="noor"), "ilse")
    assert normalise_request(req(principal="ilse"), "ilse").principal == "ilse"


def test_multi_party_requires_viewer():
    with pytest.raises(ReadRequestError, match="requires a viewer"):
        normalise_request(req(multi_party=True), "ilse")
    with pytest.raises(ReadRequestError, match="requires a viewer"):
        normalise_request(req(scope=GROUP), "ilse")
    with pytest.raises(ReadRequestError):
        normalise_request(req(scope=GROUP), None)
    ok = normalise_request(req(scope=GROUP, viewer="ilse"), "ilse")
    assert ok.multi_party is True and ok.viewer == "ilse"


def test_unknown_fields_and_empty_query_rejected():
    with pytest.raises(ReadRequestError, match="unknown"):
        normalise_request(req(admin=True), "ilse")
    for q in ("", "   \n\t"):
        with pytest.raises(ReadRequestError, match="empty"):
            normalise_request(req(query=q), "ilse")
    with pytest.raises(ReadRequestError):
        normalise_request({"scope": SCOPE}, "ilse")
    with pytest.raises(ReadRequestError):
        normalise_request({"query": "x"}, "ilse")


def test_query_whitespace_normalised_case_kept():
    assert (
        normalise_request(req(query="  Where   does\tTomasz\nlive? "), "i").query
        == "Where does Tomasz live?"
    )


def test_depth_must_be_allowed():
    cfg = ReadConfig(allowed_depths=("fast", "standard"))
    with pytest.raises(ReadConfigError):
        normalise_request(req(depth="deep"), "ilse", cfg)
    assert normalise_request(req(depth="standard"), "ilse", cfg).depth == "standard"


def test_budget_tokens():
    assert normalise_request(req(budget_tokens=512), "i").budget_tokens == 512
    with pytest.raises(ReadRequestError):
        normalise_request(req(budget_tokens=0), "i")
    assert normalise_request(req(), "i", ReadConfig(default_budget_tokens=900)).budget_tokens == 900


def test_as_of_validation_and_utc():
    tz = timezone(timedelta(hours=2))
    r = normalise_request(
        req(
            as_of_valid=datetime(2026, 3, 1, 12, tzinfo=tz),
            reference_time=datetime(2026, 5, 1, tzinfo=UTC),
        ),
        "i",
    )
    assert r.as_of_valid == datetime(
        2026, 3, 1, 10, tzinfo=UTC
    ) and r.as_of_valid.utcoffset() == timedelta(0)
    with pytest.raises(ReadRequestError):
        normalise_request(req(as_of_valid=datetime(2026, 3, 1)), "i")  # naive
    with pytest.raises(ReadRequestError, match="later than reference_time"):
        normalise_request(
            req(
                as_of_known=datetime(2026, 6, 1, tzinfo=UTC),
                reference_time=datetime(2026, 5, 1, tzinfo=UTC),
            ),
            "i",
        )
    # no reference time: no future check; valid-time in the future is allowed
    normalise_request(req(as_of_known=datetime(2030, 1, 1, tzinfo=UTC)), "i")
    normalise_request(
        req(
            as_of_valid=datetime(2030, 1, 1, tzinfo=UTC),
            reference_time=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        "i",
    )


def test_accepts_readrequest_instance():
    r0 = ReadRequest(query="hi  there", scope=SCOPE)
    r = normalise_request(r0, "ilse")
    assert r.query == "hi there" and r.principal == "ilse"


def test_lang_normalised():
    assert normalise_request(req(lang=" DE "), "i").lang == "de"
    assert normalise_request(req(lang="  "), "i").lang is None


# ------------------------------------------------------------------ cache-key inputs


def test_prepare_read_cache_key():
    p = prepare_read(req(scope=GROUP, viewer="ilse"), "ilse", model_ids=["m2", "m1", "m1"])
    assert p.model_ids == ("m1", "m2") and p.config_hash == ReadConfig().digest()
    k = p.cache_key(store_generation=3, erasure_generation=1)
    assert isinstance(k, ReadCacheKey)
    assert k.base.viewer == "ilse" and k.base.scope.owner == "household"
    assert k.store_generation == 3 and k.erasure_generation == 1
    # equivalent raw inputs share the slot; a bump keeps the slot, changes the digest
    q = prepare_read(
        req(scope=GROUP, viewer="ilse", query="where does  bram LIVE?"),
        "ilse",
        model_ids=["m1", "m2"],
    )
    assert q.cache_key(store_generation=3, erasure_generation=1).slot() == k.slot()
    assert p.cache_key(store_generation=4, erasure_generation=1).slot() == k.slot()
    assert p.cache_key(store_generation=4, erasure_generation=1).digest() != k.digest()


def test_cache_key_differs_by_viewer_and_config():
    a = prepare_read(req(scope=GROUP, viewer="ilse"), "ilse")
    b = prepare_read(req(scope=GROUP, viewer="noor"), "noor")
    c = prepare_read(
        req(scope=GROUP, viewer="ilse"), "ilse", config=ReadConfig(default_depth="standard")
    )
    g = {"store_generation": 1, "erasure_generation": 0}
    assert a.cache_key(**g).slot() != b.cache_key(**g).slot()
    assert a.cache_key(**g).slot() != c.cache_key(**g).slot()


# ------------------------------------------------------------------ choose_tier


def nreq(depth=None):
    return normalise_request(
        req(depth=depth) if depth else req(), "ilse", ReadConfig(default_depth="auto")
    )


def test_explicit_wins():
    assert choose_tier(nreq("fast"), contract("join"), WITH_WORKER).name == "fast"
    assert choose_tier(nreq("standard"), None, NO_WORKER).name == "standard"
    assert choose_tier(nreq("deep"), contract("single"), WITH_WORKER).name == "deep"


def test_explicit_deep_without_worker():
    with pytest.raises(ReadConfigError, match="worker"):
        choose_tier(nreq("deep"), None, NO_WORKER)
    t = choose_tier(nreq("deep"), None, NO_WORKER, ReadConfig(on_missing_model="rules"))
    assert t.name == "standard" and t.degraded_from == "deep"
    assert t.max_generative_calls == 0


def test_explicit_depth_not_allowed():
    r = ReadRequest(query="q", scope=SCOPE, depth="deep")
    with pytest.raises(ReadConfigError):
        choose_tier(r, None, WITH_WORKER, ReadConfig(allowed_depths=("fast", "standard")))


@pytest.mark.parametrize(
    ("shape", "view", "worker", "allowed", "want"),
    [
        ("single", "current", True, ("fast", "standard", "deep"), "fast"),
        ("latest", "current", True, ("fast", "standard", "deep"), "fast"),
        ("join", "current", True, ("fast", "standard", "deep"), "deep"),
        ("list_count", "current", True, ("fast", "standard", "deep"), "deep"),
        ("history", "current", True, ("fast", "standard", "deep"), "deep"),
        ("single", "history", True, ("fast", "standard", "deep"), "deep"),
        ("as_of", "as_of", True, ("fast", "standard", "deep"), "fast"),
        ("join", "current", False, ("fast", "standard", "deep"), "standard"),
        ("join", "current", True, ("fast", "standard"), "standard"),
        ("join", "current", True, ("fast",), "fast"),
        ("join", "current", True, ("deep",), "deep"),
        ("join", "current", False, ("fast", "deep"), "fast"),
    ],
)
def test_auto_matrix(shape, view, worker, allowed, want):
    cfg = ReadConfig(default_depth="auto", allowed_depths=allowed)
    layers = WITH_WORKER if worker else NO_WORKER
    assert choose_tier(nreq(), contract(shape, view), layers, cfg).name == want


def test_auto_without_contract_and_with_unknown_shape():
    cfg = ReadConfig(default_depth="auto")
    assert choose_tier(nreq(), None, WITH_WORKER, cfg).name == "fast"
    assert choose_tier(nreq(), TypedContract(), WITH_WORKER, cfg).name == "fast"


def test_configured_default_used_when_depth_unset():
    r = ReadRequest(query="q", scope=SCOPE)
    cfg = ReadConfig(default_depth="standard")
    assert choose_tier(r, None, NO_WORKER, cfg).name == "standard"
    cfg = ReadConfig(default_depth="deep")
    assert choose_tier(r, None, NO_WORKER, cfg).degraded_from == "deep"
    assert choose_tier(r, None, WITH_WORKER, cfg).name == "deep"


def test_auto_never_exceeds_caps():
    cfg = ReadConfig(default_depth="auto")
    for shape in ("single", "join", "list_count", "history", "unknown"):
        for layers in (WITH_WORKER, NO_WORKER):
            t = choose_tier(nreq(), contract(shape), layers, cfg)
            assert t.max_generative_calls <= (2 if layers is WITH_WORKER else 0)


def test_custom_tier_table_respected():
    tiers = dict(DEFAULT_TIER_TABLE)
    tiers["deep"] = tiers["deep"].model_copy(update={"max_generative_calls": 1})
    assert (
        choose_tier(nreq("deep"), None, WITH_WORKER, ReadConfig(tiers=tiers)).max_generative_calls
        == 1
    )


# ------------------------------------------------------------------ evidence unit


def env() -> AssertionEnvelope:
    return AssertionEnvelope.model_validate_json(FIX.read_text(encoding="utf-8"))


def bundle(rid="rec-1", tokens=0) -> EvidenceBundle:
    e = env()
    return EvidenceBundle(
        record_id=rid,
        assertion=e,
        support=[x for x in e.evidence if x.role == "support"],
        binding=[x for x in e.evidence if x.role == "binding"],
        tokens=tokens,
    )


def test_unit_structure_and_tokens():
    b = bundle()
    u = unit_of(b)
    assert u.record_id == "rec-1" and u.text == b.assertion.text
    assert u.support == len(b.support) >= 1 and u.binding == len(b.binding) >= 1
    assert u.tokens == unit_tokens(b) > 0
    assert unit_tokens(bundle(tokens=77)) == 77  # a stated count wins over the estimate
    assert unit_tokens(bundle(tokens=77), counter=len) != 77  # an exact counter overrides it
    assert unit_tokens(b, counter=lambda s: 1) >= 1


def test_count_units_by_record():
    assert count_units([]) == 0
    assert count_units([bundle("a"), bundle("b"), bundle("a")]) == 2


def test_fit_whole_units():
    bs = [bundle("a", 40), bundle("b", 70), bundle("c", 30), bundle("a", 5)]
    kept, dropped = fit_units(bs, 80)
    assert [b.record_id for b in kept] == ["a", "c"]  # b skipped, later smaller c fits
    assert [b.record_id for b in dropped] == ["b", "a"]  # repeat of a dropped
    kept, dropped = fit_units(bs, 80, stop_at_first_overflow=True)
    assert [b.record_id for b in kept] == ["a"] and len(dropped) == 3
    assert fit_units(bs, 0) == ([], bs)
    kept, _ = fit_units(bs, 10_000)
    assert [b.record_id for b in kept] == ["a", "b", "c"]


# ------------------------------------------------------------------ determinism


def test_determinism():
    a = prepare_read(req(scope=GROUP, viewer="ilse"), "ilse", model_ids=["x"])
    b = prepare_read(req(scope=GROUP, viewer="ilse"), "ilse", model_ids=["x"])
    assert a == b
    assert a.request.model_dump_json() == b.request.model_dump_json()
    bs = [bundle("a", 10), bundle("b", 20)]
    assert fit_units(bs, 25) == fit_units(bs, 25)
    c = contract("join")
    assert choose_tier(nreq(), c, WITH_WORKER, ReadConfig(default_depth="auto")) == choose_tier(
        nreq(), c, WITH_WORKER, ReadConfig(default_depth="auto")
    )
