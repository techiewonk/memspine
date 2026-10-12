"""LIB-43: deep tier sufficiency check and one rewrite (all callables stubbed)."""

from __future__ import annotations

import json

import pytest

from memspine.read.deep import (
    Candidate,
    DeepBudget,
    deep_read,
    parse_rewrite,
    parse_sufficiency,
)
from memspine.read.obligations import (
    EvidenceBundle,
    EvidenceFact,
    QueryDescription,
    check,
    derive_obligations,
)
from memspine.read.router_rules import RewritePlan

CONTRACT = QueryDescription(kind="join", entities=("Ilse",), relation="sister", attribute="city")


def _report(contract: QueryDescription = CONTRACT, bundles=()):
    return check(derive_obligations(contract), bundles)


def _met_report():
    facts = (
        EvidenceFact(fact_id="f1", entity="Ilse", attribute="sister", related_entity="Noor"),
        EvidenceFact(fact_id="f2", entity="Noor", attribute="city", value="Delft"),
    )
    r = _report(bundles=[EvidenceBundle(bundle_id="b", facts=facts)])
    assert r.verdict == "answerable"
    return r


FIRST = [Candidate(item_id="a", score=0.9), Candidate(item_id="b", score=0.5)]


class Stubs:
    def __init__(self, suff=None, rew=None, ret=None):
        self.suff = suff if suff is not None else {"sufficient": False, "missing": []}
        self.rew = rew if rew is not None else ["Noor city of residence"]
        self.ret = ret if ret is not None else [Candidate(item_id="c", score=0.7)]
        self.log: list[str] = []
        self.queries = None

    def sufficiency_fn(self, req):
        self.log.append("suff")
        if isinstance(self.suff, Exception):
            raise self.suff
        return self.suff

    def rewrite_fn(self, req):
        self.log.append("rew")
        if isinstance(self.rew, Exception):
            raise self.rew
        return self.rew

    def retrieve_fn(self, queries):
        self.log.append("ret")
        self.queries = queries
        if isinstance(self.ret, Exception):
            raise self.ret
        return self.ret

    def run(self, report=None, budget=None, first=FIRST, contract=CONTRACT):
        return deep_read(
            first,
            contract,
            report or _report(),
            sufficiency_fn=self.sufficiency_fn,
            rewrite_fn=self.rewrite_fn,
            retrieve_fn=self.retrieve_fn,
            budget=budget or DeepBudget(),
        )


def test_met_means_zero_calls():
    s = Stubs()
    r = s.run(report=_met_report())
    assert s.log == []
    assert r.action == "answer" and r.reason == "rules_met"
    assert r.model_calls == 0 and r.retrieve_calls == 0
    assert [c.item_id for c in r.items] == ["a", "b"]


def test_unmet_one_sufficiency_one_rewrite_one_retrieve():
    s = Stubs()
    r = s.run()
    assert s.log == ["suff", "rew", "ret"]
    assert r.model_calls == 2 and r.retrieve_calls == 1
    assert all(c.cost_class == "model_generative" for c in r.calls[:2])
    assert r.calls[2].cost_class == "retrieval"
    assert r.new_item_ids == ("c",)
    assert [c.item_id for c in r.items] == ["a", "b", "c"]
    assert r.action == "answer" and r.reason == "extended"
    assert r.sufficient is False
    origins = [q.origin for q in s.queries]
    assert origins[0] == "follow_up" and origins[-1] == "rewrite"
    assert s.queries[-1].text == "Noor city of residence"


def test_follow_ups_precede_rewrite_and_come_from_lib15():
    rep = _report()
    s = Stubs()
    s.run(report=rep)
    fu = [q for q in s.queries if q.origin == "follow_up"]
    assert {(q.entity, q.attribute, q.reason) for q in fu} <= {
        (f.entity, f.attribute, f.reason) for f in rep.follow_ups
    }
    assert fu


