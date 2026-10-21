# Asyncio loop factory and test skip audit

> Historical 2026-10-17 snapshot. The expanded PA-038 review in
> [TEST_SKIP_REVIEW.md](../TEST_SKIP_REVIEW.md) supersedes the skip inventory
> scope/counts below and assigns per-site owners and milestones. The asyncio
> implementation notes remain historical evidence.

Snapshot: 2026-10-17. This audit covers the current static skip guards under
`tests/unit`, `tests/integration`, `tests/e2e` and `tests/combinations`, plus the
skip totals actually present in the retained verification logs. Static guards
describe when a test will skip; they do not establish that a specific run
skipped for that reason.

## pytest-asyncio loop selection

The validated environment has pytest-asyncio 1.4.0 installed. Its installed
`plugin.py` declares `pytest_asyncio_loop_factories(config, item)` as a first
result hook returning a mapping of names to zero-argument loop factories. For
async tests, the plugin parameterizes available factories (or those named by an
`asyncio(loop_factories=...)` marker). The same source emits the deprecation
warning for overriding `event_loop_policy` and directs projects to this hook.

`tests/conftest.py` now returns `{"windows_selector": asyncio.SelectorEventLoop}`
only when all three conditions hold: the platform reports `win32`, the test is
one of the known Postgres module cells (including pgvector), and one of
`MEMSPINE_TEST_PG_URL`, `MEMSPINE_TEST_POSTGRES_URL` or
`MEMSPINE_TEST_LIVE_POSTGRES_URL` is set. In `test_provider_live_contracts.py`,
the selector is further limited to `test_postgres_storage_contract` by test
name. All other async tests receive `asyncio.new_event_loop`, retaining the
current platform policy behavior. The focused tests directly assert factory
type under simulated Windows selection and check opt-in and module/test-name
boundaries; these are hook-selection tests, not claims that Windows or a
Postgres server was exercised.

Focused validation used the dependency-validation Python 3.13 environment:

```text
PYTHONPATH=src python -m pytest -p no:deepeval -o addopts='' -q --tb=short -W error::pytest.PytestDeprecationWarning tests/unit/test_asyncio_loop_factory_hook.py tests/unit/config
124 passed in 4.94s
```

The result is preserved in [asyncio-hook-tests.log](asyncio-hook-tests.log).

## Static skip inventory

