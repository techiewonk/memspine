# Scenario fixtures F1..F12 (RM-22, RM-54)

Executable behaviour fixtures for the synthetic discussion cases. Every scenario runs through
the public `Engine` API on the default rules-only profile (`template="base"`: online rules
extractor over the durable outbox, no LLM role bound) with the `hash` embedder and the
associative graph plus edge compiler. Shared setup and checks live in `scenario_harness.py`.

Each module asserts the claims (`SemanticEnvelope` modality, status, outcome, role bindings,
evidence spans), the compiled graph edges and qualifiers, and the read answers, and includes:

- a known-time check (`recorded_before` and/or `as_of` reads);
- a replay check (`rebuild` gives identical claims, entity ids, edges and annotations);
- an erasure check (hard `forget` of the evidence removes the claims, edges, annotations and
  hits, also after a rebuild; re-authorisation by new evidence re-derives where specified).

Contract points the engine does not meet yet are single `xfail(strict=True)` tests naming
their tracker row and the code location.

| Fixture | Module | Source |
|---|---|---|
| F1 residence | `test_scenario_f01_residence.py` | `docs/BI01_CONTRACT_REWRITE_PLAN.md` "RM-22 fixture specification"; `docs/CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md` section 6 |
| F2 identity | `test_scenario_f02_identity.py` | same; ADR-089 |
| F3 correction | `test_scenario_f03_correction.py` | same |
| F4 modality | `test_scenario_f04_modality.py` | same |
| F5 later evidence | `test_scenario_f05_later_evidence.py` | same |
| F6 document | `test_scenario_f06_document.py` | `docs/BI01_CONTRACT_REWRITE_PLAN.md` "Expanded synthetic fixtures (RM-54)" |
| F7 tool | `test_scenario_f07_tool.py` | same |
| F8 commitment | `test_scenario_f08_commitment.py` | same; role family 1 |
| F9 group | `test_scenario_f09_group.py` | same |
| F10 preference | `test_scenario_f10_preference.py` | same |
| F11 numeric | `test_scenario_f11_numeric.py` | same |
| F12 language | `test_scenario_f12_language.py` | same |
| Role families 1-3 (RM-47, RM-50) | `test_scenario_role_families.py` | `docs/BI01_CONTRACT_REWRITE_PLAN.md` "Owner review decisions 2026-10-22", "Registry contracts implemented" |

F13 (shared grant) lives in `tests/unit/wiring/test_rm88_rm91_graph_caller.py`.

No rule extracts the family 1-3 predicates yet, so their fixtures use a scripted
non-generative extractor behind the DS-18 extractor hook; the compiler, registry gate,
deposit, edge compiler, rebuild and erasure are the real path. Fixture texts are invented
discussion cases, not benchmark gold.