def test_judge_sufficient_stops():
    s = Stubs(suff={"sufficient": True, "missing": []})
    r = s.run()
    assert s.log == ["suff"]
    assert r.reason == "judge_sufficient" and r.action == "answer_with_caveat"
    assert r.sufficient is True and r.model_calls == 1


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "null",
        42,
        [],
        {"sufficient": "yes", "missing": []},
        {"sufficient": 1, "missing": []},
        {"sufficient": False, "extra": 1},
        {"sufficient": True, "missing": [{"kind": "missing_fact", "entity": "Ilse"}]},
        {"sufficient": False, "missing": [{"kind": "bogus"}]},
        {"sufficient": False, "missing": [{"kind": "missing_fact", "entity": "Mallory"}]},
        {"sufficient": False, "missing": [{"kind": "missing_fact", "item_id": "x"}]},
        ValueError("boom"),
    ],
)
def test_invalid_sufficiency_falls_back_to_rule_holes(bad):
    s = Stubs(suff=bad)
    r = s.run()
    assert s.log == ["suff", "rew", "ret"]
    assert r.calls[0].valid is False
    assert r.sufficient is None
    assert r.holes == tuple(h for res in _report().results for h in res.holes)


def test_valid_model_holes_are_used_and_default_entity_is_contract_entity():
    judge = {"sufficient": False, "missing": [{"kind": "missing_attribute", "attribute": "street"}]}
    s = Stubs(suff=judge)
    r = s.run()
    assert len(r.holes) == 1
    assert r.holes[0].entity == "Ilse" and r.holes[0].attribute == "street"
    fu = [q for q in s.queries if q.origin == "follow_up"]
    assert fu[0].attribute == "street" and fu[0].time_window is None


def test_json_string_and_fenced_outputs_accepted():
    j = parse_sufficiency('{"sufficient": false, "missing": []}', allowed_entities=["Ilse"])
    assert j is not None and j.sufficient is False
    j = parse_sufficiency('```json\n{"sufficient": true}\n```', allowed_entities=[])
    assert j is not None and j.sufficient is True
    assert parse_rewrite(json.dumps({"queries": ["a b", "a  b"]})) == ("a b",)


@pytest.mark.parametrize(
    "bad",
    [[], [""], ["x" * 301], ["a", "b", "c", "d"], {"queries": []}, "nope", 3, [1], ["a\x00b"]],
)
def test_invalid_rewrite_dropped_follow_ups_still_run(bad):
    s = Stubs(rew=bad)
    r = s.run()
    assert s.log == ["suff", "rew", "ret"]
    assert r.calls[1].valid is False
    assert all(q.origin == "follow_up" for q in s.queries)


def test_rewrite_exception_is_contained():
    s = Stubs(rew=RuntimeError("x"))
    r = s.run()
    assert r.calls[1].valid is False and r.retrieve_calls == 1


def test_budget_zero_no_calls_caveat():
    s = Stubs()
    r = s.run(budget=DeepBudget(max_model_calls=0))
    assert s.log == []
    assert r.action == "answer_with_caveat" and r.reason == "budget_exhausted"
    assert r.calls == () and [c.item_id for c in r.items] == ["a", "b"]
    assert any("budget" in c for c in r.caveats)


def test_plan_without_permission_gives_zero_calls():
    plan = RewritePlan(
        sufficiency_check_allowed=False, rewrite_allowed=False, max_rewrites=0, reason="x"
    )
    s = Stubs()
    r = s.run(budget=DeepBudget.from_plan(plan))
    assert s.log == [] and r.reason == "budget_exhausted"


def test_plan_allowed_matches_default():
    plan = RewritePlan(
        sufficiency_check_allowed=True, rewrite_allowed=True, max_rewrites=3, reason="x"
    )
    b = DeepBudget.from_plan(plan)
    assert b.calls_available == 2 and b.allow_rewrite


def test_one_call_budget_skips_rewrite():
    s = Stubs()
    r = s.run(budget=DeepBudget(max_model_calls=1))
    assert s.log == ["suff", "ret"]
    assert r.model_calls == 1