| Guard class and source sites | Reason and owner role | Environment or action required |
|---|---|---|
| Optional libraries: `tests/unit/services/cache/test_cashews_cache.py`, `test_cache_wiring.py`; `tests/unit/store/test_vector_spaces.py`; `tests/unit/services/vector/test_per_namespace_store.py`, `test_lancedb_rescore.py`, `test_lancedb_namespace_index.py`, `test_lancedb_memory_bound.py`, `test_lancedb_exclusive.py`; `tests/unit/wiring/test_glift_derived_lift.py`; `tests/unit/services/graph/test_wiring.py`, `test_parity.py`, `test_ladybug.py`; `tests/unit/test_graph_semantic_entities.py`; `tests/unit/services/secrets/test_secrets.py`; REST tests under `tests/unit/api/rest/`; `tests/unit/wiring/test_f_16c_scope.py`; `tests/unit/services/test_ladybug_fts_install.py`; `tests/unit/clients/test_sqlite_encryption.py`; `tests/unit/workers/test_temporal_runner.py`; `tests/unit/wiring/test_rt_01_transport.py` | The adapter or optional extra is unavailable; package/adapter maintainers own installing and exercising that integration. | Install the named dependency/extra (`cashews`, `lancedb`, `psutil`, `ladybug`, `fastapi`, `boto3`, `psycopg`, SQLCipher driver, `temporalio`/`resonate`, `taskiq`/`taskiq_redis`) in the selected environment. `test_tantivy.py` calls `importorskip` but describes Tantivy as a required core dependency; a skip there should be treated as a broken test environment, not expected optional coverage. |
| Instrumentation and tracing SDKs: `tests/unit/wiring/test_t5_06_observability.py`, `tests/unit/observability/test_stage_trace_coverage.py` | The in-memory exporter test requires the OpenTelemetry SDK; the trace coverage module also has a defensive package import guard. Observability maintainers own SDK coverage. | Install `opentelemetry-sdk` for the exporter test. `test_stage_trace_coverage.py` guards `memspine` itself, which should normally be importable from the project test environment; a skip there indicates a setup issue. |
| Optional model and runtime branches: `tests/unit/test_engine_e4.py`, `tests/unit/services/embedding/test_static_local.py`, `tests/unit/services/decision/test_decider.py`, `tests/unit/services/decision/test_decider_cpu_knobs.py`, `tests/unit/memories/associative/test_communities.py`, `tests/unit/workers/test_reorganize.py`, `tests/unit/memories/resource/test_extraction.py`, `tests/unit/services/rerank/test_rerank.py`, `tests/unit/test_engine_p7b.py`, `tests/unit/workers/test_taskiq_runner.py`, `tests/unit/wiring/test_w2_13_prepass.py`, `tests/unit/wiring/test_md_02_embedding_profile.py` | These tests skip a branch that cannot be reached with the currently installed dependency/model (or when a required native/extra capability is absent); feature owners own the matching matrix cell. | Install or remove the relevant optional package to make the desired path reachable; download the specifically named model where a cache is required. Several guards deliberately skip *negative import-gate* assertions when the package is already installed. They are not product failures. |
| Local data, cache, and benchmark fixtures: `tests/unit/test_list_mode_round2.py`, `tests/unit/test_no_dataset_text.py`, `tests/unit/test_prompt_gold_lint.py`, `tests/unit/test_forensic_schema.py` (evals harness and NLTK gates), `tests/unit/services/decision/test_decider.py`, `test_decider_cpu_knobs.py`, `tests/unit/wiring/test_w2_13_prepass.py` (GLiNER availability); `tests/integration` has no benchmark-data skip recorded in the scanned source | The local dataset/checkpoint/cache is intentionally not checked in; benchmark or model owners own provisioning. | Fetch the named local dataset/checkpoint or provide the local eval-harness and NLTK dependencies. Static inventory does not imply the data is available in CI. |
| Host tools and optional container: `tests/unit/schemas/test_strict_tiers.py`; `tests/unit/services/vector/test_lancedb_memory_bound.py`; `tests/unit/wiring/test_f_16e_neo4j_outbox.py` | Git/host capability unavailable, or the optional live service is not configured; test/platform owners own enabling it. | Install `git`; satisfy the memory-bound test's platform prerequisite; or set up the Neo4j service configuration expected by the test. |
| Live Neo4j, Postgres, and wording-dependent behavior: `tests/unit/wiring/test_fu_gaps.py`, `tests/unit/wiring/test_fu_sleep.py`, `tests/unit/wiring/test_rw_d_gate_deep.py` | The live cases need optional graph/database services; one time-view case skips when the rules do not classify the phrase as temporal. Graph/storage feature owners own the service-backed cases, and the request-classification owner owns the conditional assertion. | Set `MEMSPINE_TEST_NEO4J_URL` (plus its documented user/password variables) for Neo4j, or `MEMSPINE_TEST_PG_URL` for Postgres. The wording-dependent skip has no external setup requirement; it means its specific conditional input did not activate. |
| Live Postgres: `tests/unit/store/test_f16d_storage_profile.py`, `tests/unit/wiring/test_fu_sleep.py`, `tests/integration/test_storage_backends.py`, `tests/e2e/test_f16f_matrix.py`, `tests/e2e/test_ex1a_server.py` | The test needs an explicitly selected scratch database and, where applicable, the psycopg driver; database integration owners own the live cell. | Set the exact `MEMSPINE_TEST_PG_URL` or `MEMSPINE_TEST_POSTGRES_URL` variable named in each test and provide reachable scratch Postgres. The tests check reachability and may skip after failed connection. Never point these tests at production. |
| Live Temporal / Resonate: `tests/integration/test_temporal_resonate_recovery.py`, `tests/integration/test_runner_parity.py` | SDK absence or missing live Temporal address gates the live engine/server case; orchestration integration owners own the external service cell. | Install `[temporal]` dependencies and set `MEMSPINE_TEST_TEMPORAL_ADDRESS` to a test server. Local journal/recovery cases are separate and do not need this server. |
| Live provider services: `tests/integration/providers/test_pgvector_contract.py`, `test_provider_live_contracts.py`, `test_remote_search_live_contracts.py` | Unconfigured pgvector or remote-provider services are opt-in; provider integration owners own service-backed contracts. | Supply the local service variables named by the tests and provide reachable scratch pgvector / remote-provider endpoints and credentials. The skip text reports missing variable *names*, not their values. |
| Fixture semantics or unsupported replay: `tests/unit/memories/associative/test_communities.py`, `tests/unit/services/rerank/test_rerank.py`, `tests/unit/test_engine_p7b.py`, `tests/unit/workers/test_taskiq_runner.py`, `tests/unit/wiring/test_fu_sleep.py`, `tests/e2e/test_f16f_matrix.py`, `tests/e2e/test_ex1a_server.py`, `tests/unit/wiring/test_f_16e_neo4j_outbox.py` | Some paths intentionally skip because the feature is disabled, an installed package makes an import-failure branch unreachable, or a replay mode only supports SQLite. Feature/test owners should keep the reason aligned with the actual contract. | No environment change can make an intentionally unreachable assertion valid; exercise the corresponding reachable behavior or the documented backend-specific path. For Postgres lease cases, supply the scratch DB as above. |
| Optional ingestion/model cache and input fixture: `tests/combinations/test_dedup_and_ingest_smoke.py`, `tests/unit/wiring/test_md_02_embedding_profile.py`, `tests/unit/test_prompt_gold_lint.py`, `tests/unit/test_list_mode_round2.py`, `tests/unit/test_no_dataset_text.py`, `tests/unit/test_forensic_schema.py` | The ingest extra, model cache, or local benchmark input is absent; ingestion, embedding, and evaluation-fixture owners respectively own enabling these cases. `test_forensic_schema.py` also gates the separate eval harness and NLTK F1 dependency. | Install `[ingest]`; cache the specified FastEmbed model; fetch the named local dataset; or install/import the eval harness and NLTK for its corresponding test. |
| External runner parity: `tests/integration/test_runner_parity.py` | Runner parity skips when only inline is installed because the parity assertion would have one participant; orchestration maintainers own the expanded runner matrix. | Install the optional fakeredis/taskiq runners. Its separate live Temporal tests require the address and SDK described above. |

