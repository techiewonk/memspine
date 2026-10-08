"""E1/A5 interaction: reinforcement-on-read is bounded by the firewall's trust verdict.

Background — the defect this file pins down. ``Engine.search`` emits one RETRIEVE event
over *every* record it scored, and ``RecordProjector._apply_retrieve`` raised ``utility``
unconditionally toward ``RETRIEVE_UTILITY_MAX = 1.0``. There is no decrement site anywhere
in the codebase, so the lift is permanent. Because the composite score is

    (recency + relevance + importance) / 3  +  0.5 * utility

a saturated record enjoys a +0.5 bonus while the *entire dynamic range* of the relevance
term is 1/3. A record at ``utility = 1.0`` therefore outranks any ``importance = 0``
competitor of equal recency **at any relevance, including relevance 0 against a
perfect-relevance competitor** — and since the pump also refreshes ``last_accessed_at``,
its recency term stays pinned at 1.0 while honest content decays.

That made reinforcement a ranking-level privilege escalation: REST writes land at
``TRUST_RETRIEVED_CAP = 0.3``, above ``QUARANTINE_TRUST_THRESHOLD = 0.25``, so untrusted
external content is ACTIVATED and fully pumpable. ``trust`` was load-bearing at the write
door and in conflict resolution but appeared nowhere in scoring, assembly or decay: the
firewall's graded verdict collapsed to a binary at the read door.

The fix bounds the lift by the record's own trust. It is deterministic and replay-safe —
``trust`` is immutable post-write (see ``_DELTA_MUTABLE``, which excludes it by design), so
a rebuild reproduces the same ceiling.
"""

from __future__ import annotations

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.policies.scoring import ScoringPolicy
from memspine.core.records import SourceInfo


def _engine() -> Engine:
    return Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
    )


async def test_low_trust_record_cannot_pump_past_its_trust() -> None:
    """A record written on an untrusted channel saturates at its trust, not at 1.0."""
    eng = _engine()
    await eng.start()
    try:
        low = constants.TRUST_RETRIEVED_CAP  # 0.3 — the cap on every external channel
        # Trust is firewall-assigned from (role x channel), never caller-supplied.
        # "rest" is in _EXTERNAL_CHANNELS, so this is exactly what an unauthenticated
        # REST write gets: capped at 0.3, which is still above the 0.25 quarantine
        # threshold, so the record lands ACTIVATED and fully pumpable.
        rec = await eng.write(
            "the sky is blue today",
            source=SourceInfo(role="user", channel="rest"),
        )
        storage = eng._storage
        assert storage is not None

        stored = await storage.get_record(rec.record_id)
        assert stored is not None
        assert stored.trust == pytest.approx(low)
        assert not stored.quarantined, "0.3 > QUARANTINE_TRUST_THRESHOLD, so it is ACTIVATED"

        # The pump: far more retrievals than the 10 needed to reach the old cap.
        for _ in range(30):
            await eng.search("the sky is blue today")

        pumped = await storage.get_record(rec.record_id)
        assert pumped is not None
        assert pumped.scoring.utility == pytest.approx(low), (
            "utility must saturate at the record's trust, not at RETRIEVE_UTILITY_MAX"
        )
        assert pumped.scoring.utility < constants.RETRIEVE_UTILITY_MAX
    finally:
        await eng.stop()


async def test_trusted_record_still_reinforces_normally() -> None:
    """The cap must not break legitimate reinforcement for in-process writes."""
    eng = _engine()
    await eng.start()
    try:
        # A default in-process write is role="user" on the "internal" channel, so the
        # firewall assigns the *role* trust (0.7), not TRUST_DEFAULT.
        rec = await eng.write("the sky is blue today")
        storage = eng._storage
        assert storage is not None

        first = await storage.get_record(rec.record_id)
        assert first is not None
        trusted = first.trust
        assert trusted > constants.TRUST_RETRIEVED_CAP, (
            "an internal write must outrank an external one"
        )

        await eng.search("the sky is blue today")
        after1 = await storage.get_record(rec.record_id)
        assert after1 is not None
        assert after1.scoring.utility == pytest.approx(constants.RETRIEVE_UTILITY_STEP), (
            "the first bump is unchanged — the cap only binds at saturation"
        )

        for _ in range(30):
            await eng.search("the sky is blue today")
        capped = await storage.get_record(rec.record_id)
        assert capped is not None
        assert capped.scoring.utility == pytest.approx(min(constants.RETRIEVE_UTILITY_MAX, trusted))
    finally:
        await eng.stop()


def test_trust_cap_restores_relevance_dominance_for_untrusted_content() -> None:
    """The arithmetic the cap exists to fix, asserted directly on the scoring policy.

    Without the cap an untrusted record at ``utility = 1.0`` beats a perfectly relevant
    honest record: 0.5 utility bonus against a relevance term whose whole range is 1/3.
    With the cap its ceiling is 0.3, worth +0.15 — below that range — so relevance decides.
    """
    from datetime import UTC, datetime

    from memspine.core.records import MemoryRecord

    now = datetime.now(UTC)
    policy = ScoringPolicy.bind({})

    def _rec(utility: float) -> MemoryRecord:
        r = MemoryRecord(namespace="n", memory_type="semantic", content="c")
        return r.model_copy(
            update={
                "recorded_at": now,
                "scoring": r.scoring.model_copy(
                    update={"utility": utility, "importance": 0.0, "last_accessed_at": now}
                ),
            }
        )

    # Attacker: irrelevant (relevance 0) but pumped. Honest: perfectly relevant, unpumped.
    saturated_uncapped = policy.composite_score(_rec(constants.RETRIEVE_UTILITY_MAX), 0.0, now=now)
    honest = policy.composite_score(_rec(0.0), 1.0, now=now)
    assert saturated_uncapped > honest, (
        "documents the defect: an irrelevant record pumped to 1.0 outranks a perfectly "
        "relevant one — this is why the cap is needed"
    )

    # With the cap, an untrusted record cannot exceed TRUST_RETRIEVED_CAP.
    saturated_capped = policy.composite_score(_rec(constants.TRUST_RETRIEVED_CAP), 0.0, now=now)
    assert saturated_capped < honest, (
        "with utility bounded by trust=0.3 the bonus (0.15) is below the relevance "
        "range (1/3), so relevance decides again"
    )
