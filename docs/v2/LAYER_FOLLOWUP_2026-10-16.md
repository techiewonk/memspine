# Layer review follow-up, 2026-10-16

Changes following the Codex review of `f5efe4a3`. Three GPT-6 Luna agents handled migration testing, profile testing and evaluation documentation; the parent reviewed their work and implemented runtime inventory wiring. No production database was migrated, no model benchmark was run, and no changes were committed or pushed.

| Review item | Change | Evidence / remaining limit |
|---|---|---|
| Historical explicit L0/L1 repair | Corrected the migration guide: ordinary rebuild preserves explicit labels; fresh-copy `migrate_sqlite` applies the versioned correction. Added an integration regression checking repaired target and unchanged source bytes/events | `tests/unit/store/test_migrate_explicit_layer_regression.py`; historical production stores still need an operator-run migration. Live PostgreSQL upgrade remains unverified |
| Profile-card reader | Public `Engine.read()` tests cover assembled profile text, expiry, quarantine, erasure and partial support. Rendering now requires nonempty, fully visible cited support; freshness uses the engine clock | `tests/unit/contract/test_profile_card_read.py`; partial support cannot justify rendering an unattributed whole entry. This can omit a valid redundantly supported entry until independent-support semantics are available |
| Runtime inventory | Registered read legs now use the effective configuration rather than an empty template. Optional offline producers contribute configured layers; latest profile-card entries are inventoried separately from records. Graph artefacts remain separate; the leg path emits `read.layers` | `tests/unit/contract/test_layer_inventory.py` checks actual public-search wiring, marker replacement and configured/selected separation. Inventory availability is not an access or freshness decision; profile inclusion is still decided by the consumer |
| Evaluation taxonomy | Separated procedural skills, task state, prospective watches, anticipation cues, standing answers, profile cards and document/asset ingestion | `MEMORY_TYPE_EVAL_PLAN.md`; held-out accuracy, model cost and p95 latency remain pending |

## Validation commands

Use the project virtual environment with `PYTHONPATH=src;evals`. Run pytest with `-p no:deepeval -o addopts='' -q` over the contract tests, migration tests, tri-temporal tests, graph-leg wiring, session-close/profile wiring, pattern/standing wiring and router-rule tests. Run Ruff on changed Python files and `git diff --check`.

Existing warnings in the exercised suites concern pytest-asyncio fixture deprecation and LanceDB version cleanup in erasure tests. These changes do not establish benchmark improvements. The older forensic report is historical evidence; this follow-up and the updated testing guide describe the current corrections.
