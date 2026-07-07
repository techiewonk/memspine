"""Assembly policy (M12 + E2): abstention, budget, MMR, cache-aware placement."""

from __future__ import annotations

from memspine.core.policies.assembly import AssemblyPolicy
from memspine.core.records import MemoryRecord, SourceInfo


def rec(content: str, memory_type: str = "semantic", persona: bool = False) -> MemoryRecord:
    source = SourceInfo(channel="persona") if persona else SourceInfo()
    return MemoryRecord(namespace="n", memory_type=memory_type, content=content, source=source)


def test_abstains_below_theta() -> None:
    policy = AssemblyPolicy.bind({"theta_abstain": 0.5})
    context = policy.assemble([(rec("weak match"), 0.3)])
    assert context.abstained and context.records == []

    assert policy.assemble([]).abstained


def test_token_budget_caps_selection_but_never_empty() -> None:
    policy = AssemblyPolicy.bind({"theta_abstain": 0.0})
    big = rec("word " * 400)  # ~500 tokens
    small = rec("tiny")
    context = policy.assemble([(big, 0.9), (small, 0.8)], budget_tokens=200)
    assert len(context.records) == 1  # big alone admitted (never-empty rule)
    assert not context.abstained


def test_mmr_penalizes_near_duplicates() -> None:
    policy = AssemblyPolicy.bind({"mmr_lambda": 0.5, "theta_abstain": 0.0})
    a = rec("the quick brown fox jumps over the lazy dog")
    dup = rec("the quick brown fox jumps over the lazy dogs")
    fresh = rec("completely unrelated topic about databases")
    # Budget fits exactly two records: after picking `a`, MMR must spend the
    # second slot on the diverse record, not the higher-scored near-duplicate.
    context = policy.assemble([(a, 0.9), (dup, 0.85), (fresh, 0.6)], budget_tokens=24)
    ids = {record.record_id for record in context.records}
    assert ids == {a.record_id, fresh.record_id}


def test_e2_placement_persona_then_stable_then_volatile() -> None:
    policy = AssemblyPolicy.bind({"theta_abstain": 0.0})
    persona = rec("I am a helpful assistant", memory_type="working", persona=True)
    skill = rec("how to deploy", memory_type="procedural")
    fact = rec("sky is blue", memory_type="semantic")
    episode = rec("yesterday we talked", memory_type="episodic")
    turn = rec("current scratch", memory_type="working")

    context = policy.assemble(
        [(episode, 0.95), (turn, 0.9), (fact, 0.5), (skill, 0.4), (persona, 0.3)],
        budget_tokens=10_000,
    )
    order = [record.record_id for record in context.records]
    assert order == [
        persona.record_id,
        skill.record_id,
        fact.record_id,
        episode.record_id,
        turn.record_id,
    ]
    # Cache boundary sits after the stable prefix (persona+skills+facts, E2).
    assert context.boundary_index == 3