The grouped inventory is paired with a machine-readable per-callsite snapshot:
[test-skip-inventory.json](test-skip-inventory.json). Regenerate it with:

```text
python scripts/audit_test_skips.py
```

The stdlib-only script parses, but never imports or evaluates, Python source
under `tests/unit`, `tests/integration`, `tests/e2e` and `tests/combinations`.
The snapshot contains file, line, call kind, source reason/condition expressions,
literal reasons when statically resolvable, module environment references, and
a suggested owner role inferred from the test path. Dynamic expressions remain
unresolved. Environment names are marked as source references only; their
presence does not prove a specific skip uses them as a guard. Owner roles are
not sourced from repository ownership metadata. The current snapshot has 98
callsites (45 `importorskip`, 39 `skip`, 14 `skipif`) and zero parse errors.
`evals/tests` is excluded because the project runs it as a separate harness.

## Executed skip outcomes in retained logs

| Evidence | Observed outcome | Attribution limits |
|---|---|---|
| `gliner25-adapters-tests.log` | 69 passed, 1 skipped | The retained pytest output omits `-rs`, so it does not preserve the skipped node ID or reason. Source inspection alone cannot prove which guard accounted for this observed skip. |
| `temporal-migration-regression.log` | 1,308 passed, 13 skipped, 1 failed during implementation | The log records only the aggregate skip total; it does not retain skipped node IDs/reasons. Its single failure is an implementation-era config test and is not a skip. |
| `temporal-migration-final-focused.log`, `temporal-migration-final-state-tests.log`, `temporal-live-final-tests.log` | 160 passed, 161 passed, and 9 passed respectively; no skips reported | These are focused final gates and do not cover all skip guards. |
| `provider_verification/README.md` historical sweep summary | 7,434 passed / 61 skipped for the offline unit sweep; 96 passed / 9 skipped for combinations/integration | The README gives aggregate counts without a per-skip list. Those counts are historical run results, not results from this audit. |
| `provider_verification/README.md` earlier sweep summary | 7,440 passed / 31 skipped for the offline unit suite; 96 passed / 5 skipped for combinations/integration | Earlier aggregates only; the README does not map those skipped counts to individual call sites. |

Future verification runs intended to establish executed skip reasons should use
pytest's `-rs` option and retain the resulting output next to the run log. Do not
promote an aggregate static inventory into a claim about an individual run.