def test_budget_cannot_exceed_two_model_calls():
    s = Stubs()
    r = s.run(budget=DeepBudget(max_model_calls=50))
    assert r.model_calls == 2 and r.retrieve_calls == 1


def test_rewrite_disallowed_still_one_retrieve():
    s = Stubs()
    r = s.run(budget=DeepBudget(allow_rewrite=False))
    assert s.log == ["suff", "ret"] and r.retrieve_calls == 1


def test_retrieve_failure_keeps_first_pass():
    s = Stubs(ret=RuntimeError("down"))
    r = s.run()
    assert r.reason == "retrieve_failed" and r.action == "answer_with_caveat"
    assert [c.item_id for c in r.items] == ["a", "b"]


def test_dedupe_and_rescore_on_merge():
    ret = [
        Candidate(item_id="a", score=0.95),
        Candidate(item_id="c", score=0.2),
        Candidate(item_id="c", score=0.8),
        {"item_id": "d"},
        {"nonsense": True},
    ]
    s = Stubs(ret=ret)
    r = s.run(first=[{"item_id": "a", "score": 0.9}, {"item_id": "a", "score": 0.1}, FIRST[1]])
    assert [c.item_id for c in r.items] == ["a", "b", "c", "d"]
    assert r.items[0].score == 0.95
    assert next(c for c in r.items if c.item_id == "c").score == 0.8
    assert r.new_item_ids == ("c", "d")


def test_nothing_new_is_caveat():
    s = Stubs(ret=[Candidate(item_id="a")])
    r = s.run()
    assert r.reason == "extended_nothing_new" and r.action == "answer_with_caveat"
    assert r.new_item_ids == ()


def test_max_new_items_and_max_queries_bounded():
    s = Stubs(ret=[Candidate(item_id=f"n{i}") for i in range(10)])
    r = s.run(budget=DeepBudget(max_new_items=3, max_queries=1))
    assert len(r.new_item_ids) == 3 and len(s.queries) == 1


def test_no_queries_no_retrieve():
    s = Stubs()
    rep = _report().model_copy(update={"results": (), "follow_ups": ()})
    r = s.run(report=rep, budget=DeepBudget(allow_rewrite=False))
    assert s.log == ["suff"] and r.reason == "no_queries"


def test_never_loops_one_of_each_call_at_most():
    s = Stubs()
    s.run()
    assert s.log.count("suff") == 1 and s.log.count("rew") == 1 and s.log.count("ret") == 1


def test_judge_sees_bounded_items_and_token_cap():
    seen = {}

    def suff(req):
        seen["n"] = len(req.items)
        seen["cap"] = req.max_output_tokens
        return {"sufficient": True}

    first = [Candidate(item_id=f"i{i}") for i in range(40)]
    deep_read(
        first,
        CONTRACT,
        _report(),
        sufficiency_fn=suff,
        rewrite_fn=lambda r: [],
        retrieve_fn=lambda q: [],
        budget=DeepBudget(max_items_shown=5, max_output_tokens=64),
    )
    assert seen == {"n": 5, "cap": 64}


def test_trace_records_decisions():
    r = Stubs().run()
    steps = [t.step for t in r.trace]
    assert steps[0] == "rules" and "sufficiency" in steps and "rewrite" in steps
    assert steps[-1] == "result"


def test_deterministic():
    a = Stubs().run()
    b = Stubs().run()
    assert a == b
    assert a.model_dump_json() == b.model_dump_json()


def test_caveat_verdict_still_gets_checked_and_keeps_caveats():
    contract = QueryDescription(kind="list", entities=("Bram",), attribute="hobbies")
    facts = (EvidenceFact(fact_id="h1", entity="Bram", attribute="hobbies", value="chess"),)
    rep = check(derive_obligations(contract), [EvidenceBundle(bundle_id="b", facts=facts)])
    assert rep.verdict == "answer_with_caveat"
    s = Stubs(suff={"sufficient": True})
    r = s.run(report=rep, contract=contract)
    assert r.caveats == rep.caveats and r.caveats
