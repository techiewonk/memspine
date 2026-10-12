"""Evidence register for default-on config flags (X-02, decision G-22).

Rule (G-22, plan rule 10): a flag whose schema default is ON must carry evidence for
that default: a commit, a run id or an ADR. ``tests/unit/test_flag_evidence.py``
discovers every boolean field of ``memspine/config/schema.py`` whose default is
``True`` and fails when

* a default-on flag has no entry here,
* an entry names a field that does not exist or whose default is no longer ON
  (retire the entry when the flag is retired or flipped), or
* an entry says ``UNKNOWN (pre-G-22)`` for a flag that is not in
  ``PRE_G22_UNKNOWN`` (the frozen list below; it may only shrink).

Keys are ``"<ConfigClass>.<field>"``. ``evidence`` is free text naming at least one
commit hash, run id or ADR id (or the literal UNKNOWN marker).
"""

from __future__ import annotations

UNKNOWN = "UNKNOWN (pre-G-22)"

# Frozen on 2026-10-13. Never add to this set: a new default-on flag needs real evidence.
# Remove an entry here in the same change that replaces its UNKNOWN with evidence.
PRE_G22_UNKNOWN: frozenset[str] = frozenset(
    {
        "ReadConfig.perspective_leg",
        "ReadConfig.record_access",
        "MemspineConfig.strict_services",
    }
)

FLAG_EVIDENCE: dict[str, dict[str, str]] = {
    # --- read ---
    "ReadConfig.hybrid": {
        "evidence": "commit 011ef4a (v0.2 A3 default-on flip); ADR-019",
    },
    "ReadConfig.perspective_leg": {
        "evidence": UNKNOWN,
        "note": "commit fa81ad8 (I75b) introduced the flag at the then-current behaviour; "
        "no paired verdict found for the default",
    },
    "ReadConfig.record_access": {
        "evidence": UNKNOWN,
        "note": "commit 9a7c7c2 added the switch; the ADR-032/033 templates set it false "
        "for eval; the True default has no recorded verdict",
    },
    # --- write ---
    "WriteConfig.window_async": {
        "evidence": "commit 0eb2fd7 (W1 adaptive-window write); ADR-066",
        "note": "timing only: result identical to window_async=false (schema comment)",
    },
    "WriteConfig.window_cache": {
        "evidence": "commit 0eb2fd7 (W1 adaptive-window write); ADR-066",
        "note": "cache only: a re-run on the same history is free",
    },
    # --- firewall (memory firewall, N1) ---
    "FirewallConfig.enabled": {
        "evidence": "commit 8d45cfe (B8 firewall on/off switch); ADR-013",
        "note": "True reproduces the pre-B8 firewall",
    },
    "FirewallSignalsConfig.instruction": {
        "evidence": "commit 8882208 (W2/N20/N22 extended instruction patterns); ADR-058",
    },
    "FirewallSignalsConfig.anomaly": {
        "evidence": "commit 8882208 (W2/N20/N22 signals); ADR-058",
    },
    "FirewallSignalsConfig.minja_bridge": {
        "evidence": "commit 8882208 (W2/N20/N22 signals); ADR-058",
    },
    # --- integrity (trust-horizon invariant; inert unless integrity.enabled) ---
    "IntegrityConfig.trust_weighted_ranking": {
        "evidence": "commit 6af7234 (monotone trust invariant); ADR-029 (proposed)",
        "note": "inert while IntegrityConfig.enabled is False",
    },
    "IntegrityConfig.principal_bound_corroboration": {
        "evidence": "commit 6af7234 (monotone trust invariant); ADR-029 (proposed)",
        "note": "inert while IntegrityConfig.enabled is False",
    },
    "IntegrityConfig.merge_reinforcement_gate": {
        "evidence": "commit 6af7234 (monotone trust invariant); ADR-029 (proposed)",
        "note": "inert while IntegrityConfig.enabled is False",
    },
    # --- top level ---
    "MemspineConfig.strict_services": {
        "evidence": UNKNOWN,
        "note": "commit 7d17b8a (P0.2 layered config) introduced it; ADR-031 mentions it; "
        "validation behaviour, no measured verdict",
    },
}
