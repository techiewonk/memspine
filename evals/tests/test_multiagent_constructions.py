"""Paper A section 6.1: the Prop. 3 and Prop. 4(c) constructions on today's engine.

These pin the *pre-MTI* behaviour. When G1/G2/G5 land, flip the expectations
(bounded depth, zero promotions) rather than deleting the tests.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("memspine")

from memspine_evals.multiagent.constructions import (
    laundering_chain,
    llm_propagation,
    predicted_radius,
    scripted_propagation,
    sybil_promotion,
)


def test_laundering_resets_trust_and_reaches_the_whole_chain() -> None:
    report = asyncio.run(laundering_chain(n_agents=5, theta=0.2))
    assert not report.seed_quarantined, "declarative poison should pass the firewall"
    assert report.seed_trust <= 0.3  # external channel is capped at write
    # Prop. 3: every hop admits, and every re-deposit is back at assistant trust.
    assert report.depth == 4 and report.reach == 1.0
    assert all(row.admitted for row in report.hops)
    assert all(row.redeposit_trust == 0.5 for row in report.hops)
    assert all(row.view_trust == pytest.approx(0.3) for row in report.hops)


def test_mti_stops_the_same_laundering_at_the_origin() -> None:
    integrity = {"enabled": True, "kappa": 0.5, "admission_threshold": 0.2}
    report = asyncio.run(laundering_chain(n_agents=5, theta=0.2, integrity=integrity))
    # 0.3 * 0.5 = 0.15 < theta: nothing crosses the first grant (Prop. 2, L* = 0).
    assert report.depth == 0 and report.reach == 0.0
    assert not report.hops[0].admitted


def test_conservative_parents_are_necessary() -> None:
    """MG-10 / H-10b: drop A2 (declare only the most-trusted context record) and
    the radius disappears, even with the invariant on."""
    common = {
        "topology": "chain",
        "n_agents": 5,
        "kappa": 0.5,
        "theta": 0.2,
        "seed_role": "assistant",
        "seed_channel": "internal",
        "benign_decoys": True,
    }
    sound = asyncio.run(scripted_propagation(**common, parent_mode="all"))
    broken = asyncio.run(scripted_propagation(**common, parent_mode="max_trust"))
    assert sound.depth == sound.predicted_radius == 1  # 0.5*0.5 >= 0.2 > 0.5*0.25
    assert sound.mti_violations == 0
    assert broken.depth == 4  # laundering via a benign high-trust parent


@pytest.mark.parametrize("same_session", [True, False])
def test_key_only_notes_no_longer_release_the_payload(same_session: bool) -> None:
    # #3 (value-matched corroboration): innocuous notes on the payload's key state
    # a different value, so they contradict it rather than corroborate it.
    report = asyncio.run(sybil_promotion(same_session=same_session, memory_type="episodic"))
    assert report.held_quarantined_at_write
    assert not report.promoted and report.final_status == "quarantined"


@pytest.mark.parametrize("same_session", [True, False])
def test_single_principal_reactivates_its_own_quarantined_payload(same_session: bool) -> None:
    report = asyncio.run(
        sybil_promotion(same_session=same_session, memory_type="episodic", restate_payload=True)
    )
    assert report.held_quarantined_at_write
    assert report.distinct_principals == 1
    # Prop. 4(c): still holds when the corroborators restate the payload; must be
    # False after G5 (principal-bound corroboration).
    assert report.promoted and report.final_status == "activated"


def test_semantic_key_only_notes_no_longer_release_the_payload() -> None:
    report = asyncio.run(sybil_promotion(same_session=True, memory_type="semantic"))
    assert report.held_quarantined_at_write
    assert not report.promoted


def test_b0_implicit_parents_restore_the_radius_when_writers_declare_nothing() -> None:
    """Writers omit derived_from entirely. Without B0 that launders (no radius);
    with B0 the engine's own read ledger restores the bound."""
    common = {
        "topology": "chain",
        "n_agents": 5,
        "kappa": 0.5,
        "theta": 0.2,
        "seed_role": "assistant",
        "seed_channel": "internal",
        "parent_mode": "none",
    }
    laundered = asyncio.run(scripted_propagation(**common))
    enforced = asyncio.run(
        scripted_propagation(**common, integrity_extra={"implicit_parents": "turn"})
    )
    assert laundered.depth == 4  # no declared parents, no bound
    assert enforced.depth == enforced.predicted_radius == 1


# ── R4-1 / N7: engine enforcement (no declared parents, no harness filter) ────


