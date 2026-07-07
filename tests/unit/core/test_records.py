"""Universal record model: Phase-0 field contract (M1, E1, D-27, D-42)."""

from __future__ import annotations

from memspine.core.records import MemoryRecord, PiiTier, RecordStatus


def test_record_defaults_cover_phase0_contract() -> None:
    r = MemoryRecord(namespace="agent/alice", memory_type="semantic", content="sky is blue")
    # identity + fingerprint (D-37)
    assert r.record_id and r.content_fingerprint
    # lifecycle + provenance (D-42)
    assert r.status is RecordStatus.ACTIVATED
    assert r.version == 1 and r.history == [] and r.evolve_to is None
    assert r.source.role == "system"
    # firewall columns present from day one (E1)
    assert r.trust == 0.5 and r.quarantined is False and r.instruction_flag is False
    # governance (M7)
    assert r.pii_tier is PiiTier.NONE and r.consent_tags == []
    # dedup fields reserved (D-27)
    assert r.simhash is None and r.minhash_sig is None
    # bi-temporal (M4)
    assert r.valid_to is None and r.superseded_at is None


def test_same_content_same_fingerprint() -> None:
    a = MemoryRecord(namespace="n", memory_type="semantic", content="x")
    b = MemoryRecord(namespace="n", memory_type="episodic", content="x")
    assert a.content_fingerprint == b.content_fingerprint
    assert a.record_id != b.record_id