_ENGINE_CHAIN = {
    "topology": "chain",
    "n_agents": 5,
    "seed_role": "assistant",
    "seed_channel": "internal",
    "enforcement": "engine",
}


@pytest.mark.parametrize(("kappa", "theta"), [(0.5, 0.2), (0.8, 0.3)])
def test_engine_enforcement_holds_the_radius_without_declared_parents(
    kappa: float, theta: float
) -> None:
    """The writer declares nothing and the harness filters nothing: the engine's
    session read ledger (implicit parents) and its own admission gate alone
    keep the chain depth at the predicted radius."""
    report = asyncio.run(scripted_propagation(**_ENGINE_CHAIN, kappa=kappa, theta=theta))
    assert report.predicted_radius is not None
    assert report.depth == report.predicted_radius
    assert report.mti_violations == 0


def test_engine_enforcement_matches_harness_mode_on_a_chain() -> None:
    common = {**_ENGINE_CHAIN, "kappa": 0.5, "theta": 0.2}
    engine = asyncio.run(scripted_propagation(**common))
    harness = asyncio.run(scripted_propagation(**{**common, "enforcement": "harness"}))
    assert (engine.depth, engine.exposed) == (harness.depth, harness.exposed)


def test_engine_mode_without_implicit_parents_launders() -> None:
    """Control: the same agents with the engine's read ledger switched off reach
    the whole chain, so the bound above is the engine's doing."""
    report = asyncio.run(
        scripted_propagation(
            **_ENGINE_CHAIN,
            kappa=0.5,
            theta=0.2,
            integrity_extra={"implicit_parents": "off"},
        )
    )
    assert report.depth == 4


def test_engine_enforcement_requires_integrity() -> None:
    with pytest.raises(ValueError, match="integrity_on"):
        asyncio.run(scripted_propagation(**_ENGINE_CHAIN, integrity_on=False))
    with pytest.raises(ValueError, match="enforcement"):
        asyncio.run(scripted_propagation("chain", enforcement="bogus"))


# ── R4-1 for the LLM study: llm_propagation(enforcement="engine") ─────────────


class _Answer:
    def __init__(self, text: str) -> None:
        self.text = text


class _EchoReader:
    """Stub LLM (no network): answers by echoing the team notes it was shown, so
    an agent restates the poison exactly when the poison is in its context."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> _Answer:
        self.prompts.append(prompt)
        notes = prompt.split("Team notes:\n", 1)[1].split("\n\nQuestion:", 1)[0]
        return _Answer(notes)


_LLM_CHAIN = {
    "topology": "chain",
    "n_agents": 5,
    "kappa": 0.5,
    "theta": 0.2,
    "seed_role": "assistant",
    "seed_channel": "internal",
    "rounds": 2,
}


def test_llm_engine_enforcement_holds_the_radius_without_declared_parents() -> None:
    report = asyncio.run(llm_propagation(reader=_EchoReader(), **_LLM_CHAIN, enforcement="engine"))
    radius = predicted_radius(report.seed_trust, report.kappa, report.theta)
    assert radius == 1  # 0.5*0.5 >= 0.2 > 0.5*0.25
    assert report.depth == radius
    assert report.exposed == [0, 1]


def test_llm_harness_mode_is_unchanged() -> None:
    """Default and explicit ``harness`` give the same run, and it matches the
    declared-parents bound that the published results rest on."""
    default = asyncio.run(llm_propagation(reader=_EchoReader(), **_LLM_CHAIN))
    explicit = asyncio.run(
        llm_propagation(reader=_EchoReader(), **_LLM_CHAIN, enforcement="harness")
    )
    assert default.as_dict() == explicit.as_dict()
    assert default.depth == 1 and default.exposed == [0, 1]


def test_llm_engine_mode_without_implicit_parents_launders() -> None:
    """Control: switch the engine's read ledger off and the same echo agents
    carry the poison down the whole chain."""
    report = asyncio.run(
        llm_propagation(
            reader=_EchoReader(),
            **_LLM_CHAIN,
            enforcement="engine",
            integrity_extra={"implicit_parents": "off"},
        )
    )
    assert report.depth == 4
    assert report.reach == 1.0


def test_llm_engine_enforcement_requires_integrity() -> None:
    with pytest.raises(ValueError, match="integrity_on"):
        asyncio.run(
            llm_propagation(
                reader=_EchoReader(), **_LLM_CHAIN, enforcement="engine", integrity_on=False
            )
        )
    with pytest.raises(ValueError, match="enforcement"):
        asyncio.run(llm_propagation("chain", _EchoReader(), enforcement="bogus"))
