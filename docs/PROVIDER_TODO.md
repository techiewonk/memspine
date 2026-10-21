# MemSpine - single live task and status tracker

Updated 2026-10-22. **This is the only editable task-status source for Codex, Claude and maintainers.** All current statuses, completed work, next milestones and task ownership live in the register below. Plans, reviews, handoffs and JSON/logs are context or historical evidence, not competing live trackers. Never create a second current-status list.

Keep one row per stable ID. PA IDs preserve the 136-item provider program; BI IDs are the 12 new background packages; DS IDs are review findings; RS IDs are bounded research; RM IDs preserve otherwise untracked roadmap suggestions. Do not combine their denominators or mark runtime implementation done because documentation/benchmarks passed. `DONE` is limited to its recorded gate. `REVIEW`/`PROPOSED`/`DEFERRED` do not authorise execution.

## Current status summary

<!-- CURRENT_SUMMARY_START -->
| Current status | All work items |
|---|---:|
| BLOCKED | 1 |
| DEFERRED | 6 |
| DEPLOYMENT_CHECK | 19 |
| DONE | 137 |
| EXCLUDED | 4 |
| IN_PROGRESS | 30 |
| NEEDS_INPUT | 3 |
| NEEDS_TOOLCHAIN | 1 |
| OPEN | 19 |
| OPEN_REVIEW | 18 |
| PLANNED | 11 |
| PROPOSED | 3 |
| REVIEW | 6 |

| Program | Items |
|---|---:|
| BI | 12 |
| DS | 21 |
| PA | 136 |
| RM | 80 |
| RS | 9 |

Total: **258 unique task IDs**. Provider-only historical denominator stays 136; the combined total also includes BI, DS, RS and imported review suggestions.
<!-- CURRENT_SUMMARY_END -->

## Status and column legend

| Status | Meaning |
|---|---|
| PLANNED | Accepted design package; implementation not started |
| OPEN | Selected local work, not started or not finished |
| IN_PROGRESS | Someone is actively working on it (add a progress entry) |
| VERIFY | Implemented; its completion gate is being checked |
| DONE | The recorded gate passed for the recorded scope only; not production approval |
| OPEN_REVIEW | Confirmed finding; no fix selected yet |
| REVIEW | Imported suggestion; scope and evidence must be reviewed and selected before work |
| PROPOSED | Optional enhancement; not selected |
| NEEDS_INPUT | Waiting on an owner decision, budget, host or destination |
| NEEDS_TOOLCHAIN | Waiting on a local build toolchain |
| DEPLOYMENT_CHECK | Needs a real target deployment; does not block local development |
| BLOCKED | Has a concrete unmet dependency and a named next action |
| DEFERRED | Owner postponed it; findings retained |
| EXCLUDED | Owner excluded it; reopen only by a new explicit decision |

Normal flow: `PLANNED/OPEN/REVIEW/PROPOSED → IN_PROGRESS → VERIFY → DONE`.

**Dependencies column:** `Prereq:` lists IDs that must be DONE (or decided) before this item can
start or be marked DONE. `Feeds:` lists IDs that receive this item's remaining scope or results;
it is not a prerequisite. `Note:` is free text about ordering and is not parsed as IDs. A cell
with plain IDs and no labels lists prerequisites. A DONE row must never have an unfinished `Prereq:`; if it does, either
its scope is narrower than the prerequisite (say so in the row) or its status is wrong.

## Current task register

<!-- TASK_REGISTER_START -->
| ID | Current status | Priority | Owner role | Task | Completed work / evidence | Next milestone / completion gate | Dependencies |
|---|---|---|---|---|---|---|---|

| PA-001 | DONE | P1 | Provider maintainer | Unify the eight provider categories under one registry owner | Recorded local gate: Descriptor registration and category lookup are covered. [registry.py](../src/memspine/providers/registry.py) | Completed for recorded scope; revalidate affected gates on change | None |
| PA-002 | DONE | P1 | Provider maintainer | Expose one inspection catalog over descriptors | Recorded local gate: Catalog discovery does not construct native services. [test_provider_catalog.py](../tests/unit/providers/test_provider_catalog.py) | Completed for recorded scope; revalidate affected gates on change | PA-001 |
| PA-003 | DONE | P1 | Provider maintainer | Validate selected provider settings strictly | Recorded local gate: Wrong types and unknown fields are rejected before connection. [test_provider_settings.py](../tests/unit/providers/test_provider_settings.py) | Completed for recorded scope; revalidate affected gates on change | PA-001 |
| PA-004 | DONE | P1 | Provider maintainer | Publish provider-owned defaults and required settings | Recorded local gate: Generated reference includes model and native settings. [PROVIDER_DEFAULTS.md](../docs/PROVIDER_DEFAULTS.md) | Completed for recorded scope; revalidate affected gates on change | PA-003 |
| PA-005 | DONE | P1 | Provider maintainer | Use typed provider build contexts and results | Recorded local gate: Category contracts replace untyped builder wiring. [contexts.py](../src/memspine/providers/contexts.py) | Completed for recorded scope; revalidate affected gates on change | PA-001 |
| PA-006 | DONE | P1 | Provider maintainer | Keep optional provider dependencies lazy | Recorded local gate: Discovery avoids importing unused native clients. [test_provider_registry.py](../tests/unit/providers/test_provider_registry.py) | Completed for recorded scope; revalidate affected gates on change | PA-001 |
| PA-007 | DONE | P1 | Provider maintainer | Apply owned and borrowed resource lifecycles | Recorded local gate: Engine closes owned resources and preserves borrowed resources. [test_provider_lifecycle.py](../tests/unit/providers/test_provider_lifecycle.py) | Completed for recorded scope; revalidate affected gates on change | PA-005 |
| PA-008 | DONE | P1 | Provider maintainer | Protect dependencies when a dependent resource fails to close | Recorded local gate: Failures block dependent teardown and independent cleanup continues. [test_provider_lifecycle.py](../tests/unit/providers/test_provider_lifecycle.py) | Completed for recorded scope; revalidate affected gates on change | PA-007 |
| PA-009 | DONE | P1 | Provider maintainer | Unwind partial startup and drain blocking cancellation paths | Recorded local gate: Failure and cancellation regressions pass. [test_engine_cleanup.py](../tests/unit/runtime/test_engine_cleanup.py) | Completed for recorded scope; revalidate affected gates on change | PA-007 |
| PA-010 | DONE | P1 | Provider maintainer | Provide managed SQL access and shared leases | Recorded local gate: Borrowed SQL use does not close the authoritative connection. [test_sql_access.py](../tests/unit/providers/test_sql_access.py) | Completed for recorded scope; revalidate affected gates on change | PA-007 |
| PA-011 | DONE | P1 | Provider maintainer | Build SQL/vector/lexical/graph/cache/model services through descriptors | Recorded local gate: Engine integration uses the selected provider contracts. [test_provider_engine_integration.py](../tests/unit/providers/test_provider_engine_integration.py) | Completed for recorded scope; revalidate affected gates on change | PA-005 |
| PA-012 | DONE | P1 | Provider maintainer | Separate primary graph and optional graph mirror | Recorded local gate: Independent graph targets have isolated identities. [test_graph_targets.py](../tests/unit/providers/test_graph_targets.py) | Completed for recorded scope; revalidate affected gates on change | PA-011 |
| PA-013 | DONE | P1 | Provider maintainer | Support a directly selected Neo4j graph provider | Recorded local gate: Driver ownership and native graph contracts are checked. [test_provider_neo4j_client.py](../tests/unit/providers/test_provider_neo4j_client.py) | Completed for recorded scope; revalidate affected gates on change | PA-012 |
| PA-014 | DONE | P1 | Provider maintainer | Scope identities and cache state by deployment | Recorded local gate: Deployment changes cannot silently reuse another deployment's cache. [test_provider_cache_isolation.py](../tests/unit/providers/test_provider_cache_isolation.py) | Completed for recorded scope; revalidate affected gates on change | PA-011 |
| PA-015 | DONE | P1 | Provider maintainer | Fingerprint model profiles and projection targets | Recorded local gate: Target drift is rejected and SQL target identity is credential-free. [test_provider_fingerprint.py](../tests/unit/providers/test_provider_fingerprint.py) | Completed for recorded scope; revalidate affected gates on change | PA-014 |
| PA-016 | DONE | P1 | Provider maintainer | Implement durable controlled-change plans and operations | Recorded local gate: Operation ownership, revision, fence and checkpoint checks pass. [test_provider_changes.py](../tests/unit/providers/test_provider_changes.py) | Completed for recorded scope; revalidate affected gates on change | PA-015 |
| PA-017 | DONE | P1 | Provider maintainer | Support projection generations, replay and verified activation | Recorded local gate: Serving activation requires verified target proof. [test_provider_switching.py](../tests/unit/providers/test_provider_switching.py) | Completed for recorded scope; revalidate affected gates on change | PA-016 |
| PA-018 | DONE | P1 | Provider maintainer | Make expired-generation cleanup explicit and bounded | Recorded local gate: Opt-in sweeps respect fences, scope and retention settings. [test_provider_cleanup_sweep.py](../tests/unit/providers/test_provider_cleanup_sweep.py) | Completed for recorded scope; revalidate affected gates on change | PA-017 |
| PA-019 | DONE | P1 | Provider maintainer | Add audited hard-crash cleanup-fence recovery | Recorded local gate: Recovery requires matching guard proof and transactional CAS. [test_provider_recovery.py](../tests/unit/providers/test_provider_recovery.py) | Completed for recorded scope; revalidate affected gates on change | PA-018 |
| PA-020 | DONE | P1 | Provider maintainer | Add SQLite and PostgreSQL provider-control migrations | Recorded local gate: Change, binding and recovery schema migrations are present. [storage](../src/memspine/services/storage) | Completed for recorded scope; revalidate affected gates on change | PA-016 |
| PA-021 | DONE | P1 | Provider maintainer | Implement typed HTTP service-change transport | Recorded local gate: Protocol, acknowledgements, deadlines and payload bounds are tested. [test_http_service_changes.py](../tests/unit/providers/test_http_service_changes.py) | Completed for recorded scope; revalidate affected gates on change | PA-016 |
| PA-022 | DONE | P1 | Provider maintainer | Add read-only inspection of a running deployment | Recorded local gate: Inspection makes no ingestion/model call and never infers production approval. [test_provider_deployment.py](../tests/unit/providers/test_provider_deployment.py) | Completed for recorded scope; revalidate affected gates on change | PA-011 |
| PA-023 | DONE | P1 | Provider maintainer | Implement OpenSearch lexical adapter | Recorded local gate: Scoped BM25/upsert/delete and Engine integration passed locally. [test_remote_search_providers.py](../tests/unit/providers/test_remote_search_providers.py) | Completed for recorded scope; revalidate affected gates on change | PA-011 |
| PA-024 | DONE | P1 | Provider maintainer | Implement independent Weaviate lexical and vector adapters | Recorded local gate: Scoped BM25/cosine paths and Engine integration passed locally. [test_remote_search_providers.py](../tests/unit/providers/test_remote_search_providers.py) | Completed for recorded scope; revalidate affected gates on change | PA-011 |
| PA-025 | DONE | P1 | Provider maintainer | Implement pgvector adapter on managed PostgreSQL leases | Recorded local gate: Provisioning/serving restart, ranking and isolation passed natively. [pgvector-live-contract.json](../docs/provider_verification/pgvector-live-contract.json) | Completed for recorded scope; revalidate affected gates on change | PA-010 |
| PA-026 | DONE | P1 | Provider maintainer | Reject unsupported remote vector modes before connection | Recorded local gate: Unsupported modes, invalid dimensions and invalid vectors fail closed. [test_pgvector_provider.py](../tests/unit/providers/test_pgvector_provider.py) | Completed for recorded scope; revalidate affected gates on change | PA-024, PA-025 |
| PA-027 | DONE | P1 | Provider maintainer | Preserve honest deletion semantics across backends | Recorded local gate: Logical deletion is not reported as proven WAL/history erasure. [pgvector-live-contract.json](../docs/provider_verification/pgvector-live-contract.json) | Completed for recorded scope; revalidate affected gates on change | PA-025 |
| PA-028 | DONE | P1 | Provider maintainer | Forward provider-owned LLM generation and embedding settings | Recorded local gate: Role settings, temperature and embedding dimensions are checked. [test_provider_llm_generation_settings.py](../tests/unit/providers/test_provider_llm_generation_settings.py) | Completed for recorded scope; revalidate affected gates on change | PA-004 |
| PA-029 | DONE | P1 | Provider maintainer | Detach and recursively freeze the effective Engine configuration | Recorded local gate: Input mutation cannot alter the running snapshot. [test_effective_config_snapshot.py](../tests/unit/config/test_effective_config_snapshot.py) | Completed for recorded scope; revalidate affected gates on change | PA-003 |
| PA-030 | DONE | P1 | Provider maintainer | Revalidate mutated typed input constraints at startup | Recorded local gate: Range and cross-field failures are rejected without dumping settings. [test_effective_config_snapshot.py](../tests/unit/config/test_effective_config_snapshot.py) | Completed for recorded scope; revalidate affected gates on change | PA-029 |
| PA-031 | DONE | P1 | Provider maintainer | Remove old provider registries and compatibility translation | Recorded local gate: Removed keys and aliases are rejected by the breaking schema. [PROVIDER_OPERATIONS.md](../docs/PROVIDER_OPERATIONS.md) | Completed for recorded scope; revalidate affected gates on change | PA-001 |
| PA-032 | DONE | P1 | Provider maintainer | Record final targeted verification evidence | Recorded local gate: 352 config/runtime/provider tests, Ruff and mypy passed. [README.md](../docs/provider_verification/README.md) | Completed for recorded scope; revalidate affected gates on change | PA-030 |
| PA-033 | DONE | P1 | Provider maintainer | Record native provider contract evidence | Recorded local gate: Combined eight cases plus separate pgvector case passed. [live-provider-core-latest.json](../docs/provider_verification/live-provider-core-latest.json) | Completed for recorded scope; revalidate affected gates on change | PA-023, PA-024, PA-025 |
| PA-034 | DONE | P1 | Provider maintainer | Record deterministic baseline/performance comparison | Recorded local gate: Five trials preserve stored and ordered-search hashes. [provider-benchmark.json](../docs/provider_verification/provider-benchmark.json) | Completed for recorded scope; revalidate affected gates on change | PA-032 |
| PA-035 | DONE | P1 | Provider maintainer | Commit the implementation locally | Recorded local gate: Implementation is recorded as e6596f44; no push claimed. [PROVIDER_ARCHITECTURE_PLAN.md](../docs/PROVIDER_ARCHITECTURE_PLAN.md) | Completed for recorded scope; revalidate affected gates on change | PA-032, PA-033, PA-034 |
| PA-036 | DONE | P1 | Provider maintainer | Create a durable verification run manifest | Recorded local gate: Each run records source revision, command, environment, result and artifact path. [Ledger tool](../scripts/record_verification_run.py); [usage](provider_verification/LEDGER_USAGE.md) | Completed for recorded scope; revalidate affected gates on change | PA-032 |
| PA-037 | DONE | P1 | Provider maintainer | Preserve portable logs outside temporary directories | Recorded local gate: Sanitized important logs survive workstation temp cleanup. [Portable evidence](provider_verification/README.md) | Completed for recorded scope; revalidate affected gates on change | PA-036 |
| PA-038 | DONE | P1 | Verification lead | Review all active test skip sites | Recorded local gate: Every discovered site has a reviewed reason, owner role, prerequisite, disposition and milestone; source pins and focused checks pass. [Complete skip review](TEST_SKIP_REVIEW.md); [reviewed inventory](provider_verification/test-skip-inventory.json) | Completed for recorded scope; revalidate affected gates on change | PA-036 |
| PA-039 | OPEN | P1 | Verification lead | Schedule a clean full unit sweep for the next release candidate | Gate not complete; prior evidence: [README.md](../docs/provider_verification/README.md) | One final-revision run closes the historical three-failure/retest split | PA-038 |
| PA-040 | DONE | P2 | Provider maintainer | Review provider checks in existing CI | Recorded local gate: Document existing coverage first, then add only missing provider checks. [CI coverage](PROVIDER_CI_COVERAGE.md) | Completed for recorded scope; revalidate affected gates on change | PA-036 |
| PA-041 | DONE | P2 | Provider maintainer | Add a gated disposable-native CI matrix if missing | Recorded local gate: Pinned service versions run with owned cleanup and no secrets in logs. [Strict local native gate](provider_verification/native-strict-final.json); [manual CI](../.github/workflows/provider-native.yml) | Completed for recorded scope; revalidate affected gates on change | PA-040 |
| PA-042 | DONE | P2 | Provider maintainer | Run an installed-wheel smoke test in a clean environment | Recorded local gate (2026-10-17, e6596f44 dirty tree, Windows/CPython 3.13.5): installed core Engine start/write/search/stop passed without source-tree imports; predates final wave sources. [Installed-wheel evidence](provider_verification/installed-wheel-checks.json) | Completed for recorded scope; release-candidate rerun tracked in RM-30 | Prereq: none; Feeds: RM-30 |
| PA-043 | DONE | P2 | Provider maintainer | Test clean installs of each optional remote extra | Recorded local gate: OpenSearch, Weaviate and Postgres extras install independently. [Independent extra installs](provider_verification/installed-wheel-checks.json) | Completed for recorded scope; revalidate affected gates on change | PA-042 |
| PA-044 | DONE | P2 | Provider maintainer | Review the supported Python/OS/dependency test matrix | Recorded local gate: A documented matrix identifies tested and untested combinations. [Platform matrix](PROVIDER_PLATFORM_MATRIX.md) | Completed for recorded scope; revalidate affected gates on change | PA-040 |
| PA-045 | DONE | P2 | Provider maintainer | Exercise a sample third-party provider plugin | Recorded local gate: Pinned plugin discovery, bad contracts and lifecycle behavior are checked. [Plugin wheel evidence](provider_verification/plugin-wheel-checks.json) | Completed for recorded scope; revalidate affected gates on change | PA-001, PA-043 |
| PA-046 | DONE | P1 | Performance maintainer; budget approval by application owner | Resolve or explicitly accept the cold-start regression | Owner selected optimise (2026-10-22). Profile: import lancedb is about 1.26 s of start, mostly its eager lance_namespace REST models. Fix: ProviderDescriptor.prewarm_modules plus a startup prewarm thread after config resolution (LanceDB declares lancedb). Re-measure vs a81e7f0 (5 paired trials, behaviour identical): cold start -5.3% (was +13.5%), import+start -12.7%, ingest -5.9%, nothing over the 10% threshold. [before](provider_verification/pa046-coldstart-remeasure-2026-10-22.json) [after](provider_verification/pa046-coldstart-prewarm-2026-10-22.json) | Completed on a shared host; controlled-host confirmation stays under PA-051. Broader warm-up and sharing tracked in RM-70 | PA-034 |
| PA-047 | DONE | P2 | Provider maintainer | Profile the final configuration-admission code | Recorded local gate: New profile includes final range/cross-field revalidation, unlike earlier component profile. [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Completed for recorded scope; revalidate affected gates on change | PA-030 |
| PA-048 | DONE | P2 | Provider maintainer | Evaluate SQLite migration/import startup cost | Recorded local gate: import/connect/schema phases sampled once (shared host); no code change retained. A single sample cannot support an optimization decision. [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Completed for evaluate-only scope; optimization decision deferred to the PA-046 budget | Prereq: PA-047; Feeds: PA-046 |
| PA-049 | DONE | P2 | Provider maintainer | Evaluate LanceDB first-connect/import cost | Recorded local gate: import, connect, table create and table open timed separately (one shared-host sample, import 858 ms); no optimization selected. [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Completed for recorded scope; revalidate affected gates on change | Prereq: PA-047; Feeds: PA-046 |
| PA-050 | DONE | P2 | Provider maintainer | Measure first serialization separately from startup | Recorded local gate: Track approximately 2.69 ms extra first-dump cost; no hidden deferred-cost claim. [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Completed for recorded scope; revalidate affected gates on change | PA-047 |
| PA-051 | NEEDS_INPUT | P2 | Performance maintainer; host supplied by application owner | Repeat timing on a controlled representative host | Gate not complete; prior evidence: [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Record host load, repetitions and variability, including repeated PA-052 corpus sizes, before setting latency guarantees | PA-046 |
| PA-052 | DONE | P2 | Provider maintainer | Measure vector and lexical scaling with larger synthetic corpora | Recorded local gate (exploratory, shared host, one run per size): recall/precision/p50/p95/working set at 100/500/1,000 records with fixed hash-embedding settings. [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Completed for exploratory scope; no latency or semantic-quality claim; controlled repeat under PA-051 | Prereq: PA-047; Feeds: PA-051, PA-053 |
| PA-053 | OPEN | P2 | Ingestion/worker maintainer | Measure concurrent ingest/search and queue backpressure | Gate not complete; prior evidence: [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Owner sets foreground p95 and derivation-lag budgets; measured concurrent ingest/search stays within them | PA-052 |
| PA-054 | DONE | P2 | Provider maintainer | Measure remote-provider tail latency and timeout behavior | Recorded local gate: Per-backend p50/p95/p99 and bounded failure behavior are recorded. [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Completed for recorded scope; revalidate affected gates on change | PA-052 |
| PA-055 | DONE | P2 | Provider maintainer | Profile long-running memory and connection growth | Recorded local gate (finite): fresh-process 5 warmup + 30 cycles in 20.8 s, local SQLite/LanceDB/Tantivy; 105/105 DBAPI connect/close, zero open after each stop, RSS spread under diagnostic guard. Perf doc still says partial pending review. [30-cycle resource soak](provider_verification/provider-resource-soak.json) | Completed for finite local scope only; no long-running or remote-pool claim; extension tracked in RM-29 and RM-31 | Prereq: none; Feeds: RM-29, RM-31, PA-051 |
| PA-056 | DEPLOYMENT_CHECK | P1 | Application/deployment owner | Identify the actual deployment and owning application | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | Deployment ID, environment, controller owner and runbook are supplied | None |
| PA-057 | DEPLOYMENT_CHECK | P1 | Platform maintainer | Record intended provider versions and effective configuration | Gate not complete; prior evidence: [PROVIDER_DEFAULTS.md](../docs/PROVIDER_DEFAULTS.md) | Deployment manifest pins services/models and credential-free config identity | PA-056 |
| PA-058 | DEPLOYMENT_CHECK | P1 | Platform/security maintainer | Verify credentials, TLS and network permissions on the target | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | Least-privilege connectivity is tested without exposing secrets | PA-057 |
| PA-059 | DEPLOYMENT_CHECK | P1 | Application integration maintainer | Implement or connect the real service-change controller | Gate not complete; prior evidence: [http_service_changes.py](../src/memspine/providers/http_service_changes.py) | All registered phases perform native actions and return actual evidence | PA-056 |
| PA-060 | DEPLOYMENT_CHECK | P1 | Controller maintainer | Enforce remote fencing and persisted idempotency atomically | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | Stale epochs are refused and unknown HTTP outcomes reconcile safely | PA-059 |
| PA-061 | DEPLOYMENT_CHECK | P1 | Model serving maintainer | Verify LLM/reranker forward-revision changes end to end | Gate not complete; prior evidence: [service_changes.py](../src/memspine/providers/service_changes.py) | Native revisions activate once and subsequent work uses the selected revision | PA-060 |
| PA-062 | DEPLOYMENT_CHECK | P1 | Cache maintainer | Verify cache coldstart changes end to end | Gate not complete; prior evidence: [service_changes.py](../src/memspine/providers/service_changes.py) | Old cache state is isolated and the new revision serves correctly | PA-060 |
| PA-063 | DEPLOYMENT_CHECK | P1 | Orchestration maintainer | Verify worker handover end to end | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | Old admission is stopped and leases/acknowledgements prove safe handover | PA-060 |
| PA-064 | DEPLOYMENT_CHECK | P1 | Application/deployment owner | Provide a real quiescence guard for cleanup recovery | Gate not complete; prior evidence: [recovery.py](../src/memspine/providers/recovery.py) | Proof shows previous owners cannot resume during recovery and retry | PA-056 |
| PA-065 | DEPLOYMENT_CHECK | P1 | Recovery maintainer | Run hard-crash recovery and stale-owner drills | Gate not complete; prior evidence: [test_provider_recovery.py](../tests/unit/providers/test_provider_recovery.py) | A killed worker can be recovered without accepting stale writes | PA-064 |
| PA-066 | DEPLOYMENT_CHECK | P1 | Security/tenant isolation maintainer | Check production tenant and namespace isolation | Gate not complete; prior evidence: [PROVIDER_OPERATIONS.md](../docs/PROVIDER_OPERATIONS.md) | Synthetic tenants cannot search, overwrite or erase one another | PA-058 |
| PA-067 | DEPLOYMENT_CHECK | P1 | Platform/security maintainer | Check native creation versus serving permissions | Gate not complete; prior evidence: [PROVIDER_OPERATIONS.md](../docs/PROVIDER_OPERATIONS.md) | Bootstrap privileges are separate from ordinary serving access | PA-058 |
| PA-068 | DEPLOYMENT_CHECK | P1 | Storage/backup maintainer | Validate backup and restore procedures | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | A deployment-owned synthetic restore is checked against expected records | PA-057 |
| PA-069 | DEPLOYMENT_CHECK | P1 | Platform reliability maintainer | Validate outages, restart and reconnect behavior | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | Loss of each selected dependency has documented recovery behavior | PA-058 |
| PA-070 | DEPLOYMENT_CHECK | P1 | Data retention owner | Validate retention and deletion policy on the deployment | Gate not complete; prior evidence: [PROVIDER_OPERATIONS.md](../docs/PROVIDER_OPERATIONS.md) | Scope includes indexes and documented WAL/replica/backup limitations | PA-067, PA-068 |
| PA-071 | DEPLOYMENT_CHECK | P2 | Observability maintainer | Add provider health, projection lag and recovery audit alerts | Gate not complete; prior evidence: [inspection.py](../src/memspine/providers/inspection.py) | Alerts are routed and tested with actionable thresholds | PA-056 |
| PA-072 | DEPLOYMENT_CHECK | P1 | Application acceptance owner | Run the authorized representative production workload | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | Latency, error, capacity and model-quality gates have real evidence | PA-061, PA-062, PA-063, PA-066, PA-069 |
| PA-073 | DEPLOYMENT_CHECK | P1 | Application/deployment owner | Record deployment acceptance separately from infrastructure readiness | Gate not complete; prior evidence: [deployment.py](../src/memspine/providers/deployment.py) | Owner signs off measured gates; inspection alone is insufficient | PA-065, PA-068, PA-070, PA-072 |
| PA-074 | DONE | P2 | Provider maintainer | Map all nine memory types onto required provider capabilities | Recorded local gate: Matrix links each memory type to SQL/vector/lexical/graph/model needs. [Nine-type provider matrix](MEMORY_TYPE_PROVIDER_MATRIX.md) | Completed for recorded scope; revalidate affected gates on change | PA-011 |
| PA-075 | DONE | P2 | Provider maintainer | Check semantic facts across selected backend combinations | Recorded local gate: Source grounding, dedup, contradictions and historical validity are preserved. [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-076 | DONE | P2 | Provider maintainer | Check working-memory overflow and persona behavior | Recorded local gate: Window limits and persona isolation survive provider combinations. [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-077 | DONE | P2 | Provider maintainer | Check episodic chronology, historical ingestion and sessions | Recorded local gate: Event time, sender IDs and session/conversation links remain inspectable. [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-078 | DONE | P2 | Provider maintainer | Check resource/document chunk lineage and retrieval | Recorded local gate: Chunks cite source assets and isolation/erasure follows lineage. [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-079 | DONE | P2 | Provider maintainer | Check procedural promotion and plan-cache validity | Recorded local gate: Only usable validated skills are returned and cached plans stay scoped. [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-080 | DONE | P2 | Provider maintainer | Check reflective memory provenance and depth limits | Recorded local gate: Parent trust, depth and tenant restrictions hold across retrieval paths. [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-081 | DONE | P2 | Provider maintainer | Check associative graph direction, time and entity ownership | Recorded local gate: Graph providers preserve endpoint identity and temporal filtering. [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-082 | DEPLOYMENT_CHECK | P2 | Prospective memory/worker maintainer | Check prospective watches and worker execution | Gate not complete; prior evidence: [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Watches do not duplicate work or fire across forbidden scopes | PA-063, PA-074 |
| PA-083 | DONE | P2 | Provider maintainer | Check shared-memory grants and revocation | Recorded local gate: Revocation removes visibility across lexical/vector/graph/cache paths. [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-084 | DONE | P2 | Provider maintainer | Trace one message through ingest, derive, offline and retrieval | Recorded local gate: Each stage records IDs, provider selection, outcome and evidence safely. [Persisted lifecycle trace](provider_verification/memory-lifecycle-trace.json); [debugging guide](MEMORY_LIFECYCLE_DEBUGGING.md) | Completed for recorded scope; revalidate affected gates on change | PA-074 |
| PA-085 | DONE | P2 | Provider maintainer | Define cross-provider retrieval relevance acceptance cases | Recorded local gate: Ranking changes are separated from correctness/isolation failures. [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Completed for recorded scope; revalidate affected gates on change | PA-075, PA-081 |
| PA-086 | DONE | P2 | Provider maintainer | Review multilingual and literal-query behavior | Recorded local gate: Languages, punctuation and query operators have documented expected results. [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Completed for recorded scope; revalidate affected gates on change | PA-085 |
| PA-087 | DONE | P2 | Provider maintainer | Review compression and token budgets by memory layer | Recorded local gate: Compression preserves evidence/date/identity and records token/quality tradeoffs. [Compression acceptance](provider_verification/compression-acceptance.json); lossy model quality remains PA-088 | Completed for recorded scope; revalidate affected gates on change | PA-084 |
| PA-088 | DEFERRED | P2 | Evaluation maintainer | Run clean model-quality evaluations when explicitly budgeted | Gate not complete; prior evidence: [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Owner selected 2026-10-22: no paid quality-evaluation budget; local-only evaluation when selected | PA-085, PA-087 |
| PA-089 | DONE | P3 | Provider maintainer | Evaluate native hybrid search as a separate future capability | Recorded local gate: Compare portable fusion fairly and avoid double-counting one native result. [Native hybrid diagnostic](provider_verification/native-hybrid-evaluation.json) | Completed for recorded scope; revalidate affected gates on change | PA-085 |
| PA-090 | DONE | P1 | Engineering lead | Review this tracker and select the next work package | Recorded local gate: PA-038/090/091 scope, gates and exclusions recorded; performance/deployment work stays separate. [Selected scope and milestones](PROVIDER_NEXT_MILESTONES.md) | Completed for recorded scope; revalidate affected gates on change | Owner-selected local scope |
| PA-091 | DONE | P2 | Engineering lead | Assign owners and target milestones to unresolved items | Recorded local gate: All 36 initially unresolved/dispositioned PA IDs have accountable roles, actions, gates, prerequisites and review dates. [Ownership and next reviews](PROVIDER_NEXT_MILESTONES.md) | Completed for recorded scope; revalidate affected gates on change | PA-090 |
| PA-092 | DONE | P2 | Provider maintainer | Create a concise breaking-configuration upgrade guide | Recorded local gate: Show only the new schema and explicit caller edits; no translation layer. [Breaking upgrade guide](PROVIDER_UPGRADE_GUIDE.md); [validated examples](provider_verification/upgrade-examples-validation.json) | Completed for recorded scope; revalidate affected gates on change | PA-031 |
| PA-093 | DONE | P2 | Provider maintainer | Check defaults/runbook/examples for drift during release | Recorded local gate (2026-10-17 working tree): read-only defaults, stage-parameter and config-snapshot references match (3/3). Runbook and examples/*.py not covered; no release revision recorded. [Read-only release reference gate](provider_verification/release-reference-check.json) | Completed for recorded scope; release-revision rerun including examples tracked in RM-30 | Prereq: PA-092; Feeds: RM-30 |
| PA-094 | DONE | P2 | Provider maintainer | Prepare a focused release summary and evidence index | Recorded local gate: selected-work delivery packet and evidence index (31 items). [Selected-work delivery and review packet](PROVIDER_PROPOSED_DELIVERY.md) | Completed for selected-work scope; release-candidate summary is part of PA-095 | Prereq: PA-093; Feeds: PA-095 |
| PA-095 | OPEN | P2 | Release maintainer | Prepare branch review and PR when requested | Gate not complete; prior evidence: [Local review packet; PR pending destination](PROVIDER_PROPOSED_DELIVERY.md) | Diff, verification and deployment prerequisites are reviewer-ready; refresh the packet rows for PA-048/055 with the perf-doc limits (partial/finite) | Prereq: PA-094, PA-039, RM-30; Feeds: PA-096 |
| PA-096 | NEEDS_INPUT | P2 | Release maintainer; authorization by repository owner | Publish or push only through the requested release workflow | 2026-10-22: branch restructure pushed to origin up to 80454dd0 (no PR) | Later local commits push on owner request; release workflow still separate | PA-095 |
| PA-097 | EXCLUDED | — | Storage architecture maintainer | Implement automatic authoritative database copying | Gate not complete; prior evidence: [PROVIDER_OPERATIONS.md](../docs/PROVIDER_OPERATIONS.md) | Excluded by owner; reopen only through a new explicit decision | None |
| PA-098 | EXCLUDED | — | Model/data architecture maintainer | Implement automatic historical re-embedding | Gate not complete; prior evidence: [PROVIDER_OPERATIONS.md](../docs/PROVIDER_OPERATIONS.md) | Excluded by owner; changing an embedder is not migration permission | None |
| PA-099 | EXCLUDED | — | Provider architecture maintainer | Restore backwards-compatible registries, aliases or RETIRED maps | Gate not complete; prior evidence: [PROVIDER_ARCHITECTURE_PLAN.md](../docs/PROVIDER_ARCHITECTURE_PLAN.md) | Direct breaking schema remains the agreed contract | None |
| PA-100 | EXCLUDED | — | Application acceptance owner | Claim production approval from local tests or basic health | Gate not complete; prior evidence: [PROVIDER_DEPLOYMENT_RUNBOOK.md](../docs/PROVIDER_DEPLOYMENT_RUNBOOK.md) | Production sign-off needs actual workload and operational evidence | None |
| PA-101 | DONE | P1 | Dependency / model maintainer | Audit current stable dependencies for Python 3.13 | Recorded local gate: 66 direct/build packages checked against public release metadata. [dependency-audit.json](../docs/provider_verification/dependency-audit.json) | Completed for recorded scope; revalidate affected gates on change | None |
| PA-102 | DONE | P1 | Dependency / model maintainer | Refresh direct minimums and the complete uv lock | Recorded local gate: Current compatible releases resolve without dependency overrides. [pyproject.toml](../pyproject.toml) | Completed for recorded scope; revalidate affected gates on change | PA-101 |
| PA-103 | DONE | P1 | Dependency / model maintainer | Validate a clean Python 3.13 install | Recorded local gate: 169 installed distributions pass uv pip check. [PYTHON313_DEPENDENCIES.md](../docs/PYTHON313_DEPENDENCIES.md) | Completed for recorded scope; revalidate affected gates on change | PA-102 |
| PA-104 | DONE | P1 | Dependency / model maintainer | Test updated provider contracts and DBOS 3.x recovery | Historical gate (pre-ADR-071): 581 passed, 5 skipped; DBOS 3.3 resume/parity 2 passed. DBOS has since been removed; current durable-runner evidence is live Temporal 9 passed. [PYTHON313_DEPENDENCIES.md](../docs/PYTHON313_DEPENDENCIES.md); [verification index](provider_verification/README.md) | Superseded by ADR-071; not revalidated. Temporal rerun is RM-16; container failover is ACD-11 (RM-27) | PA-103 |
| PA-105 | DONE | P1 | Dependency / model maintainer | Adapt stricter SQLAlchemy row typing | Recorded local gate: Distinct result shapes use distinct variables and mypy passes. [intake_store.py](../src/memspine/ingest/intake_store.py) | Completed for recorded scope; revalidate affected gates on change | PA-102 |
| PA-106 | DONE | P1 | Dependency / model maintainer | Use locked installations and requirement consistency checks in CI | Recorded local gate: CI workflow uses uv sync --locked and uv pip check. [ci.yml](../.github/workflows/ci.yml) | Completed for recorded scope; revalidate affected gates on change | PA-102 |
| PA-107 | DEFERRED | P1 | Evaluation maintainer | Migrate active evaluation generators and fixtures to the provider schema | Gate not complete; prior evidence: [tests](../evals/tests) | Resolve 185 failures and seven errors reproduced in the original environment; preserve immutable historical results | PA-031 |
| PA-108 | DONE | P2 | Dependency / model maintainer | Validate GLiNER2 local inference on Python 3.13 | Recorded local gate: Install the full local extra and test an explicitly budgeted model; base-only import is insufficient. [Real GLiNER runtime](GLINER25_LOCAL_RESULTS.md) | Completed for recorded scope; revalidate affected gates on change | PA-102 |
| PA-109 | NEEDS_TOOLCHAIN | P2 | Windows/native platform maintainer | Validate llama-cpp-python native builds on Windows 3.13 | Gate not complete; prior evidence: [PYTHON313_DEPENDENCIES.md](../docs/PYTHON313_DEPENDENCIES.md) | A supported toolchain builds the optional package and native inference passes | PA-102 |
| PA-110 | DONE | P2 | Dependency / model maintainer | Repeat native backend contracts with the refreshed lock | Recorded local gate: Record backend versions and selected operations again; earlier evidence remains historical. [Native refreshed lock](provider_verification/live-provider-refreshed-lock.json); [pgvector](provider_verification/pgvector-live-refreshed-lock.json) | Completed for recorded scope; revalidate affected gates on change | PA-102 |
| PA-111 | DONE | P2 | Dependency / model maintainer | Expand installed optional-extra/platform coverage | Recorded local gate: Document installed versus metadata-only support for heavy inference and native extras. [Expanded optional runtime matrix](PROVIDER_PLATFORM_MATRIX.md) | Completed for recorded scope; revalidate affected gates on change | PA-103 |
| PA-112 | DONE | P2 | Dependency / model maintainer | Replace deprecated pytest-asyncio loop-policy fixture | Recorded local gate: Use current loop-factory hook while preserving Windows/PostgreSQL loop requirements. [Loop factory audit](provider_verification/ASYNCIO_AND_SKIP_AUDIT.md); [combined regression](provider_verification/provider-followup-final-regression.log) | Completed for recorded scope; revalidate affected gates on change | PA-104 |
| PA-113 | OPEN | P2 | Dependency maintainer | Refresh dependency audit during future releases | Gate not complete; prior evidence: [audit_dependencies.py](../scripts/audit_dependencies.py) | Run the public metadata audit and review intentional version caps; no silent periodic job | PA-101 |
| PA-114 | DONE | P1 | Dependency / model maintainer | Keep community summary event time when historical data is derived | Recorded local gate: valid_from follows source members and the observed bundle wall-clock check passes. [communities.py](../src/memspine/derive/pipeline/stages/communities.py) | Completed for recorded scope; revalidate affected gates on change | PA-103 |
| PA-115 | DONE | P1 | Dependency / model maintainer | Finalize refreshed-dependency evidence and limitations | Recorded local gate: Record exact test results, original-environment comparison and unresolved optional-runtime gates. [PYTHON313_DEPENDENCIES.md](../docs/PYTHON313_DEPENDENCIES.md) | Completed for recorded scope; revalidate affected gates on change | PA-104, PA-114 |
| PA-116 | DONE | P1 | Dependency / model maintainer | Verify GLiNER2.5 package, architecture and checkpoint feasibility | Recorded local gate: Read SDK source and four pinned checkpoint configs; document limits without inference claims. [GLINER25_INGESTION_PLAN.md](../docs/GLINER25_INGESTION_PLAN.md) | Completed for recorded scope; revalidate affected gates on change | PA-101 |
| PA-117 | DONE | P1 | Dependency / model maintainer | Use AutoExtractor for GLiNER2.5 loading paths | Recorded local gate: Both semantic NER and decision paths can select boundary checkpoints without span-only loader selection. [Adapter test log](provider_verification/gliner25-adapters-tests.log) | Completed for recorded scope; revalidate affected gates on change | PA-116 |
| PA-118 | DONE | P1 | Dependency / model maintainer | Implement shared configurable GLiNER2.5 model ownership | Recorded local gate: Explicit checkpoint/revision/device/task settings and one bounded owned model per effective configuration. [Shared pool and wiring](GLINER_SHARED_RUNTIME.md); final focused evidence below | Completed for recorded scope; revalidate affected gates on change | PA-116 |
| PA-119 | DONE | P1 | Dependency / model maintainer | Preserve grounded GLiNER2.5 ingestion outputs | Recorded local gate: Keep source-message spans, sender ownership, confidence and mention/fact distinction through integration. [Grounding/storage contract](GLINER_GROUNDING_WINDOW.md); [ADR-076](adr/ADR-076-grounded-gliner-mention-ingestion.md) | Completed for recorded scope; revalidate affected gates on change | PA-117, PA-118 |
| PA-120 | DONE | P2 | Dependency / model maintainer | Add GLiNER2.5 typed record/relation extraction | Recorded local gate: Explicit schemas produce only validated grounded fact/graph candidates. [Typed source-cited adapter](GLINER25_STRUCTURED_EXTRACTION.md); [real runtime gate](provider_verification/gliner-structured-real.json) | Completed for recorded scope; revalidate affected gates on change | PA-119 |
| PA-121 | DONE | P1 | Dependency / model maintainer | Smoke-test real GLiNER2.5 local ingestion on Python 3.13 | Recorded local gate: Selected model loads and one message/window produces valid source-backed output. [Real Engine acceptance](GLINER_INGESTION_ACCEPTANCE.md); [offline result](provider_verification/gliner-ingestion-real.json) | Completed for recorded scope; revalidate affected gates on change | PA-117, PA-118, PA-123 |
| PA-122 | DONE | P2 | Dependency / model maintainer | Compare GLiNER2.5 Base and Small for ingestion | Recorded local gate: Measure extraction quality, latency and memory on held-out synthetic inputs; multilingual remains selectable. [Base/Small bounded comparison](GLINER25_LOCAL_RESULTS.md) | Completed for recorded scope; revalidate affected gates on change | PA-121 |
| PA-123 | DONE | P1 | Dependency / model maintainer | Verify GLiNER2.5 revision and offline-option propagation | Recorded local gate: Config, weights and tokenizer honor the same selected revision and offline policy, or load a verified local snapshot. [Loader policy](GLINER_SHARED_RUNTIME.md); [one actual offline Base load/release](provider_verification/gliner-ownership-local-load.json); no inference claim | Completed for recorded scope; revalidate affected gates on change | PA-116 |
| PA-124 | DONE | P2 | Dependency / model maintainer | Benchmark JSON alternatives and adopt only safe measured improvements | Recorded local gate: Actual payloads and edge-value behavior measured; current or faster path retained with reasons. [JSON alternatives](PERFORMANCE_ALTERNATIVES.md) | Completed for recorded scope; revalidate affected gates on change | PA-047 |
| PA-125 | DONE | P2 | Dependency / model maintainer | Benchmark CPU primitives and apply output-identical kernel optimization | Recorded local gate: Repeated kernel timings, byte/bit parity, bounded memory and focused tests; no full-engine performance run required. [Kernel alternatives](PERFORMANCE_ALTERNATIVES_NON_JSON.md); [ADR-073](adr/ADR-073-measured-simhash-kernel.md) | Completed for recorded scope; revalidate affected gates on change | PA-124 |
| PA-126 | DONE | P2 | Dependency / model maintainer | Add bounded RRF selection at safe fixed-prefix callers | Recorded local gate: Exact full-prefix score/order parity, all legs accumulated, full main-router tail preserved; large-pool kernel comparison. [Top-k results](BOUNDED_TOPK_IMPLEMENTATION.md); [ADR-074](adr/ADR-074-bounded-ranking-and-shared-gliner.md) | Completed for recorded scope; revalidate affected gates on change | PA-125 |
| PA-127 | DONE | P2 | Dependency / model maintainer | Batch MinHash updates without changing stored sketches | Recorded local gate: Bounded temporary matrices, exact signature/reconstruction/LSH fixture parity and repeated kernel measurements. [MinHash results](MINHASH_BATCH_KERNEL.md) | Completed for recorded scope; revalidate affected gates on change | PA-125 |
| PA-128 | DONE | P2 | Dependency / model maintainer | Evaluate real cached embedding batch sizes | Recorded local gate: Offline model-only kernel with fixed inputs, vector parity, bounded repetitions and explicit default decision. [Real ONNX batch kernel](EMBEDDING_BATCH_KERNEL.md); cached files staged temporarily because standard snapshot resolution failed; keep defaults | Completed for recorded scope; revalidate affected gates on change | PA-125 |
| PA-129 | DONE | P2 | Dependency / model maintainer | Isolate SQLite import and connection startup cost | Recorded local gate: Repeated fresh-process standalone client timings; distinguish deferred imports from removed work. [SQLite results](SQLITE_STARTUP_KERNEL.md); keep existing implementation | Completed for recorded scope; revalidate affected gates on change | PA-125 |
| PA-130 | DONE | P1 | Dependency / model maintainer | Define strict named model profiles and task bindings | Recorded local gate: One profile contract for LLM and GLiNER, validated references/capabilities and no old models.gliner alias. [ADR-075](adr/ADR-075-named-model-profiles-and-escalation.md); [configuration checks](provider_verification/model-profiles-config-focused.log) | Completed for recorded scope; revalidate affected gates on change | PA-118 |
| PA-131 | DONE | P1 | Dependency / model maintainer | Route GLiNER consumers to independently selectable profiles | Recorded local gate: Classifier, semantic and prepass can differ; identical settings load once; pool capacity/lifecycle explicit. [Shared GLiNER runtime](GLINER_SHARED_RUNTIME.md); [focused checks](provider_verification/model-profiles-gliner-focused.log) | Completed for recorded scope; revalidate affected gates on change | PA-130 |
| PA-132 | DONE | P1 | Dependency / model maintainer | Bind generative roles to named provider profiles | Recorded local gate: Different models/settings per role, clear precedence versus inline roles and tier planning. [Model guide](MULTI_MODEL_PROFILES.md); [LLM checks](provider_verification/model-profiles-llm-focused.log) | Completed for recorded scope; revalidate affected gates on change | PA-130 |
| PA-133 | DONE | P1 | Dependency / model maintainer | Implement bounded opt-in escalation between model profiles | Recorded local gate: Explicit failure/empty/invalid-JSON triggers, bounded attempts/deadline/output settings, cancellation and failure tests. [Escalation policy](MULTI_MODEL_PROFILES.md); [LLM checks](provider_verification/model-profiles-llm-focused.log) | Completed for recorded scope; revalidate affected gates on change | PA-132 |
| PA-134 | DONE | P2 | Dependency / model maintainer | Separate deployment prerequisites from input and toolchain needs | Recorded local gate: Pending rows identify the actual prerequisite; production acceptance does not block local development. 19 DEPLOYMENT_CHECK, 3 NEEDS_INPUT, 1 NEEDS_TOOLCHAIN; checks remain unexecuted | Completed for recorded scope; revalidate affected gates on change | None |
| PA-135 | DONE | P1 | Dependency / model maintainer | Verify profile routing, resource sharing and escalation | Recorded local gate: Focused config/runtime/DI tests, slim optional imports, lint/type checks and regenerated references. [Focused results](MULTI_MODEL_PROFILES.md); no full-engine performance benchmark | Completed for recorded scope; revalidate affected gates on change | PA-131, PA-133 |
| PA-136 | DONE | P2 | Dependency / model maintainer | Document model profiles, reasoning levels and configuration examples | Recorded local gate: Executable current-schema examples and an updated decision register/todo result. [ADR-075](adr/ADR-075-named-model-profiles-and-escalation.md); [guide/example](MULTI_MODEL_PROFILES.md) | Completed for recorded scope; revalidate affected gates on change | PA-135 |
| BI-01 | IN_PROGRESS | P1 | Architecture / ingestion maintainer | Contracts and configuration | Owner selections 2026-10-21 recorded: Q1 A, Q2 B (53/44), Q3 refined A (shared typed roles), Q4 B (cancel/requeue), Q5 B (embedding/vector required). [Contract preparation](BI01_CONTRACT_REWRITE_PLAN.md); focused implementation started; no full envelope cutover or BI promotion claimed 2026-10-22: pure packaged semantic foundation (53 predicates/44 keys/shared role specs), 23 focused tests; Ruff and mypy pass. [foundation evidence](provider_verification/bi01-semantics-final-state-2026-10-22.json) | Complete canonical RoleBinding/typed-envelope integration, role-slot cardinality/evidence validation and F1..F5 gates; implement cancel/requeue and required embedding/vector enforcement through coordinated cutover. Foundation is not BI completion | Prereq: none (first buildable package); Feeds: BI-02..BI-12 |
| BI-02 | PLANNED | P1 | Ingestion / storage maintainer | Preserve raw acceptance | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Identical retry, conflicting ID, historical time, parent mismatch, policy exclusion and interrupted application cases | Prereq: BI-01; Feeds: BI-03, BI-12; Note: BI-02 and BI-03 must land before any background default |
| BI-03 | PLANNED | P1 | Runtime / storage maintainer | Durable discovery and queue | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Kill/reopen between raw append, job discovery, cursor commit and acknowledgement; no missing or duplicate work | Prereq: BI-01, BI-02 (inferred: scanner reads BI-02 manifests); Feeds: BI-04, BI-07, BI-09, BI-12; Note: must land before any background default |
| BI-04 | PLANNED | P1 | Ingestion maintainer | Isolated window formation | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Interleaved users/conversations, roots/replies, late history, idle close, partial windows after restart | Prereq: BI-01, BI-03 (inferred from handoff order); Feeds: BI-12 |
| BI-05 | PLANNED | P1 | Model / grounding maintainer | Grounded mention stage | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Repeated/Unicode spans, unknown confidence, malformed SDK data, source mutation, forged sender and empty-result clearing | Prereq: BI-01; Feeds: BI-07, BI-08, BI-11, BI-12; Note: can run in parallel with the grounded fact stage |
| BI-06 | PLANNED | P1 | Semantic / grounding maintainer | Grounded fact stage | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Unsupported quote/relation rejected; assistant proposal not promoted to user fact; exact raw offsets; temporal precision, corrections, negation and zero-generative-call cases | Prereq: BI-01, RM-22, RM-23 (inferred); Feeds: BI-07, BI-11, BI-12; Note: can run in parallel with the mention stage |
| BI-07 | PLANNED | P1 | Runtime / semantic maintainer | Fenced effect commit | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Two workers, expired lease, crash after some effects, replay and forget-during-inference; stale worker cannot commit | Prereq: BI-01, BI-03 (inferred from handoff order); Feeds: BI-08, BI-11, BI-12; Note: commits the mention and fact stage effects |
| BI-08 | PLANNED | P1 | Graph maintainer | Mention graph projection | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Repeated occurrences survive; namespace isolation, replay equality, source erasure and no false asserted paths on SQLite graph plus provider contract fixtures; includes RM-41 typed edge qualifiers (temporal, domain, provenance, assertion status, trust, scope, sensitivity, context) with filtered traversal | Prereq: BI-05, BI-07; Feeds: BI-12; Note: carries the RM-41 typed edge-qualifier contract |
| BI-09 | PLANNED | P1 | API / observability maintainer | Readiness and controls | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Raw success with derived pending/failed, disabled layers, zero-result completion, timeout and retry status | Prereq: BI-01, BI-03 (inferred from handoff order); Feeds: BI-12; Note: reports the mention, fact, commit and graph layer states |
| BI-10 | PLANNED | P1 | Storage / retention maintainer | Recoverable compression | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Exact UTF-8 round-trip/fingerprint, cold-source extraction, corruption loud failure, legal hold, restart and no forgotten-source resurrection | Prereq: BI-01; Feeds: BI-12; Note: its cold-source gate exercises the mention and fact stages |
| BI-11 | PLANNED | P1 | Offline / memory maintainer | Separate summaries | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Summary absence/failure does not invalidate grounded facts; raw support still accessible; scope/trust, zero-generative-call profile and token limits tested | Prereq: BI-05, BI-06, BI-07, separate owner selection; Feeds: none; Note: not required for integration and promotion |
| BI-12 | PLANNED | P1 | Verification / engineering lead | Integration and promotion | Design and reusable building blocks documented ([background design](BACKGROUND_INGESTION_PLAN.md)); BI package not implemented | Focused suites and bounded cached-model correctness gate pass; no full benchmark or broad quality claim | Prereq: BI-02..BI-10, RM-22 (inferred from handoff section 8); Feeds: application profile promotion; Note: separate summaries are excluded |
| DS-01 | DONE | P1 | Window/runtime maintainer | Staged windows retained across enqueue errors and cancellation | Acknowledged prefix retained; unaccepted suffix restored ahead of concurrent staging; accepted work still woken. 11 new recovery regressions; combined focused gate: 185 passed. [verification](provider_verification/ds-fixes-final-state-2026-10-21.json) | Completed local enqueue/cancellation scope. Open-window crash discovery remains BI-03/BI-04; provider acceptance ambiguity remains at-least-once | Prereq: none (DONE); Feeds: BI-03, BI-04 |
| DS-02 | DONE | P1 | Runtime maintainer | Bound direct readiness waits with a cooperative monotonic deadline | Deadline now encloses inline drain, polling and barriers; nonpositive timeout snapshots without draining; pending work remains retryable. Combined focused gate: 185 passed. [verification](provider_verification/ds-fixes-final-state-2026-10-21.json) | Completed cooperative async deadline scope; blocking or cancellation-suppressing providers need their own native deadlines | Prereq: none (DONE); Feeds: BI-09 |
| DS-03 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Current window construction still requires generative extraction | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | A profile with no LLM role produces grounded allowed candidates/annotations or explicit unresolved outcomes without losing the whole stage. | Prereq: none; Feeds: BI-01, BI-06 |
| DS-04 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Window prepass annotations are attached through surviving assertion envelopes | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Zero facts, rejected facts and uncited source with valid mentions all yield correct source annotation readiness. | Prereq: none; Feeds: BI-05, BI-07 |
| DS-05 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Raw acceptance and window observation are separate failure boundaries | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Kill/fail after raw append before observation; eventual required job exists exactly once. | Prereq: none; Feeds: BI-02, BI-03 |
| DS-06 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Open window membership is volatile before a cut | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Restart after each of the first five turns of a six-turn window, plus idle/session-close cuts. | Prereq: none; Feeds: BI-03, BI-04 |
| DS-07 | OPEN_REVIEW | P2 | Owning subsystem maintainer | One namespace window store serialises interleaved conversation/session state | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Interleaved A1/B1/A2 with separate conversations and sessions; no false cross-session close or mixed support. | Prereq: none; Feeds: BI-04 |
| DS-08 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Civil source date accepted and naive time can be localised | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Date-only, naive datetime, aware offset, generated timestamp and month/year precision variants preserve their supported meaning. | Prereq: none; Feeds: BI-01, BI-02, BI-06 |
| DS-09 | OPEN_REVIEW | P2 | Owning subsystem maintainer | Unknown message fields can conceal ID mistakes | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Misspelled identity/time/sender fields fail clearly; accepted identical message_id retries remain idempotent. | Prereq: none; Feeds: BI-01, BI-02 |
| DS-10 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Name-keyed graph nodes are not identity-safe mention occurrences | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Two different Alex identities and repeated identical spans survive replay without false merges or asserted traversal. | Prereq: none; Feeds: BI-01, BI-08 |
| DS-11 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Known-entity prompt context lacks the target evidence manifest | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Later-known alias and erased/revoked sources cannot create unsupported bindings or leak into historical views. | Prereq: none; Feeds: BI-01, BI-04, BI-06 |
| DS-12 | OPEN_REVIEW | P2 | Owning subsystem maintainer | Output prefill caps do not bound candidate collection work | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Large entity sets and many namespaces respect work/memory limits without treating a capped set as globally unique. | Prereq: none; Feeds: BI-04, BI-09 |
| DS-13 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Live optional fact bundles request at most two source turns | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Three/four-source counts, origin binding plus time, and correction chains retain all required support or explicitly abstain. | Prereq: none; Feeds: BI-01, BI-09 |
| DS-14 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Benchmarked fitter is not the live Engine assembly implementation | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Parity on rendered token cost, dependency closure, caps, known-time and inaccessible/missing sources with the actual runtime packer. | Prereq: none; Feeds: BI-09, BI-12 |
| DS-15 | OPEN_REVIEW | P1 | Owning subsystem maintainer | AssemblyPolicy can admit an oversized first record | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | One oversized record, leading headers, stable prefix and compression on/off all obey the final budget or explicit failure contract. | Prereq: none; Feeds: BI-09, BI-12 |
| DS-16 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Copied-excerpt erasure policy needs expanded surface coverage | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Forget during inference, copied excerpts/caches/logs, restart and restored backup cannot re-authorise erased source. | Prereq: none; Feeds: BI-01, BI-07, BI-10 |
| DS-17 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Maintenance Temporal runner does not automatically execute ingestion outbox jobs | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Activity retries/reopen/reassignment, idempotent effects and journal-volume loss are tested with documented recovery limits. Covers ACD-04/07: one durable outbox-to-Temporal route designed with BI-02/03; directive section 8 tests (crash after business write, interrupt during LLM call, old-history versioning) | Prereq: none; Feeds: BI-03, BI-07, BI-12 (Temporal adapter part lands with BI-03) |
| DS-18 | OPEN_REVIEW | P2 | Owning subsystem maintainer | Real GLiNER ingestion evidence uses a deterministic window-LLM fixture | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Actual configured non-generative path handles positive and abstention cases with exact spans and zero generation. | Prereq: none; Feeds: BI-05, BI-06, BI-12 |
| DS-19 | OPEN_REVIEW | P1 | Owning subsystem maintainer | Accepted typed schema and predicate catalog are not integrated contracts yet | Baseline reproduced/code traced; [review evidence](DEEP_SCAN_REVIEW_2026-10-21.md). No fix selected/completed | Structural and behavioural contracts agree for all five synthetic scenarios, typed values, authority, precision and unresolved records. | Prereq: RM-22 for the scenario gate; Feeds: BI-01, BI-06, BI-07 |
| DS-20 | OPEN_REVIEW | P2 | Owning subsystem maintainer | Large uncommitted working tree weakens replay of historical verification | Baseline reproduced 2026-10-21. 2026-10-22: working tree committed locally (RM-01), so later verification can record exact revisions; historical evidence still cites dirty-tree hashes | Verification records identify exact revision or working-tree hashes, selected commands, failures/skips and output artifacts | Prereq: RM-01; Feeds: BI-12, PA-039 |
| DS-21 | DONE | P1 | Semantic/rule maintainer | Prevent unsupported attribution and modality in rule candidates | Conservative guards reject quoted, reported, conditional, uncertain and alternative assertions across rule families; direct positives retained. 87 rule tests; combined focused gate: 185 passed. [verification](provider_verification/ds-fixes-final-state-2026-10-21.json) | Completed tested English rule scope; unresolved annotation persistence and canonical sender-bound derivation remain BI-06 | Prereq: none (DONE); Feeds: BI-06 |
| RS-01 | DONE | P2 | Research / retrieval maintainer | LoCoMo structural/evidence research | 1,986-question inventory and 52 generic requirements recorded; [gap register](NON_LLM_GAP_REGISTER.md) | No full non-generative answer-quality claim | Prereq: none (DONE); Feeds: BI-01, BI-06, BI-09, BI-12 as applicable |
| RS-02 | DONE | P2 | Research / retrieval maintainer | Target schema shape review | 41 definitions, 15 positive examples, 12 negative checks; [contracts](ENRICHMENT_SCHEMA_CONTRACTS.md) | Runtime integration belongs to BI-01; this item is shape research only | Prereq: none (DONE); Feeds: BI-01, BI-06, BI-09, BI-12 as applicable |
| RS-03 | DONE | P2 | Research / retrieval maintainer | CPU non-generative model probes | Bounded candidate/extractor/reranker kernels; [results](CPU_NON_LLM_BENCHMARK_RESULTS.md) | No global best-in-class or production promotion | Prereq: none (DONE); Feeds: BI-01, BI-06, BI-09, BI-12 as applicable |
| RS-04 | DONE | P2 | Research / retrieval maintainer | Rank48 and 2K/3K evidence kernels | Six methods, 288 length/query conditions; 1,728 replayed packing outcomes; [results](CPU_EVIDENCE_BUDGET_BENCHMARK.md) | Measured fixture only; actual automatic linking/trimming remains open | Prereq: none (DONE); Feeds: BI-01, BI-06, BI-09, BI-12 as applicable |
| RS-05 | OPEN | P2 | Research / retrieval maintainer | Independent natural-message and automatic-link quality | Synthetic kernels complete; correct links/spans were supplied | Hold out natural positive/ambiguous/negative cases; measure factual support and abstention | Prereq: none; Feeds: BI-06, BI-12 |
| RS-06 | OPEN | P2 | Research / retrieval maintainer | Actual runtime packer and deployment performance envelope | Standalone fitter measured; [review](DEEP_SCAN_REVIEW_2026-10-21.md) | Actual packer, reader token counter, thread/batch sweep, contention and optional ONNX/int8 parity | Prereq: none; Feeds: BI-09, BI-12, PA-051 |
| RS-07 | PROPOSED | P2 | Research / retrieval maintainer | Obligation-aware packing / immutable token-cost cache / read-time span dedup | Proposal only; no durable copied-excerpt semantic change | Select a bounded experiment and demonstrate dependency retention/cost benefit | Prereq: RS-04; Feeds: BI-09 |
| RS-08 | IN_PROGRESS | P2 | Research / retrieval maintainer | Vocabulary/semantic structure extensions | BI-Q2 B selects all 53 predicates/44 property keys; BI-Q3 refined A selects shared typed roles. Selection is not runtime extraction coverage 2026-10-22: pure packaged semantic foundation (53 predicates/44 keys/shared role specs), 23 focused tests; Ruff and mypy pass. [foundation evidence](provider_verification/bi01-semantics-final-state-2026-10-22.json) | Complete canonical RoleBinding/typed-envelope integration, role-slot cardinality/evidence validation and F1..F5 gates; implement cancel/requeue and required embedding/vector enforcement through coordinated cutover. Foundation is not BI completion | Prereq: none; Feeds: RM-23, BI-06 |
| RS-09 | IN_PROGRESS | P2 | Research / retrieval maintainer | Optional model and 2K/3K profile selection, including reranker role (none, TinyBERT or MiniLM) beside the accepted 4K budget | Owner selected on 2026-10-22: evidence budget configurable with 4K default; reranker a configurable stage (none, TinyBERT or MiniLM); default reranker not yet selected | Expose budget and reranker selection in read config; select the default reranker with BI-09/BI-12 evidence | Prereq: RS-03, RS-04; Feeds: BI-06, BI-09 |
| RM-01 | DONE | P1 | Owner decision | **[owner]** Authorize committing the working tree to `restructure` (no push). The plan docs say no commit is implied by approval of a document. | 2026-10-22: committed locally on restructure, no push: 5f8f60d0 format, then C1 d32e8f7d deps, C2 839dc346 Temporal, C3 53a0953c kernels, C4 47526490 model profiles, C5 eec8b286 GLiNER mentions, C6 d151616f fixes, C7 f290bdec verification, C8 d2abc077 semantics/design, C9 51b43af7 docs. Files grouped whole; engine.py/container.py carry mixed themes. Focused changed tests before commit: 591 passed, 13 skipped, 7 failed only on missing [cache]/[graph] extras in the unsynced .venv | Completed; push stays a separate owner decision (PA-096) | Prereq: none; Feeds: RM-03, RM-15, PA-039 |
| RM-02 | NEEDS_INPUT | P1 | Owning maintainer | **[local]** `uv self update` (>= 0.13.1), then `uv sync --locked --extra dev`. | Revalidated 2026-10-22: two uv installs, Anaconda 0.8.0 first on PATH and winget 0.12.5; both below required >=0.13.1 and self update is unsupported for both. .venv lacks [cache]/[graph] extras, causing 7 focused-test failures | Owner upgrades uv (for example winget upgrade astral-sh.uv) and removes or reorders the Anaconda copy on PATH; then uv sync --locked --extra dev plus needed extras, uv pip check | Prereq: none; Feeds: RM-03, RM-15, RM-16 |
| RM-03 | DONE | P1 | Owning maintainer | **[local]** Format-only commit first (`ruff format`), so later diffs are readable. | 2026-10-22: 5f8f60d0 formats 326 committed files with locked ruff 0.17.1 (the .venv ruff was aligned from 0.17.0); remaining changed files formatted inside their themed commits | Completed; ruff format --check passes repo-wide | Prereq: RM-01, RM-02; Feeds: RM-15 |
| RM-04 | DONE | P2 | Owner decision | **[owner]** Decide the fate of ~190 `docs/provider_verification/*.log/*.json` files: commit, prune, or `.gitignore`. | Owner selected commit all on 2026-10-22: every docs/provider_verification log/json file is committed with the C7 verification chunk | Completed decision; the commit itself is tracked in RM-01 (C7) | Prereq: none; Feeds: RM-01 (C7 commit) |
| RM-05 | DONE | P2 | Owning maintainer | **[local]** Fix the 166 ruff errors in `evals/bench/` and `docs/schema_review/` before C8, or exclude those research paths in ruff config. | 2026-10-22: ruff check passes repo-wide; formatting cleared E701/E702, safe fixes applied, UP028 rewritten, one documented per-file B023 ignore for evals/bench/cpu_evidence_budget_kernels.py | Completed | Prereq: none; Feeds: RM-01 (C8 commit) |
| RM-06 | DONE | P2 | Owning maintainer | **[local]** CLAUDE.md: test count, ADR count/range (ADR-001..078), register range (D-100), `workers/pipelines.py` path (now `derive/pipeline/stages/table.py`), the `-p no:deepeval` rule (DeepEval removed), Cache/KV "LMDB" (default is `memory`), entities/gliner2 row (now `models.profiles`/`bindings`, gliner2.5), `uv sync --all-extras` vs locked baseline, repo layout (ADR-067 tree). | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-07 | DONE | P2 | Owning maintainer | Make STATUS.md a pointer to the single live tracker, or a generated read-only view | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-08 | DONE | P2 | Owning maintainer | **[local]** README: ADR count; OpenSearch/Weaviate are shipped extras, not "planned". | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-09 | DONE | P2 | Owning maintainer | **[local]** CHANGELOG entries for ADR-068/069/070/072-076. | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-10 | DONE | P2 | Owning maintainer | **[local]** ADR status lines: ADR-068 and ADR-071 ("in progress" → implemented, locally verified); ADR-070 superseded-by-ADR-071 note on DBOS; ADR-074 superseded-by-ADR-075 note on `models.gliner`; ADR-066 needs its register row. | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-11 | DONE | P2 | Owning maintainer | **[local]** PROVIDER_ARCHITECTURE_PLAN.md stale sections: §2 current-code facts, §11 pre-implementation boxes, "no commit performed" lines. | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-12 | DONE | P2 | Owning maintainer | **[local]** Stale-in-place sections in performance and enrichment docs (section 6). | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-13 | DONE | P2 | Owning maintainer | **[local]** DBOS references in active `docs/v2` briefs/areas/plan.json/tracker.html. | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-14 | DONE | P2 | Owning maintainer | **[local]** Fix UTF-8 mojibake in CPU_NON_LLM_BENCHMARK_RESULTS.md. | 2026-10-22: fixed in 7956e1af (docs-only, 54 files); ruff clean | Completed; follow-ups recorded as RM-64..RM-70 where something was left | Prereq: none; roadmap Phase B: make the docs true |
| RM-15 | BLOCKED | P2 | Owning maintainer | **[local]** Chunked full unit run with `-p no:deepeval`, on the committed revision; record with `scripts/record_verification_run.py`. | Revalidated 2026-10-21: deepeval absent from uv.lock but still installed in .venv, so -p no:deepeval stays needed until RM-02 sync. Needs RM-01 commit first. Last full collection: 7,910 tests. | Owner selected 2026-10-22: run after RM-02 uv upgrade and extras sync; chunked full unit run recorded with scripts/record_verification_run.py | Prereq: RM-01, RM-02; Feeds: PA-039 |
| RM-16 | REVIEW | P2 | Owning maintainer | **[local]** Rerun live Temporal gate (`MEMSPINE_TEST_TEMPORAL_ADDRESS`) and the strict native Docker gate on the same revision. | Revalidated 2026-10-21: claim still holds; not executed | Review scope/evidence and select explicitly; keep PA-107 deferred and no broad sweep/deploy/commit implied | Prereq: none; roadmap Phase C: re-establish a verified baseline |
| RM-17 | DONE | P2 | Owning maintainer | **[local]** Close skip-review P2 milestones M-MODEL, M-BRANCH, M-LIVE, M-PLAN (review date 2026-10-25). | 2026-10-22: a94f79d3 closes M-MODEL (cache helpers only tolerate the absent package), M-BRANCH (7 negative import-gate tests simulate absence instead of skipping), M-LIVE (opted-in Postgres unreachability fails), M-PLAN (synthetic plan exercises the forbidden-owner branch). Focused 112 passed, 8 legitimate skips | Completed; live Postgres opt-in path checked by reading only. Unrelated skip-audit drift is RM-65 | Prereq: none; roadmap Phase C: re-establish a verified baseline |
| RM-18 | DONE | P2 | Owning maintainer | **[local]** Scoped review of learned-alias isolation (engine-wide, not namespace-scoped); see also DS-11 (alias leak through known-entity context). | 2026-10-22: 6fba3c69 scopes learned aliases per namespace (hints, KNOWN prompt context, unseen-entity check, flush, reload) and adds learned.alias.reset on namespace erasure. Leak reproduced first; test_rm18_alias_scope 7 passed | Completed; aliases still lack source evidence links (DS-11); pre-fix logs reviewed under RM-66 | Prereq: none; roadmap Phase C: re-establish a verified baseline |
| RM-19 | DEFERRED | P2 | Owning maintainer | **[local]** `evals/run_v2_batch.sh`: drop `--extra dbos`. | Revalidated 2026-10-21: evals/run_v2_batch.sh:7 still passes --extra dbos; pyproject has no dbos extra. | Review scope/evidence and select explicitly; keep PA-107 deferred and no broad sweep/deploy/commit implied | Prereq: none; roadmap Phase D: unblock evals (small, high value) |
| RM-20 | DEFERRED | P2 | Owning maintainer | **[local]** `evals/memspine_evals/multiagent/constructions.py:47`: nested storage settings. | Revalidated 2026-10-21: constructions.py:47 uses flat storage path; StorageConfig forbids extra keys and accepts only profile, provider, settings. | Move path to storage.settings | Prereq: none; roadmap Phase D: unblock evals (small, high value) |
| RM-21 | DEFERRED | P2 | Owning maintainer | **[local]** Mechanical arm migrator: flat → `provider`/`settings`, `rerank_model` → rerank settings, add `provider` to LLM roles. Historical run records stay untouched. | Revalidated 2026-10-21: 184 arm files; 134 still use rerank_model; flat storage path still widespread. | Review scope/evidence and select explicitly; keep PA-107 deferred and no broad sweep/deploy/commit implied | Prereq: none; roadmap Phase D: unblock evals (small, high value) |
| RM-22 | IN_PROGRESS | P2 | Owning maintainer | Fixtures F1..F5 as executable schema + behaviour tests with negative/unresolved variants. | Owner selections 2026-10-21 recorded: Q1 A, Q2 B (53/44), Q3 refined A (shared typed roles), Q4 B (cancel/requeue), Q5 B (embedding/vector required). [Contract preparation](BI01_CONTRACT_REWRITE_PLAN.md); focused implementation started; no full envelope cutover or BI promotion claimed | Implement selected pure contracts/registry and focused fixtures, then coordinate compiler/gateway/storage/projector/read cutover without aliases or dual writers | Prereq: none; Feeds: BI-06, BI-12, DS-19 |
| RM-23 | IN_PROGRESS | P2 | Owning maintainer | Pure semantic registry with 53 predicates / 44 property keys, shared role definitions and gated extraction | Owner selections 2026-10-21 recorded: Q1 A, Q2 B (53/44), Q3 refined A (shared typed roles), Q4 B (cancel/requeue), Q5 B (embedding/vector required). [Contract preparation](BI01_CONTRACT_REWRITE_PLAN.md); focused implementation started; no full envelope cutover or BI promotion claimed 2026-10-22: pure packaged semantic foundation (53 predicates/44 keys/shared role specs), 23 focused tests; Ruff and mypy pass. [foundation evidence](provider_verification/bi01-semantics-final-state-2026-10-22.json) | Complete canonical RoleBinding/typed-envelope integration, role-slot cardinality/evidence validation and F1..F5 gates; implement cancel/requeue and required embedding/vector enforcement through coordinated cutover. Foundation is not BI completion | Prereq: none; Feeds: BI-06 |
| RM-24 | IN_PROGRESS | P2 | Owning maintainer | Q-S1-B is a coordinated breaking rewrite (compiler, gateway, storage, projectors, graph, readers, fixtures). Plan it as one cut-over. | Owner selections 2026-10-21 recorded: Q1 A, Q2 B (53/44), Q3 refined A (shared typed roles), Q4 B (cancel/requeue), Q5 B (embedding/vector required). [Contract preparation](BI01_CONTRACT_REWRITE_PLAN.md); focused implementation started; no full envelope cutover or BI promotion claimed | Implement selected pure contracts/registry and focused fixtures, then coordinate compiler/gateway/storage/projector/read cutover without aliases or dual writers | Prereq: none; roadmap Phase E: background ingestion BI-01..12 (main feature work) |
| RM-25 | REVIEW | P2 | Owning maintainer | **[local]** Repair the HF cache snapshot, rerun embedding batching from a clean cache. | Revalidated 2026-10-21: claim still holds; not executed | Review scope/evidence and select explicitly; keep PA-107 deferred and no broad sweep/deploy/commit implied | Prereq: none; roadmap Phase F: performance follow-ups (need owner budgets) |
| RM-26 | REVIEW | P2 | Owning maintainer | **[local]** CPU model envelope: thread sweep, batch variants, ONNX/int8 parity. | Revalidated 2026-10-21: claim still holds; not executed | Review scope/evidence and select explicitly; keep PA-107 deferred and no broad sweep/deploy/commit implied | Prereq: none; roadmap Phase F: performance follow-ups (need owner budgets) |
| RM-27 | REVIEW | P2 | Deployment maintainer | **[deploy]** ACD-09 (check for old DBOS jobs), ACD-11 (volume failover drill), ACD-12 (canary rollout). | Revalidated 2026-10-21: claim still holds; not executed | Review scope/evidence and select explicitly; keep PA-107 deferred and no broad sweep/deploy/commit implied | Prereq: none; roadmap Phase G: release and deployment |
| RM-28 | DONE | P1 | Claude / Codex / maintainers | Consolidate all current task statuses into one shared register | Provider, BI, DS, research and roadmap items mapped to unique IDs; completed work, current status, owner and next gate recorded. Duplicate handoff snapshot removed; read-only HTML generated from this file | Completed register consolidation; all future status edits stay here and regenerate the read-only view with scripts/refresh_tracker.py | None |
| RM-29 | REVIEW | P2 | Provider maintainer (performance) | Extend the lifecycle resource soak beyond 30 cycles (follow-up previously folded into DONE PA-055) | Not started; PA-055 covered a finite 20.8 s local run | Fresh-process soak on a recorded clean revision; cycle count or wall time and RSS-slope, handle and connection guards declared before running; zero open connections after each stop | Prereq: PA-055, RM-01; Feeds: PA-051, RM-31 |
| RM-30 | PROPOSED | P2 | Release maintainer | Revalidate the release candidate: installed-wheel smoke (core and 3 extras), read-only reference check, runbook and examples/*.py execution | Not started; PA-042 and PA-093 evidence predates final sources | Owner selected 2026-10-22: run at release time on the release-candidate revision | Prereq: PA-039, PA-042, PA-093; Feeds: PA-095 |
| RM-31 | PROPOSED | P3 | Platform reliability maintainer | Check remote-provider connection-pool growth over repeated lifecycles (PA-055 covered local providers only) | Not started | Owner selected 2026-10-22: run with deployment checks alongside PA-069 | Prereq: PA-054, RM-29; Feeds: PA-072 |
| RM-32 | DONE | P2 | Claude / maintainers | Tracker consistency pass: status legend, Prereq/Feeds dependency labels, BI ordering, mislabelled DONE dependencies, imported-roadmap remapping, missing decision rows, glued-number typos | 2026-10-21: four read-only review agents re-checked evidence; changes applied to this register and the refresh_tracker.py summary text; independent review findings (12) applied; duplicate H heading renamed and stale next-sequence marked historical; scripts/refresh_tracker.py --check passes | Completed for the register; PA-048/055 wording in PROVIDER_PERFORMANCE_FOLLOWUP.md and the delivery packet stays under RM-12 and PA-095 | Prereq: RM-28; Feeds: RM-12, PA-095 |
| RM-33 | DONE | P1 | Owner decision; model / ingestion maintainer | Select the default GLiNER2.5 checkpoint for ingestion: Base (current code default) or Small (proposed) | Owner selected on 2026-10-22: keep GLiNER2.5 Base (fastino/gliner2.5-base-v1) as the ingestion default for now; Small stays an explicit selectable profile. Evidence: local fixture Base F1 0.9796 vs Small 0.9583, warm p50 94.4 vs 31.8 ms. [fixture report](CPU_NON_LLM_BENCHMARK_RESULTS.md) | Completed decision; revisit only with held-out grounding/ambiguity evidence from BI-05/BI-06 fixtures | Prereq: PA-122, RS-03; Feeds: BI-05, BI-06, RS-09 |
| RM-34 | IN_PROGRESS | P1 | Owner decision; architecture / ingestion maintainer | Select the representation for unresolved or provisional entities | BI-Q1 A selected: unresolved source-bound Mention; canonical entity_id null until supported identity. Exact schema/negative fixtures belong to BI-01 | Implement unresolved/provisional/contested identity invariants and focused negative tests; do not infer entity identity from similarity | Prereq: RS-02; Feeds: BI-01, BI-08, DS-10, DS-19 |
| RM-35 | DONE | P2 | Owner decision; ingestion / storage maintainer | Decide whether SQL-only (no-embedding) ingestion is a supported capability | BI-Q5 B owner decision recorded 2026-10-21: SQL-only public ingestion is not supported; every ingestion profile requires embedding/vector capability. Decision only, not implemented profile enforcement | Decision complete; enforce capabilities through BI-01/02 and focused profile tests | Prereq: none; Feeds: BI-01, BI-02 |
| RM-36 | IN_PROGRESS | P2 | Owner decision; ingestion maintainer | Decide whether a reply to a stored parent inherits the parent session when the session is omitted | Owner selected A on 2026-10-22: omitted session inherits the authorised stored parent session and conversation. Refined 2026-10-22: an explicit session that differs from the stored parent session is accepted in the supplied session, keeps reply_to as a cross-session link and starts a new conversation there. Current code rejects stored-parent omission; same-batch inheritance works | Implement scoped parent lookup and inheritance; explicit different session: accept, new conversation, cross-session reply link; reject inaccessible/unresolved parents, cross-namespace parents and an explicit conversation that conflicts with the inherited one; preserve generate-new for unrelated roots; timeline/graph tests for cross-session links | Prereq: none; Feeds: BI-02, BI-04, DS-07 |
| RM-37 | OPEN | P3 | Research / retrieval maintainer | Run the offline BM25S evaluation against Tantivy selected by LX1-A | LX1-A accepted 2026-10-18 (LEXICAL_BACKEND_OPTIONS.md): keep Tantivy and OpenSearch, evaluate BM25S offline first, defer BM25x; no evaluation run yet | Bounded offline parity and latency comparison on a fixed corpus; no runtime provider added | Prereq: none (LX1-A selection recorded) |
| RM-38 | OPEN | P2 | Model / dependency maintainer | Enforce offline artifact loading before the static embedding adapter is enabled in the selected offline profile | Rechecked source: models/encoders/embedding/static_local.py::_ensure_model calls StaticModel.from_pretrained without an offline guard; accepted no-automatic-download profile requires enforcement, not silent network fallback | Mandatory gate for enabling this adapter: local hit works; missing artifact fails visibly; blocked-network test proves no fetch. No model activation or download is selected | Prereq: PA-123; Feeds: BI-01, RS-09, BI-12; Gate: static adapter cannot be promoted into the offline profile until this passes |
| RM-39 | DONE | P3 | Test / provider maintainer | Verify LanceDB version-cleanup safety behind the recurring cleanup_older_than=0 warnings | 2026-10-22: e3554722 real defect: zero-age cleanup deleted files under concurrent in-process reads (87/1,592 reads failed in a stress test). Read/cleanup gate added; exact-message warning filter; zero-age kept (ADR-053/039). Stress failures 0, zero-age warnings 13 to 0 | Completed; cross-process readers of one file-backed table remain outside the contract; file-backed read throughput about 12% lower during purges | Prereq: none; Feeds: RM-15 |
| RM-40 | DONE | P3 | Provider maintainer | PostgreSQL connect_timeout=1s takes about 2.0 s (measured effective bound 2.5 s) | 2026-10-22: e3554722 root cause psycopg/libpq per-attempt minimum 2 s per address (localhost tries ::1 and 127.0.0.1, so 4 s). Start-up connect now has one overall deadline (1.0 s config fails in 1.15 s); per-attempt value via named constant; docs updated | Completed; pooled reconnects and the sync Alembic engine keep only the per-attempt bound (documented) | Prereq: PA-054; Feeds: PA-072 |
| RM-41 | IN_PROGRESS | P2 | Graph maintainer | Define the typed graph edge-qualifier contract (temporal, domain and other context) and qualifier-filtered traversal | Owner selected on 2026-10-22: typed edge qualifiers join BI-08 scope, not only temporal validity. GraphEdge can carry arbitrary metadata but GraphStore has no typed qualifier fields or filtered traversal | Within BI-08: typed edge and claim qualifiers with filtered traversal on SQLite adjacency plus provider contract fixtures. P1: valid time, known time, assertion status, lifecycle stage, claim state, provenance/origin, authority/trust, subject binding, scope/grant/erasure state, event role, vocabulary pack. P2: topical domain, sensitivity, context/location, applicability condition, extraction confidence/method, recurrence series, completeness. P3: independent-support count [survey](BI01_CONTRACT_REWRITE_PLAN.md#benchmark-informed-vocabulary-and-qualifier-survey--2026-10-22-rm-50-rm-41). Missing qualifiers stay unknown and never pass a filter | Prereq: PA-081; Feeds: BI-08, DS-10 |
| RM-42 | IN_PROGRESS | P1 | Architecture / ingestion owner | Define raw-first fallback for supported statements outside the semantic vocabulary | Owner selected A on 2026-10-22: raw plus exact-source unmapped-statement annotation, reasons and producer revision; no invented predicate or grounded fact | Define canonical annotation/event, evidence/retry lifecycle and zero-generative extraction gate; unsupported language may remain raw/mentions without guessed statement boundaries | Prereq: none; Feeds: BI-01, BI-05, BI-06; overlaps DS-03/04/19 |
| RM-43 | IN_PROGRESS | P1 | Policy / runtime owner | Freeze policy requeue eligibility and effective-revision activation triggers | Owner selected A on 2026-10-22: explicit effective-policy activation, including activated registry digest, requeues scoped unfinished/retry-eligible work; no completed historical backfill | Implement durable desired revision, cancellation/supersession and commit fence; exclude terminal/erased/excluded/revoked sources; unactivated edits do nothing | Prereq: none; Feeds: BI-01, BI-03, BI-07; historical backfill remains separate |
| RM-44 | IN_PROGRESS | P1 | Config / verification maintainer | Prove mandatory embedding/vector capability preserves core/simple and independently configured lexical search | Owner selected on 2026-10-22: core/simple mechanics tests use the real default embedder via an explicit documented download step and CI step; no hidden download [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Add the download command and CI cache step; prove core/simple behaviour and independent lexical configuration unchanged | Prereq: RM-35; Feeds: BI-01, BI-02, BI-12 |
| RM-45 | IN_PROGRESS | P1 | Semantic contract maintainer | Define scoped predicate/value cardinality independently of source-claim multiplicity | Owner selected on 2026-10-22: scoped one-value expectation plus conflict flag. A predicate may declare expected cardinality per subject and time/role/precision scope; a second supported value raises a visible conflict. All 53 packaged predicates still declare many | Add one/many/contextual/unknown with scope to PredicateSpec; review which of the 53 predicates are scoped one-value; conflicts retain both evidence sets and never auto-delete or supersede without a supported correction or transition | Prereq: none; Feeds: BI-01, BI-06, RM-23 |
| RM-46 | IN_PROGRESS | P1 | Semantic / graph maintainer | Define symmetry, inverse query views and shared event-family metadata | Owner selected on 2026-10-22: symmetric flag and inverse_view in PredicateSpec; stored once, reverse direction is a read view, never a second fact [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Add fields, validate pairs, graph read views and replay/erasure tests | Prereq: none; Feeds: BI-01, BI-06, BI-08, RM-23 |
| RM-47 | IN_PROGRESS | P1 | Event / role contract maintainer | Add general event-role slots with typed cardinality and support requirements | Owner selected on 2026-10-22: roles actor, location, time, instrument, beneficiary, topic, companion, origin, destination, theme/patient, cause, result, audience/participant, manner, amount [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Add RoleSpecs with value kinds, per-slot cardinality and evidence requirements; fixtures per role family | Prereq: none; Feeds: BI-01, BI-06, BI-08, RM-23 |
| RM-48 | IN_PROGRESS | P1 | Source / provenance maintainer | Define generic source-origin taxonomy and canonical tool/structured adapter provenance | Owner selected on 2026-10-22: origins add tool_output, structured_record, agent_observation, reflection, transcript, ocr, caption, external_catalogue, email, calendar_event, web_page (explicit fetch only), form_input, profile_import, code_repository, system_event [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Origin taxonomy and canonical adapters with provenance; origin, sender, assertion owner and trust stay distinct; derived text links to its asset | Prereq: none; Feeds: BI-01, BI-02, BI-05, BI-06; no alternate tool writer |
| RM-49 | IN_PROGRESS | P1 | Architecture / memory maintainers | Validate the canonical cutover across all nine memory write/read/lifecycle paths | Owner selected on 2026-10-22: cutover acceptance covers all nine memory types plus F1..F13 before BI-01 promotion [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Convert the nine-memory surface map into focused regressions including core/simple; preserve grants, resource chunks, plans and watches | Prereq: none; Feeds: BI-01, BI-12, RM-24; existing PA-075..083 gates do not prove new cutover |
| RM-50 | IN_PROGRESS | P2 | Domain vocabulary / memory owners | Review further agent-memory domain coverage beyond the selected 53 predicates | Owner selected on 2026-10-22: family 1 committed_to, due_by, requested_of (+priority); family 2 decided, works_on, blocked_by (+progress, outcome); family 3 cites, about, derived_from, revision_of (+locator, publisher, licence). Survey done [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Register the selected names with role/type/unit specs and fixtures; extraction stays gated by BI-06 | Prereq: RM-57; Feeds: BI-06, RS-08, RM-54; unsupported concepts follow RM-42 |
| RM-51 | IN_PROGRESS | P2 | Entity / semantic contract owner | Review concept/document/artifact/software/system/event taxonomy boundaries | Owner selected on 2026-10-22: add entity types concept/topic, document/artifact, software/system, event [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Extend EntityType; keep event entities distinct from event-log IDs and occurrences; open-world unresolved handling | Prereq: none; Feeds: BI-01, BI-06, BI-08; overlaps RM-34/DS-10 |
| RM-52 | IN_PROGRESS | P2 | Privacy / retention maintainer | Define optional semantic sensitivity and retention hints without weakening source policy | Owner selected on 2026-10-22: optional strengthening sensitivity classes health, financial, contact, identity, location [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Add optional labels; prove they only tighten firewall/retention | Prereq: none; Feeds: BI-01, BI-07, BI-10; overlaps DS-16 |
| RM-53 | IN_PROGRESS | P2 | Semantic / plugin maintainer | Define versioned domain-pack loading, namespace/conflict rules and definition withdrawal | Owner selected on 2026-10-22: built-in default vocabulary plus data-only plugin packs plus custom config-file vocabularies under one versioned, scoped, digest-pinned, conflict-checked contract [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Pack manifest and loader; activation is a policy revision (RM-43); withdrawal stops new jobs and keeps history | Prereq: none; Feeds: BI-01, RM-23, RS-08 |
| RM-54 | IN_PROGRESS | P1 | Verification / ingestion maintainer | Expand fixtures beyond personal-chat F1..F5 | Owner selected on 2026-10-22: F1..F13 become executable tests before BI-01 promotion [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Add positive/negative/unresolved cases and known-time/replay/erasure checks with exact support; before extraction/promotion of each domain | Prereq: none; Feeds: BI-06, BI-12, RM-22; no benchmark gold or generation |
| RM-55 | IN_PROGRESS | P1 | Shared-memory / identity owner | Define grant-authorised cross-namespace identity merges with evidence and revocation boundaries | Owner selected B on 2026-10-22: authorised read grants may create cross-namespace identity merges with cited identity evidence. Refined 2026-10-22: the merge and the grantee identity evidence are visible only in the grantee view; the grantor never sees them without a separate grant in the other direction | Define grant-scoped reversible merge projection held in the grantee view, evidence/known-time dependencies and revocation/erasure invalidation; F13 must assert no grantee evidence or merge is visible to the grantor; no destructive global ID rewrite or implied source-write authority | Prereq: none; Feeds: BI-01, BI-08, BI-12; overlaps RM-18/DS-11/PA-083; resolve before shared promotion |
| RM-56 | DONE | P2 | Codex / documentation maintainer | Reconcile attached generic-memory review with source and one tracker | Review qualified tool ingestion, same-batch versus stored-parent session handling, custom registry construction and indirect memory paths; stale RM-23 detail and selected-decision table corrected. [Review plan](BI01_CONTRACT_REWRITE_PLAN.md#generic-memory-review-and-bi-01-freeze-gates--2026-10-22) Focused gate: 23 passed, no failures/skips; identity/tool probes and task/link checks passed. [verification](provider_verification/bi01-generic-review-2026-10-22.json) | Review reconciliation complete only; generic contract/runtime work remains in RM-36/42..55 and BI packages | Prereq: none; Feeds: BI-01, RM-22, RM-23, RM-24 |
| RM-57 | IN_PROGRESS | P1 | Semantic contract maintainer | Define one shared lifecycle-stage list for commitments, work status and event lifecycle | Owner selected on 2026-10-22: two axes, status (proposed..superseded incl. closed) and outcome (done, missed, failed, declined; required when closed); rescheduling is a time change plus link [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Add typed status/outcome fields to occurrences and claims; explicit-only transitions with evidence; fixtures for each terminal path | Prereq: none; Feeds: RM-50, RM-60, RM-41, BI-06 |
| RM-58 | IN_PROGRESS | P1 | Domain vocabulary owner | Review family A: social relationships beyond kin and friend | Owner selected on 2026-10-22: core social set partner_of, relative_of, colleague_of, acquainted_with, mentor_of, caregiver_of, neighbour_of + kinship_type, relationship_type [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Register with RM-46 symmetry/inverse metadata; fixtures for mixed relationship objects | Prereq: RM-46; Feeds: BI-06, RM-23 |
| RM-59 | IN_PROGRESS | P1 | Domain vocabulary / procedural owner | Review families B and C: assistant-directed instructions, interaction preferences, constraints and rules | Owner selected on 2026-10-22: merge B and C: instructed with directive polarity must/must_not/permitted and applicability condition; restricted_by; keys tone, detail_level, rule_strength [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Register; procedural-memory fixture; instructions never become user facts | Prereq: RM-47; Feeds: BI-06, RM-54 |
| RM-60 | IN_PROGRESS | P1 | Domain vocabulary / prospective owner | Review family G: goals and wishes | Owner selected on 2026-10-22: has_goal, wishes + target_value, target_date; achievement = closed + done [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Register; plans never become completed events without evidence; connect to prospective watches | Prereq: RM-57; Feeds: BI-06, RM-54 |
| RM-61 | IN_PROGRESS | P1 | Domain vocabulary / identity owner | Review family K: identity and contact attributes | Owner selected on 2026-10-22: citizen_of, speaks, affiliated_with; keys occupation, pronouns, nationality, contact_channel, preferred_contact_time [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Register; contact keys inactive until RM-52 sensitivity labels exist; aliases stay in the identity system | Prereq: RM-52; Feeds: BI-06 |
| RM-62 | OPEN | P2 | Domain vocabulary owners | Review P2 families D, E, F, H, I, J, L (health, feelings/beliefs, learning, communication acts, transactions, possession lifecycle/opinions, routines) | Owner selected on 2026-10-22: schedule families D, E, F, H, I, J, L now [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Confirm names per family during registration review; health needs RM-52 labels; beliefs stay attributed | Prereq: RM-50; Feeds: BI-06 |
| RM-63 | OPEN | P3 | Domain vocabulary owners | Review P3 families M and N (travel roles, tool and agent experience) | Owner selected on 2026-10-22: schedule families M and N now [decisions](BI01_CONTRACT_REWRITE_PLAN.md#owner-review-decisions--2026-10-22-vocabulary-contracts-and-maintenance) | Confirm names during registration review; tool experience aligns with RM-48 tool_output origin | Prereq: RM-50; Feeds: RM-48 |
| RM-64 | OPEN | P2 | Docs / config maintainer | Add USAGE.md rows for models.profiles, models.bindings, models.escalation, workers.temporal and workers.resonate | 2026-10-22: tests/unit/test_docs_config_surface.py fails because USAGE.md lacks these keys; pre-existing, found during RM-40 | Document the keys; the docs-config surface test passes | Prereq: none; Feeds: RM-15 |
| RM-65 | OPEN | P2 | Verification lead | Re-review skip-audit drift: 27 missing and 8 stale reviews in files RM-17 did not touch | 2026-10-22: scripts/audit_test_skips.py exits 1 after RM-17; 19 hash-only changes, 8 line moves (test_sqlite_encryption, test_vector_spaces, test_graph_semantic_entities, test_fu_sleep, test_md_02_embedding_profile) | Re-review each flagged site; audit exits 0 | Prereq: none; Feeds: PA-039 |
| RM-66 | REVIEW | P2 | Data / privacy owner | Review learned.alias events written before RM-18 in multi-namespace deployments | 2026-10-22: pre-fix flush could write another namespace aliases into an event labelled with the flushing namespace; reload now trusts the event namespace | Owner decides whether existing logs need review or erase_namespace; no migration shim | Prereq: RM-18; Feeds: none |
| RM-67 | OPEN | P3 | Docs / config maintainer | Remove the dead workers.dbos_system_database_url key from docs/v2/data/stage_params_manual.json | 2026-10-22: key matches nothing after ADR-071; replacing with workers.temporal.* changes stage ownership | Owner-approved mapping or removal; regenerate STAGE_PARAMS | Prereq: none |
| RM-68 | OPEN | P3 | Observability maintainer | Fix test_t5_06_observability opentelemetry _ExtendedAttributes import error | 2026-10-22: fails in the current .venv (installed opentelemetry version mismatch); seen during RM-18 regression | Pin or adapt to the locked opentelemetry version; test passes after RM-02 sync | Prereq: RM-02 |
| RM-69 | OPEN | P3 | Docs maintainer | Add the missing decision-register row for ADR-067 and update engine-wide alias notes in older docs | 2026-10-22: ADR-066 got D-101 in RM-10; ADR-067 has no row. PROVIDER_NEXT_MILESTONES.md, PERFORMANCE_IMPLEMENTATION_RESULTS.md and verification notes still call aliases engine-wide | Register row added; alias notes say namespace-scoped since RM-18 | Prereq: RM-18 |
| RM-70 | IN_PROGRESS | P1 | Provider / runtime maintainer | Warm up once and share every client across all internal consumers (keyed, refcounted resource registry) | Owner selected 2026-10-22: share within and across Engines (new ADR), background warm-up after start, per-process warm pools for separate workers, keyed process globals. Recorded as D-102 / [ADR-079](adr/ADR-079-keyed-shared-warm-resources.md). Audit findings: up to 4 Postgres pools per database, 2 Weaviate clients, per-runner brokers, per-role llama.cpp loads, duplicate static embedder, GLiNER thrash, Resonate per activity, lazy first-request model loads, unkeyed globals | Umbrella: complete when RM-71..RM-77 are DONE | Prereq: none; Feeds: RM-71, RM-72, RM-73, RM-74, RM-75, RM-76, RM-77 |
| RM-71 | DONE | P1 | Provider / runtime maintainer | ResourceRegistry core: keyed (target identity, settings digest, loop, pid), refcounted leases, coalesced first build, warm hook, fork reset | 2026-10-22: src/memspine/providers/shared.py: per-process ResourceRegistry; keys hash target identity and settings (no raw secrets), loop scope uses a never-reused per-loop token, pid checked; coalesced first build, failed builds not cached, refcounted last-release close, cancelled waiters never cancel the shared build or leak it, warm() once with retry after failure, closed-loop entries dropped, fork reset. tests/unit/providers/test_shared_registry.py 12 passed (3 runs) | Completed core; Engines adopt it per resource in RM-72/RM-73/RM-75 by owning lease.aclose in their lifecycle manager | Prereq: RM-70 |
| RM-72 | DONE | P1 | Provider maintainer | Remove in-engine duplicates: one sync SQL engine per database (purposes become leases), one Weaviate client per target, one broker per Engine, Resonate per runner, static prefilter reuses the configured static embedder | 2026-10-22 823a2738: one sync SQL engine per database (purposes are labelled leases; Postgres 4 pools to 2), file SQLite/Postgres clients shared via the registry, one Weaviate client per target, one SqlJobStore and broker per Engine, Resonate once per runner start (per-attempt promise ids), Tantivy/Ladybug shared per path with a thread lock (safe across loops). :memory: unchanged. New own_shared/path_identity helpers. Focused 10 passed; affected suites 658+1138+192 passed in worktree; 372 passed after merge | Completed; not converted by design: LanceDB (no socket), Neo4j (driver tied to scoped executor), cashews (wraps cache contents), taskiq redis across Engines. Open checks: Resonate change unrun (SDK not installed), shared Tantivy defer_commits batching | Prereq: RM-71 |
| RM-73 | DONE | P1 | Model maintainer | Share model weights through the registry: embedders, GLiNER (tasks and concurrency become per-lease limits, lease held per consumer), llama.cpp keyed by path and native options, rerankers, OpenDecider | 2026-10-22 70203558: SharedWeights leases (thread scope, weight-only keys) for fastembed/ST/static embedders, static prefilter (reuses the configured entry), GLiNER (weights keyed by model/revision/device/offline; tasks and concurrency are per-lease limits; lifetime binding per consumer), llama.cpp (absolute GGUF path plus n_ctx and native options), fastembed/flashrank/qwen3/jina rerankers, OpenDecider (globals removed, failures not cached). Warm hooks ready for RM-74. Result caches stay per Engine. After merge: 798 passed (models/providers/services/semantic/derive/clients/workers), 1290 passed (wiring + engine tests) | Completed; limits: an adapter object must not be used from two event loops (per-holder asyncio.Lock; Engines never do this); first holder's warm/close hooks are kept until the entry frees; end-to-end multi-engine proof is RM-77 | Prereq: RM-71 |
| RM-74 | OPEN | P1 | Runtime / API maintainer | Background warm-up after start with per-resource readiness; requests wait only for resources not yet warm | Audit: embedder, Tantivy, rerankers, GLiNER and decider load inside the first request | start() returns after connect; warm state in readiness; first request after warm-up pays no load; failures visible | Prereq: RM-71, RM-73 |
| RM-75 | DONE | P2 | Runtime maintainer | Key the process globals in the registry: model scheduler, economy and ops ledgers, tap sink, concurrency gate | 2026-10-22 85223a2b: per-Engine EngineBinding (observability/bindings.py) carried by a context variable around public Engine methods; scheduler keyed by its 13 settings plus loop via the shared registry; gate, SLO, cost and ops ledgers and tap keyed by config/store; single-Engine fallback unchanged. Combined tree after RM-72/73/75: 512+1104+1062+303 passed (2 order-dependent RM-72 test failures fixed by a per-test registry fixture) | Completed; follow-ups: code outside an Engine call (executor threads, direct pipeline calls) with 2+ started Engines gets no seams; private Engine methods called from REST/MCP run unbound; leases released in Engine teardown rather than the lifecycle manager; inspect flags still process-global | Prereq: RM-71 |
| RM-76 | OPEN | P2 | Worker / deployment maintainer | Per-process warm pools for separate workers: taskiq worker, uvicorn workers, MCP subprocess | Audit: each separate process builds a full Engine and repeats warm-up | Registry built and warmed at worker start after fork/spawn; documented process model | Prereq: RM-71, RM-74 |
| RM-77 | OPEN | P1 | Verification lead | Multi-engine acceptance for shared resources (extends tracker T13) | Required by ADR-079 consequences | Stop-one-while-another-active, different-settings isolation, single-writer path sharing, loop affinity, fork reset, :memory: uniqueness, no result sharing | Prereq: RM-72, RM-73, RM-75 |
| RM-78 | OPEN | P1 | Config / runtime maintainer | Add the runtime.shared_resources settings block (scope, warmup and limits, keep_warm, idle TTL, memory budget, per-category caps, SQL and HTTP pools) | Owner adopted all proposed settings 2026-10-22; default keep warm with no TTL [ADR-079 addendum](adr/ADR-079-keyed-shared-warm-resources.md) | Strict schema, constants, USAGE.md rows, wiring into the registry, warm-up and pools; tests per setting; implement after RM-72/73/75 merge to avoid schema conflicts | Prereq: RM-72, RM-73, RM-75; Feeds: RM-74, RM-76 |
| RM-79 | DEFERRED | P3 | Model / deployment maintainer | Opt-in shared local model server for cross-process RAM sharing (embedder, GLiNER, reranker) | Owner 2026-10-22: reserve the model_server setting, build later [ADR-079 addendum](adr/ADR-079-keyed-shared-warm-resources.md) | Select when multi-process deployments need shared model RAM | Prereq: RM-76 |
| RM-80 | IN_PROGRESS | P1 | Performance / runtime maintainer | Decide client and shared_resources defaults from measurements (scope, warm-up mode and concurrency, SQL and HTTP pools) | Owner 2026-10-22: decisions based on tests. Harness scripts/benchmark_shared_resources.py; rules in the ADR-079 RM-80 section. Baseline before sharing (3 trials, cached bge-small): each extra identical Engine adds about 124 MB RSS, 23 threads and 32 OS handles; first request pays 280-700 ms lazy model load. [baseline](provider_verification/rm80-shared-resources-baseline-2026-10-22.json) | Run each variant after RM-72/73/75/78 merge (latency on an idle host); record evidence JSON; set defaults per rule; regression test guards the chosen defaults | Prereq: RM-72, RM-73, RM-75, RM-78; Feeds: RM-74, RM-76 |
<!-- TASK_REGISTER_END -->

## Imported roadmap detail (no independent statuses)

These notes retain the scope of the earlier review. Each item maps to the one current row above; observations/claims in the old note may need validation. No tool upgrade, full suite, deployment or commit was selected by importing them.

| Current task ID | Earlier phase | Preserved recommendation |
|---|---|---|
| RM-01 | ### Phase A: secure the work (do first) | **[owner]** Authorize committing the working tree to `restructure` (no push). The plan docs say no commit is implied by approval of a document. |
| RM-02 | ### Phase A: secure the work (do first) | **[local]** `uv self update` (>= 0.13.1), then `uv sync --locked --extra dev`. |
| RM-03 | ### Phase A: secure the work (do first) | **[local]** Format-only commit first (`ruff format`), so later diffs are readable. |
| RM-01 | ### Phase A: secure the work (do first) | **[local]** Commit in themed chunks, each with its focused tests rerun: - C1 Python 3.13 dependency refresh (pyproject, uv.lock, ci.yml, audit script, docs) - C2 DBOS → Temporal + Resonate (runner, workflow, step journal, schema, templates, tests, docs) - C3 Performance kernels (dedup.py, rrf_fuse, legs.py, constants, tests, benchmark scripts) - C4 GLiNER pool, model profiles, escalation (models/runtime, profiles.py, schema, container) - C5 GLiNER structured candidates and source-bound mentions (derive/gliner_*, entity_mentions, assertion/records/compile changes, `workers/windows.py` offset fix) - C6 Small fixes (community summary event time PA-114, intake_store loop variable) - C7 Verification tooling and evidence - C8 Design docs, ADR-077/078, schema review, research benchmarks - C9 Doc sync (last; see Phase B) |
| RM-04 | ### Phase A: secure the work (do first) | **[owner]** Decide the fate of ~190 `docs/provider_verification/*.log/*.json` files: commit, prune, or `.gitignore`. |
| RM-05 | ### Phase A: secure the work (do first) | **[local]** Fix the 166 ruff errors in `evals/bench/` and `docs/schema_review/` before C8, or exclude those research paths in ruff config. |
| RM-06 | ### Phase B: make the docs true | **[local]** CLAUDE.md: test count, ADR count/range (ADR-001..078), register range (D-100), `workers/pipelines.py` path (now `derive/pipeline/stages/table.py`), the `-p no:deepeval` rule (DeepEval removed), Cache/KV "LMDB" (default is `memory`), entities/gliner2 row (now `models.profiles`/`bindings`, gliner2.5), `uv sync --all-extras` vs locked baseline, repo layout (ADR-067 tree). |
| RM-07 | ### Phase B: make the docs true | **[local]** Make STATUS.md point to the canonical tracker, or generate a read-only view from it. |
| RM-08 | ### Phase B: make the docs true | **[local]** README: ADR count; OpenSearch/Weaviate are shipped extras, not "planned". |
| RM-09 | ### Phase B: make the docs true | **[local]** CHANGELOG entries for ADR-068/069/070/072-076. |
| RM-10 | ### Phase B: make the docs true | **[local]** ADR status lines: ADR-068 and ADR-071 ("in progress" → implemented, locally verified); ADR-070 superseded-by-ADR-071 note on DBOS; ADR-074 superseded-by-ADR-075 note on `models.gliner`; ADR-066 needs its register row. |
| RM-32 | ### Phase B: make the docs true | **[local]** PROVIDER_TODO.md: one ID count (136) everywhere; duplicate "## H." heading; stale "Recommended next sequence"; DONE rows that depend on unfinished rows (PA-042/093/094 on PA-039, PA-052 on PA-051, PA-055 on PA-053); PA-048/049/055 marked DONE but "Partial" in PROVIDER_PERFORMANCE_FOLLOWUP.md. |
| RM-11 | ### Phase B: make the docs true | **[local]** PROVIDER_ARCHITECTURE_PLAN.md stale sections: §2 current-code facts, §11 pre-implementation boxes, "no commit performed" lines. |
| RM-12 | ### Phase B: make the docs true | **[local]** Stale-in-place sections in performance and enrichment docs (section 6). |
| RM-13 | ### Phase B: make the docs true | **[local]** DBOS references in active `docs/v2` briefs/areas/plan.json/tracker.html. |
| RM-14 | ### Phase B: make the docs true | **[local]** Fix UTF-8 mojibake in CPU_NON_LLM_BENCHMARK_RESULTS.md. |
| PA-039 | ### Phase C: re-establish a verified baseline | **[owner]** Authorize the broader sweep (PA-039). |
| RM-15 | ### Phase C: re-establish a verified baseline | **[local]** Chunked full unit run with `-p no:deepeval`, on the committed revision; record with `scripts/record_verification_run.py`. |
| RM-16 | ### Phase C: re-establish a verified baseline | **[local]** Rerun live Temporal gate (`MEMSPINE_TEST_TEMPORAL_ADDRESS`) and the strict native Docker gate on the same revision. |
| RM-17 | ### Phase C: re-establish a verified baseline | **[local]** Close skip-review P2 milestones M-MODEL, M-BRANCH, M-LIVE, M-PLAN (review date 2026-10-25). |
| RM-18 | ### Phase C: re-establish a verified baseline | **[local]** Scoped review of learned-alias isolation (engine-wide, not namespace-scoped). |
| RM-19 | ### Phase D: unblock evals (small, high value) | **[local]** `evals/run_v2_batch.sh`: drop `--extra dbos`. |
| RM-20 | ### Phase D: unblock evals (small, high value) | **[local]** `evals/memspine_evals/multiagent/constructions.py:47`: nested storage settings. |
| PA-107 | ### Phase D: unblock evals (small, high value) | **[owner]** Resume PA-107 early, at least for `v2-PICK3-j.json`. |
| RM-21 | ### Phase D: unblock evals (small, high value) | **[local]** Mechanical arm migrator: flat → `provider`/`settings`, `rerank_model` → rerank settings, add `provider` to LLM roles. Historical run records stay untouched. |
| BI-01 | ### Phase E: background ingestion BI-01..12 (main feature work) | **BI-01** contracts + config: runtime types for Q-S1..5 and operating defaults; annotation event vocabulary; receipt/status API (`ingestion_status`, `wait_for_derivation`); policy revision boundary. Rename the planned `SourceSnapshot` (collides with `providers/switching.SourceSnapshot`). |
| RM-22 | ### Phase E: background ingestion BI-01..12 (main feature work) | Fixtures F1..F5 as executable schema + behaviour tests with negative/unresolved variants. |
| RM-23 | ### Phase E: background ingestion BI-01..12 (main feature work) | Pure semantic registry `semantics/` with the selected 53 predicates / 44 property keys and shared typed roles (no model or DB deps); foundation is not extraction activation. |
| BI-02 | ### Phase E: background ingestion BI-01..12 (main feature work) | **BI-02/03** required-derivation manifest at acceptance; durable raw → job discovery on the existing outbox; crash/reopen tests. Must land before any background default is enabled. |
| BI-04 | ### Phase E: background ingestion BI-01..12 (main feature work) | **BI-04** isolated windows. **BI-05** persist mentions for every source regardless of fact yield. **BI-06** English rule packs, zero generative calls, unresolved outcomes. (BI-05 and BI-06 can run in parallel after BI-01.) |
| BI-07 | ### Phase E: background ingestion BI-01..12 (main feature work) | **BI-07** fenced effect commit with erasure-generation checks, then **BI-08** graph occurrence projection merging rule `turn_mentions` and GLiNER mentions into one contract. |
| BI-09 | ### Phase E: background ingestion BI-01..12 (main feature work) | **BI-09** readiness API, **BI-10** lossless compression verification, **BI-12** one-namespace smoke. BI-11 summaries stay separately selected. |
| RM-24 | ### Phase E: background ingestion BI-01..12 (main feature work) | Q-S1-B is a coordinated breaking rewrite (compiler, gateway, storage, projectors, graph, readers, fixtures). Plan it as one cut-over. |
| DS-17 | ### Phase E: background ingestion BI-01..12 (main feature work) | **ACD-04/07** outbox → Temporal ingestion: design together with BI-02/03 so there is one durable route; add the missing directive §8 tests (crash after business write, interrupt during LLM call, old-history versioning). |
| PA-046 | ### Phase F: performance follow-ups (need owner budgets) | **[owner]** PA-046: set a cold-start budget. **[local]** Re-measure on the committed revision (the +12.1% predates the kernels and later waves). Levers: ~180 ms SQLite/migration import on the startup path; LanceDB import ~858 ms. |
| PA-051 | ### Phase F: performance follow-ups (need owner budgets) | **[owner]** PA-051 controlled host; PA-053 foreground p95 and derivation-lag budgets. |
| RM-29 | ### Phase F: performance follow-ups (need owner budgets) | **[local]** Extend the PA-055 soak beyond 30 cycles. |
| RM-25 | ### Phase F: performance follow-ups (need owner budgets) | **[local]** Repair the HF cache snapshot, rerun embedding batching from a clean cache. |
| RM-26 | ### Phase F: performance follow-ups (need owner budgets) | **[local]** CPU model envelope: thread sweep, batch variants, ONNX/int8 parity. |
| PA-095 | ### Phase G: release and deployment | **[owner]** PA-095/096: PR destination and release workflow. Optionally trigger `provider-native.yml` remotely (never run remotely yet). |
| PA-113 | ### Phase G: release and deployment | **[local]** PA-113 dependency audit at release preparation (needs network). |
| PA-056 | ### Phase G: release and deployment | **[deploy]** PA-056 first (deployment, owner, runbook); unlocks PA-057..073 and PA-082. |
| RM-27 | ### Phase G: release and deployment | **[deploy]** ACD-09 (check for old DBOS jobs), ACD-11 (volume failover drill), ACD-12 (canary rollout). |

## Recorded evidence and history (not a second current register)

Earlier counts/status wording below describe their run dates. Use the register above for current disposition.

# Provider architecture — implementation and follow-up tracker

Implementation/status evidence baseline: 2026-10-18. Documentation handoff reconciled: 2026-10-20. Implementation baseline: `e6596f44` on `restructure`.
This tracker covers the provider program and proposed integration with the nine
memory types. Dependency follow-ups PA-101–115 extend the original baseline. It is not a fresh
audit of every engine subsystem. Open/proposed
rows are work packages or validation gaps, **not automatically proven bugs**.
PA-107 records reproduced existing evaluation failures; PA-114 records an observed
community timestamp defect and its targeted fix.
`DONE` means the stated local gate passed; it does not imply production approval.


## Current Claude handoff - 2026-10-20

[Claude project context](CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md) describes decisions, completed research and examples. Its duplicated status appendix/JSON were removed; this file alone owns the current task register for both Claude and Codex.

Documentation-only reconciliation: the background plan no longer requires generative fact extraction in the selected target profile; the memory-type matrix reflects recorded PA-075..081/083 local completions and PA-082 deployment checks. No runtime tests were rerun, no provider status advanced, and no BI implementation marked done. Counts remain 103 DONE /5 OPEN plus separate disposition categories. Next buildable package: BI-01, then durable BI-02/03.


## Deep working-tree review - 2026-10-21

[Current architecture and code review](DEEP_SCAN_REVIEW_2026-10-21.md) maps actual ingestion, background derivation, providers/models, graph, retrieval/packing, erasure/compression and nine memory types. It records 21 DS findings separately from the 136 provider IDs: 2 isolated method failures, 1 unsafe rule-candidate finding, 2 reproduced contract gaps, 13 code-confirmed target gaps and 3 evidence/review limits. [Structured findings](schema_review/deep_scan_findings_2026-10-21.json) and [probe evidence](schema_review/deep_scan_probes_2026-10-21.json) distinguish these categories.

Immediate correctness follow-ups: DS-01 restores staged jobs after unexpected enqueue failure; DS-02 clarifies/enforces direct readiness deadlines; DS-21 requires attribution/modality guards for rule candidates. Other gates cover non-generative window wiring, independent annotation persistence, raw-to-job/open-window recovery, source-time/identity, full dependency bundles and copied-data erasure. These are OPEN_REVIEW findings, not implementation completed in this pass.

AST inventory covers 615 source modules /152,039 lines /7,874 symbols with zero syntax errors; it is not a semantic audit of every line or a full runtime test. Selected critical code paths were traced and five isolated probe areas exercised without Engine/DB/model startup. Existing 103 DONE /5 OPEN provider counts remain historical local-gate statuses; affected guarantees require the new regressions before promotion. All BI-01..12 remain PLANNED, PA-107 deferred.

## Status summary

| Status | Count | Meaning |
|---|---:|---|
| DONE | 103 | Implemented or evaluated against the stated local gate, with recorded evidence |
| OPEN | 5 | Remaining local/release provider follow-ups; BI packages and deployment dispositions are tracked separately |
| DEPLOYMENT_CHECK | 19 | Target-deployment acceptance checks; separate from local development completion |
| NEEDS_INPUT | 3 | Representative host/budget, quality-evaluation budget or release destination |
| NEEDS_TOOLCHAIN | 1 | Optional Windows native build toolchain verification |
| PROPOSED | 0 | Suggested enhancement not yet selected; previous 31 items now have an explicit disposition |
| DEFERRED | 1 | Owner explicitly postponed this workstream |
| EXCLUDED | 4 | Owner excluded it or it would contradict agreed semantics |


**Priority:** P1 = correctness, operational acceptance or near-term review;
P2 = robustness, coverage, performance or release improvement; P3 = optional
future capability. Priority is a recommendation, not an authorization to deploy.
Owners and completed evidence for selected tasks are recorded below; remaining
items have no promised delivery date.

## Recommended next sequence (historical, 2026-10-18)

> Superseded: current order is the register above (Prereq/Feeds) and the BI ordering. Kept as evidence only.

New orchestration direction: see the [Temporal + Resonate change directive](TEMPORAL_RESONATE_CHANGE_DIRECTIVE.md)
for ACD-01–12. Its maintenance-runtime replacement is implemented and locally verified;
Historical DBOS rows retain prior evidence only. The maintenance runtime, extra,
config and templates now use Temporal + embedded Resonate; DBOS is removed. The
owner-selected SQLite step journal and true live Temporal retry/reopen gates pass.
Production volume failover and Temporal integration of ingestion outbox work remain
separate uncompleted gates. Unrelated evaluation migration remains deferred.

1. PA-038/090/091 complete: see TEST_SKIP_REVIEW.md and PROVIDER_NEXT_MILESTONES.md. The six selected M-GUARDS repairs subsequently passed; optional/deferred coverage remains separately gated.
2. PA-036/037/038: preserve reproducible verification evidence and explain skips.
3. PA-046/047/051: decide startup budgets, profile the final code and measure fairly.
4. PA-056/057/059/064: identify deployment, controller and real recovery guard.
5. PA-075–081/083 and PA-118–123 have recorded local gates; PA-082 is a deployment check. New enrichment integration remains BI-01..12, not an unfinished PA-118 implementation.
6. PA-039/093/094: run final release checks after selected changes, then review.

Database copying and re-embedding remain excluded. Do not silently enable optional
memory features, native hybrid search or paid evaluation while doing tracker work.

## Updating an item

Keep IDs stable. Move selected work `OPEN/PROPOSED → IN_PROGRESS → VERIFY → DONE`;
use `BLOCKED` only with a concrete dependency and next action.
Use `DEFERRED` for work postponed by the owner; retain its findings. Keep `NEEDS_ENV`
until the actual prerequisite is supplied. Add new rows with the next unused ID;
never renumber or delete history. Recompute summary counts after status edits.

For each started item append a progress entry below with owner, date, source
revision, exact action/test command, result/artifact and next action. Mark DONE
only when its completion criterion is met. Evidence links on open rows identify
starting points; they are not proof that the pending gate passed. A later change
may require revalidation of a previously DONE item.

## Source of truth and evidence

- [Current operations](PROVIDER_OPERATIONS.md), [accepted plan](PROVIDER_ARCHITECTURE_PLAN.md).
- [Verification report](provider_verification/README.md): broad sweep 7,434 passed,
  61 skipped and three metadata failures fixed by targeted retests; no claim of a
  single completely green full rerun. Final affected suites: 352 passed.
- Native contracts: eight combined cases plus one separate pgvector case passed.
- Final performance: cold start after import +12.1%; combined import/startup −2.8%;
  warm p95 −5.8%; search p95 −3.0%. Shared-host samples do not establish production SLOs.
- [Deployment runbook](PROVIDER_DEPLOYMENT_RUNBOOK.md): real production target,
  service-change controller and quiescence guard remain application prerequisites.

## A. Completed implementation baseline


## B. Verification and engineering follow-up


## C. Performance and capacity


## D. Deployment integration and operational acceptance


## E. Proposed memory-type and end-to-end coverage expansion


## F. Review, documentation and release


## G. Explicitly excluded from current delivery


## H. Python 3.13 dependency refresh (2026-10-17)


## I. GLiNER2.5 ingestion adoption
## K. Measured execution alternatives (owner-selected 2026-10-18)


## J. Multi-model profiles and explicit reasoning levels (selected 2026-10-18)

These seven work packages are recorded before implementation. Different roles may
select different models; only identical effective resources share ownership.


## Progress log

2026-10-22 — Codex: owner answered generic freeze questions RM-36 A, RM-42 A, RM-43 A and RM-55 B. Recorded authorised parent inheritance, unmapped evidence annotations, explicit scoped unfinished-work requeue and grant-authorised identity merges with evidence/revocation obligations. Design selections are not implemented runtime behaviour.

2026-10-22 — Codex: reconciled attached generic-memory review; corrected selected-choice table and stale RM-23 crosswalk; clarified RM-36 as open. Recorded RM-42..56 in this register only, including required freeze gates, broader fixtures and qualified source/runtime findings. No runtime or vocabulary activation changes. Four owner questions presented for session inference, unmapped fallback, requeue eligibility and shared identity; elapsed time is not approval.

2026-10-22 — Codex: implemented the pure semantic-registry foundation for selected 53/44 scope and shared RoleSpec definitions; 23 focused tests, Ruff and mypy pass. No fact writer, alternative ingestion path, model download, full-suite run, data conversion or provider activation. BI-01/RM-22/23/24 remain in progress for the coordinated cutover.

2026-10-21 — Codex: owner answered BI-Q1 A, BI-Q2 B, BI-Q3 refined A, BI-Q4 B and BI-Q5 B. Expanded scope to 53/44, shared typed roles, cancel/requeue on effective policy changes, mandatory embedding/vector services; SQL-only decision closed under RM-35. No unselected recommended alternative was substituted.

2026-10-21 — Codex: corrected RM-07/33/38/39/41 as selected by the owner. Started BI-01 with RM-22/23/24 in contract-preparation scope only; BI-Q1..Q5 await owner answers before schema/runtime implementation. Current statuses live only in the register above. Focused contract/tracker checks recorded in provider_verification/bi01-preparation-2026-10-21.json.

| Date | Item(s) | Owner | State/change | Evidence / next action |
|---|---|---|---|---|
| 2026-10-17 | PA-001–035 | Implementation team | Completed local baseline recorded | Commit `e6596f44`; detailed local limits above |
| 2026-10-17 | PA-036–100 | Unassigned | Historical tracker creation; later delivery entries supersede these initial states | Select PA-090, then assign work packages |
| 2026-10-17 | PA-081–089 | Retrieval acceptance agent | PA-085/086 DONE: acceptance separates relevance from correctness and records expected multilingual/literal behavior. PA-081/082/083 have selected local behavioral coverage but remain partial against the broader criteria; PA-087 is protocol/review only; PA-088/089 have no model/native ranking run. | [Acceptance test](../tests/integration/test_provider_graph_scope_acceptance.py): 4 passed, Ladybug included; [acceptance contract](PROVIDER_RETRIEVAL_ACCEPTANCE.md); [JSON evidence](provider_verification/graph-scope-acceptance.json); [log](provider_verification/graph-scope-acceptance.log). No model-quality budget; no worker handover/external exactly-once, graph-edge temporal filtering, cross-namespace graph access, shared-search result-cache path or native-hybrid ranking claim. |
| 2026-10-21 | RM-32 (touches PA-042/048/049/051–055/093–095/104, BI-01..12, DS-01..21, RS-01..09, RM-01..27; adds RM-29..41) | Claude | Tracker consistency pass: status/column legend; Prereq/Feeds labels; BI ordering from BACKGROUND_INGESTION_PLAN.md L124-128, partly inferred from handoff section 8 (marked in-row); independent review applied (12 findings); five DONE rows had mislabelled prerequisites (evidence re-read, scope narrowed in-row, no status changed); RM-01..21 revalidated against the current tree; RM-01/02/03 raised to P1; lost follow-ups and untracked decisions added as RM-29..41; four imported-detail mappings corrected | scripts/refresh_tracker.py --check passes (219 tasks); no DONE row has an unfinished Prereq. Next: owner decisions RM-01, RM-04, RM-33..36 |
| 2026-10-22 | RM-01, RM-04, RM-33, RM-36, RM-41, RM-45, RM-50, RM-55, BI-08 | Claude | Owner decisions recorded: RM-01 themed local commits (no push) authorized; RM-04 commit all evidence; RM-33 keep Base; RM-36 explicit different session starts a new conversation with a cross-session reply link; RM-41 typed edge qualifiers (temporal, domain and other context) folded into BI-08; RM-45 scoped one-value plus conflict flag; RM-50 commitments, decisions, documents first plus benchmark survey; RM-55 merges visible in grantee view only | Next: RM-02, RM-03, then C1..C9 commits; vocabulary survey result into RM-50 |

## Item update template

```text
Item: PA-___
Date:
Owner:
Status:
Source revision:
Action / exact verification command:
Result and durable evidence link:
Remaining dependency / next action:
```

## Earlier delegated delivery — 2026-10-17 (before runtime replacement)

| Item | Owner | Result | Remaining boundary |
|---|---|---|---|
| PA-110 | Provider contracts agent | 8 native contracts + 1 pgvector contract passed with refreshed lock | No production cutover/performance claim |
| PA-117 | GLiNER agent | 3 loaders use AutoExtractor; generated references updated; 69 passed, 1 skipped | No actual weight loading/inference; PA-118–123 remain |
| PA-036/037 | Verification agent + root | Hashed portable ledger and repository logs; 17 ledger tests passed | Single writer; manually supplied results; historical temp logs not all recovered |
| PA-074 | Memory mapping agent + root | All nine memory types mapped to source/provider seams | PA-075–083 behavioral combinations remain proposed |
| PA-084 | Root | Partial: synthetic F1.8 7/7, sleep 30/30, read 33/33 | Separate fixtures, not one persisted full-pipeline trace |
| ACD-01 / ACD-08 | Owner decision / journal agent + root | Option B selected; SQLite result journal + SDK example; crash/reopen and fencing tests | Main DBOS replacement, real Temporal dispatch and volume failover remain pending |

See the [verification index](provider_verification/README.md) and
[orchestration directive](TEMPORAL_RESONATE_CHANGE_DIRECTIVE.md). No retired
compatibility maps or automatic database-copy/re-embedding work was introduced.

## Durable runner replacement — 2026-10-17

| Directive item | Status | Evidence / boundary |
|---|---|---|
| ACD-02 settings and dependencies | DONE | Typed Temporal/Resonate configuration; `[temporal]` lock; DBOS removed |
| ACD-03 maintenance invocation contract | DONE | IDs and immutable config/source fingerprints; no live context serialization |
| ACD-05/06 maintenance Workflow + Activity | DONE | Real Temporal dispatch to local Resonate; default sandbox |
| ACD-08 local persistence | DONE locally | Canonical StepJournal; real failed-Activity retry and Worker reopen tests |
| ACD-10 DBOS code/config removal | DONE | No active source or lock dependency; old input rejected; no aliases |
| ACD-04/07 ingestion outbox integration | PENDING | Existing durable ingestion outbox remains its own inline/taskiq transport |
| ACD-09 operational drain | NEEDS_ENV | Stop/resolve any previously deployed DBOS jobs before rollout; no history conversion |
| ACD-11 hard container failover | NEEDS_ENV | Local process-exit journal tests and live graceful Worker reopen are not volume failover |
| ACD-12 production rollout | NEEDS_ENV | External cluster, auth, persistent volume placement and live workload acceptance |

The original 123 PA items above keep their stable IDs; the ACD migration checklist is a
separate scope. See [operations](TEMPORAL_RESONATE_OPERATIONS.md) and
[verification](provider_verification/README.md).

## Delegated verification completion — 2026-10-17

| Items | Owner | Completed evidence | Remaining boundary |
|---|---|---|---|
| PA-040/041 | Root | Existing CI reviewed; opt-in digest-pinned disposable service workflow; strict local run **8 passed, 0 skipped**; only owned containers/volumes removed | Manual GitHub workflow is configured, not remotely executed; no production acceptance |
| PA-042/043/044 | Wheel agent + root | Fresh installed wheel Engine lifecycle; three independent remote extras install/import/pip checks; explicit platform evidence matrix | Windows CPython 3.13.5 observed; other platforms and real remote service behavior are separate evidence |
| PA-045 | Wheel agent + root | Real installed plugin wheel; version/hash pins, contract rejection, strict settings, Engine write/search and one owned close | One embedding plugin kind; no semantic-quality or remote-model claim |
| PA-084 | Lifecycle agent + root | One persisted message through ingest, L1 derivation, offline stages and hybrid retrieval; source IDs/sender/time/lineage checked | Deterministic model stub; selected providers and memory types only |
| PA-092 | Root | Current-schema upgrade guide; four YAML examples validate against configuration and provider settings | Services were not started by example validation |
| PA-112 | Asyncio agent + root | Current loop-factory hook; Windows/PG selection preserved; collection-order import issue fixed | Real native PG run passed; no other-platform runtime claim |
| PA-038 | Asyncio agent + root | **Partial:** 98 static skip callsites, zero parse errors; future CI retains `-rs` reasons | Historical aggregate skips cannot be attributed to nodes from retained logs; item remains OPEN |

Final affected regression: **355 passed, 1 skipped** in
[provider-followup-final-regression.log](provider_verification/provider-followup-final-regression.log).
The single skip explicitly requires `MEMSPINE_TEST_TEMPORAL_ADDRESS`; this run did not
start a Temporal Server. Earlier live Temporal evidence remains a separate run.
Changed Python tooling/tests pass Ruff check and format. No full evaluation migration,
production rollout, database copying, re-embedding or automatic provider activation
was performed. PA-039 remains a separate release sweep. Prior partial progress rows
above are retained as historical evidence, superseded where stated here.

## Selected proposals and kernel optimization — 2026-10-18

The owner selected all 31 former proposals. **25 met their stated local gates;
6 retain explicit implementation/environment/release prerequisites.** Selection
is not reported as completion. PA-124/125 add the separately requested fast-library
and kernel audit. The current register has 125 stable IDs; no historical row was
renumbered or silently removed. See [the complete delivery table](PROVIDER_PROPOSED_DELIVERY.md).

Six memory-type acceptance cases execute under both memory and disk caches
(12 passes); graph/literal/accented-Latin cases pass (6); deterministic compression
acceptance passes (2). The typed GLiNER record/relation candidate adapter passes
real local SDK mapping and source-binding tests, including deep nested mutation
refusal. Candidate output never writes an assertion. Runtime model ownership and
normal Engine integration remain separate PA-118/119/121/123 gates.

The earlier affected integration regression was **411 passed, 1 skipped** before
final source-binding guards and the kernel change. The final changed-kernel,
candidate and measurement boundary gate is **45 passed**; changed source typing
and Ruff pass. Per the owner's later instruction, performance measurement for
the optimization is kernel-only; no complete-engine performance rerun was added.
The native hybrid and remote latency experiments are bounded diagnostics with
explicit filter/candidate/sample limits, not new shipped capabilities or SLOs.

## Selected performance implementation — 2026-10-18

The current register has **129 stable IDs: 91 DONE, 10 OPEN, 23 NEEDS_ENV,
4 EXCLUDED, 1 DEFERRED**. PA-118/123 now meet the shared-ownership and loader
policy gates. PA-126/127 implement bounded ranking and MinHash batching;
PA-128/129 complete bounded embedding/SQLite evaluations with defaults retained.
The historical counts above describe their earlier snapshots.

Three agents divided model lifecycle, adapter wiring and retrieval kernels;
follow-up kernels evaluated actual cached ONNX batching and SQLite client startup.
Root review strengthened cancellation/drain, mutable-settings snapshots,
load-waiter reservations, released model references, offline artifact checks and
prepass result-cache keys. No compatibility alias for `decision.model` remains;
use `models.gliner`.

Final focused gate: **192 passed, 1 intentionally skipped**, with 17 existing
LanceDB cleanup warnings. Ruff check/format pass on 26 Python files; strict
mypy passes on 16 source files. Generated defaults/catalog/schema references
pass. [Results and limits](PERFORMANCE_IMPLEMENTATION_RESULTS.md),
[test log](provider_verification/performance-final-focused.log) and
[hashed final state](provider_verification/performance-final-state.json).

PA-119/121 remain OPEN: shared weights and one offline load are not complete
source-grounding or real Engine ingestion acceptance. Existing Engine-wide
learned-alias state also needs a separate scoped review; the new result-cache
key alone does not establish complete prepass isolation. PA-051 remains the
controlled-host prerequisite for latency guarantees. Kernel speedups make no
end-to-end, answer-quality or production SLO claim. No push or rollout occurred.

## Named profiles and reasoning levels completed — 2026-10-18

PA-130–136 are DONE against their stated local gates. The current register has
**136 stable IDs: 98 DONE, 10 OPEN, 19 DEPLOYMENT_CHECK, 3 NEEDS_INPUT,
1 NEEDS_TOOLCHAIN, 4 EXCLUDED, 1 DEFERRED**. Older totals above are historical.
The 19 deployment checks require actual target-operation evidence; the three
input items require a representative host/budget, quality-call budget, or release
destination. None is a blanket blocker for local feature development.

Named profiles support independently selected LLM and GLiNER models. GLiNER
classification, semantic extraction and prepass use explicit task bindings;
identical effective settings share weights. Named LLM bindings override tier
selection; different models can serve fast extraction, checking and generation.
Escalation is explicit and bounded per routed chat call, with per-candidate
consent, per-attempt accounting, no implicit accounting serialization, native
resource keys and settings-sensitive producer/cache identities.

Focused checks: configuration **78 passed**; GLiNER/window **130 passed,
1 intentional skip**; LLM/lifecycle/privacy/combinations **148 passed**. One
example test occurs in both configuration and LLM groups. Ruff check/format
pass on 21 files; strict mypy on 15 source files. Generated references match.
Sixteen existing LanceDB cleanup warnings remain visible. No full-suite run,
complete-Engine performance test, remote model invocation or rollout occurred.

[Current guide and validated example](MULTI_MODEL_PROFILES.md),
[ADR-075](adr/ADR-075-named-model-profiles-and-escalation.md),
[hashed verification](provider_verification/model-profiles-final-state.json).
Real source-grounded ingestion remains PA-119/121; production acceptance,
quality evaluation and release delivery remain explicitly separate.

## PA-119/121 implementation started — 2026-10-18

Three agents are implementing source-bound semantic mention annotations,
window/prepass grounding, and a bounded real cached-model ingestion gate.
PA-119 and PA-121 remain OPEN until their checks finish. Mention presence must
not become an asserted state/relation, confidence must not raise trust, and
message/window provenance must survive the canonical event path. See ADR-076.

## PA-119/121 completed — 2026-10-18

Three agents implemented a single shared strict mention schema/parser, semantic
record annotations, source-bound window annotations, compiler spoof rejection,
and the real local Engine acceptance tool. SDK confidence remains ancillary;
mentions are `asserted=False` and do not create fact keys or increase trust.
Source identities come from canonical messages, not model output. Cached
annotations distinguish exact source text/version, namespace and attribution.
Empty extraction clears stale spans. Dropped duplicate annotations remain in
MERGE audit snapshots, not on the surviving record's different text.

The real gate found and fixed a generic evidence-coordinate bug: window
compilation used a rendered speaker-prefixed line, shifting offsets away from
original message content. Compilation now binds spans against original raw
records; prompt rendering is unchanged. The failed attempt is retained rather
than hidden by altering the synthetic source to include a speaker prefix.

The final offline Python 3.13.5 gate used the cached Base snapshot
`ca906247640776a07753514055be9726f9080ead`, six real GLiNER inference calls and
one explicitly deterministic supported-claim generative fixture. One window
fact was grounded, deposited and retrieved; three window annotations and three
separate semantic-probe annotations passed source/sender/time/span checks.
Namespace checks and SQLite close/reopen passed. There were no downloads,
remote/paid model calls or production changes. This is a behavioral acceptance
case, not a model-quality benchmark or coverage claim for every memory type.

Focused verification and final source hashes are recorded in
[the final verification manifest](provider_verification/gliner-grounding-final-state.json).
The unit-suite real-model placeholder remains intentionally skipped because the
explicit offline gate covers inference. Existing LanceDB cleanup warnings and
upstream model-type/TorchScript warnings remain visible. The learned alias
collection remains Engine-wide; this work does not claim isolation of all
prompt hints. See [grounding behavior](GLINER_GROUNDING_WINDOW.md) and
[the reproducible gate](GLINER_INGESTION_ACCEPTANCE.md).

Final focused gates: core grounding/window/compiler/configured-consumer group
**266 passed, 1 intentional skip**; disjoint structured/runtime/semantic-store/
outbox/rollback group **76 passed**; new session-close annotation-spoof case
**1 passed**; final gate-tool checks **4 passed** (three overlap the core group).
After the raw-coordinate fix, the affected window/grounding/compiler group
**95 passed, 1 intentional skip**, including the new repeated Unicode/raw-span
regression. These group counts overlap and must not be summed. Ruff/format pass
on 19 selected Python files; mypy passes 11 selected source/script files.
Provider defaults, stage catalogue and config snapshot match. The final affected
group retains 12 existing LanceDB cleanup warnings. No full-suite run.

## PA-038/090/091 selected — 2026-10-18

Owner-selected scope: review every repository test skip site, its source reason,
accountable maintainer role, prerequisites, replacement evidence and next review
milestone; record the next work-package decisions and ownership. Three agents
cover inventory tooling, per-site dispositions, and unresolved-item milestones.
Root reconciles the outputs and updates tracker status after focused gates.
No full suite, live-service provisioning, downloads, performance benchmark,
release delivery or automatic recurring job is selected by this work package.
Source inventory counts are not executed/parameterized pytest skip counts.

## PA-038/090/091 completed — 2026-10-18

Owners: Verification lead (skip inventory/review), subsystem maintainer roles
(per-site repair/optional gates), Engineering lead (selection and ownership).
Three agents and root reconciled **162 source skip sites**, including standalone
eval tests, conftests and the active docs/v2 unittest checker. Counts are source
sites, not executed/parameterized pytest skips: 77 importorskip, 54 skip and
31 skipif. Every site has a source-pinned review, reason, role, prerequisite,
priority, disposition and next milestone. There are zero parse errors, missing
reviews, missing source reasons or orphan/stale review IDs.

Disposition counts: 84 optional dependencies, 44 external environments,
13 inapplicable/separately covered branches, 7 platform, 6 test-guard defects,
5 model gates and 3 deferred evaluation findings. The standalone evaluation
harness's 21 legitimate optional Engine guards are preserved. Historical
aggregate skip counts remain unattributed; this review cannot recover node-level
outcomes absent from old logs.

[All per-site reasons and next actions](TEST_SKIP_REVIEW.md),
[source-pinned dispositions](provider_verification/test-skip-dispositions.json),
and [the ownership plan](PROVIDER_NEXT_MILESTONES.md) are the review record.
All 36 initially unresolved/dispositioned PA IDs have accountable roles, next
actions, prerequisites and milestones. Selected gates were reviewed 2026-10-18;
remaining items and skip dispositions have a **2026-10-25 target review**, earlier
when their stated trigger occurs. This is a planning checkpoint, not a scheduled
job, implementation ETA or authorization for an unselected gate.

At that initial review, the six guard repairs were **OPEN within M-GUARDS** (completed in the subsequent entry below), owned by Test maintainers
with Orchestration/Observability/Lexical/Graph/Read-routing maintainers as the
relevant subsystem reviewers. Repair before the PA-039 release-candidate sweep:

| Finding | Gap / next repair | Accountable role | Next milestone |
|---|---|---|---|
| Temporal runner import | Import the internal runner normally after checking the optional SDK; do not mask broken adapter imports | Orchestration/Test maintainer | M-GUARDS; review 2026-10-25 or earlier before PA-039 |
| Core trace import | Fail broken required project setup rather than skip registration assertions | Observability/Test maintainer | M-GUARDS; review 2026-10-25 or earlier before PA-039 |
| Tantivy import | Required core dependency must fail visibly in the supported core lane | Lexical/Test maintainer | M-GUARDS; review 2026-10-25 or earlier before PA-039 |
| Lazy Ladybug adapter import | Guard actual optional native SDK, not the internal module used with a fake client | Graph/Test maintainer | M-GUARDS; review 2026-10-25 or earlier before PA-039 |
| Temporal-query classifier skip | Assert the supported precondition and cover unsupported classification separately | Read-routing/Test maintainer | M-GUARDS; review 2026-10-25 or earlier before PA-039 |
| OpenTelemetry exception skip | Skip only the expected missing optional SDK; unexpected installed-SDK failures must fail | Observability/Test maintainer | M-GUARDS; review 2026-10-25 or earlier before PA-039 |

These are test-guard findings, not demonstrated engine bugs or newly completed
repairs. The three evaluation masks remain deferred under PA-107/M-EVAL.
M-GUARDS/M-EVAL are subordinate review milestones, not new PA IDs; the provider
tracker retains 136 stable IDs and now has **103 DONE / 5 OPEN**. The remaining
OPEN IDs are PA-039, PA-046, PA-053, PA-095 and PA-113.

Focused checks: **9 audit-tool tests passed**, source inventory/review freshness
and ownership/date/link consistency passed; Ruff check/format and script mypy
passed. No existing test guard behavior or engine code changed. No full pytest
sweep/collection, model call/download, live service, benchmark, deployment,
commit/push or recurring automation was run. Evidence and exact hashes:
[final verification](provider_verification/pa038-090-091-final-state.json).

## M-GUARDS selected — 2026-10-18

Owner selected the six test-guard repairs identified by PA-038. Three agents
own required imports, optional SDK/internal runner guards, and deterministic
time-view gate tests. Root refreshes the source-pinned skip review and results.
Preserve legitimate optional dependencies/services, including the standalone
harness's 21 optional Engine guards. PA-107 evaluation migration and its three
findings remain deferred. Focused checks only; no external service/model calls,
full sweep, dependency install, benchmark, deployment or release is selected.

## M-GUARDS completed — 2026-10-18

All six selected test guards are fixed using three agents and root review.
Required Tantivy/project/internal adapters use normal imports; missing optional
Temporal SDK/server and OpenTelemetry SDK remain legitimate prerequisites.
OpenTelemetry skips only genuinely absent optional packages; missing transitive
dependencies and ImportError/RuntimeError propagate. Fault injection confirms
internal Temporal import failures also propagate before any connection. The
installed OpenTelemetry in-memory exporter test passed.

The temporal-query correctness test no longer skips: exact as-of/range/history
classification preconditions and supported/unsupported mappings are asserted,
plus ordinary lookup cases. Existing read semantics were preserved.

Focused seven-file gate (six affected files plus audit tests): **80 passed,
2 skipped**. Both skips explicitly require MEMSPINE_TEST_TEMPORAL_ADDRESS; no
live server was contacted. Ruff/format pass on the six affected tests and audit
tooling. Engine code and evaluation test sources were not changed.

M-GUARDS is DONE; PA-038/090/091 remain DONE and totals stay **103 DONE / 5 OPEN**.
The current skip review has **156 sites**: 73 importorskip, 53 skip, 30 skipif.
Seven old declarations became one narrow optional-SDK guard: all six defect
findings resolved. Categories: 84 optional dependency, 44 external environment,
13 inapplicable/separately covered, 7 platform, 5 model and 3 deferred; no current
site is classified as one of the six guard defects. All source pins, reasons,
owners and milestones are refreshed. Original 162-site snapshots are preserved.

The 21 legitimate optional Engine imports in the standalone evaluator and all
three PA-107 deferred review records are unchanged. Deferred evaluation migration,
full-suite testing, optional live-service/model coverage, deployment and release
remain unselected. Next review checkpoint stays 2026-10-25 or earlier on triggers.
[Updated review](TEST_SKIP_REVIEW.md), [milestones](PROVIDER_NEXT_MILESTONES.md),
[focused log](provider_verification/guard-fixes-focused.log),
[verification](provider_verification/guard-fixes-final-state.json).

## Accepted next ingestion design — 2026-10-18

Owner accepted raw messages plus background grounded facts/annotations, graph
mention edges with asserted=False semantics, and recoverable raw data with
lossless encoding and separate summaries. See [D-99/ADR-077](adr/ADR-077-background-grounded-ingestion.md)
and [the BI-01..12 plan](BACKGROUND_INGESTION_PLAN.md). All twelve steps are
PLANNED; no ingestion/default/provider behavior changed. Start with contracts,
then durable acceptance/discovery, before activation. This workstream does not
change the 136 provider IDs or 103 DONE / 5 OPEN totals. PA-107 remains deferred.

## Engineering-first enrichment research — 2026-10-18

Accepted E1/R1/T1/E2/C1/L1/N1/B1/F1 policies are recorded in ADR-078 and
INGESTION_CONTEXT_ENRICHMENT.md. All 1,986 LoCoMo questions have automated
inventory entries and a question-only BM25 probe; source/evidence semantic review
has a separate local coverage manifest. See NON_LLM_GAP_REGISTER.md (52 generic
requirements) and LEXICAL_BACKEND_OPTIONS.md (Tantivy/OpenSearch versus BM25S/
BM25x). Numeric limits, learned-ranking/trimming and backend selection remain
under review. No provider implementation, dependency installation, service
startup or activation is included; existing provider totals are unchanged.

## Enrichment schema review draft — 2026-10-18

ENRICHMENT_SCHEMA_CONTRACTS.md and docs/schema_review/ contain exact proposed JSON shapes, synthetic examples, a 52-row coverage matrix and focused shape checks. Durable contract integration/behavioural tests remain PLANNED after accepted Q-S1..5 representation choices (B/B/A/A/B). Provider IDs/totals and deferred evaluation migration remain unchanged.

Schema revision: 41 definitions, 15 synthetic positive examples, 12 negative shape checks and all 52 coverage mappings validated after B/B/A/A/B selections. Behavioural C01-C22 and runtime rewrite remain PLANNED. No provider totals changed.

Operating review: owner selected 4K evidence tokens; proposed balanced budgets, 43 predicates and 20 copy-surface checks generated and consistency-validated. Remaining operating choices pending; BI runtime work remains PLANNED and provider totals unchanged. See ENRICHMENT_OPERATING_POLICY.md.

## Operating selections accepted — 2026-10-18

O1..O4 accepted A/A/A/A; operating JSON/table statuses refreshed and consistency validation passed for 43 predicates, 20 copy classes and selected 4K defaults. Starting limits are unmeasured. BI runtime tasks remain PLANNED; provider IDs/totals and deferred evaluation migration unchanged.

Generic catalog review: GENERIC_PREDICATE_ATTRIBUTE_CATALOG.md enumerates accepted 43/9 and proposed 10/35 extensions. Review counts/uniqueness validated; semantic refinements require selection before implementation. Stable provider IDs/counts unchanged.

Semantic structure guide created with pack ownership and usage examples; all 43 accepted predicates have exactly one defining pack. Extra vocabulary/schema refinements remain proposed; no runtime registry or provider changes.

## CPU model kernel review completed - 2026-10-18

| Item | Status | Evidence / next milestone |
|---|---|---|
| Bounded CPU candidate comparison | DONE (research only) | 10 primary model/baseline rows; 24 routing texts, eight NER texts, eight four-passage cases; four similarity probes and one label-sensitivity probe |
| Artifact consistency checks | DONE | Counts recomputed, exact source spans validated, parameter caps/timings/AST checked; portable JSON linked below |
| Role-specific shortlist | PROPOSED | Model2Vec8M candidate matching, GLiNER2.5 Small entity candidates, TinyBERT speed or MiniLM quality reranker; no activation |
| Production promotion | OPEN, owner: model/ingestion engineering | Independent relation/time/negation/unresolved checks and dependency-preserving 4K evidence tests |
| Deployment sizing / optimisation | OPEN, owner: performance engineering | Rerank48/batch16, realistic lengths, thread sweep and actual ONNX/int8 parity |

[Report](CPU_NON_LLM_BENCHMARK_RESULTS.md), [portable results and fixtures](schema_review/cpu_non_llm_kernel_results.json). Synthetic probes do not establish global best-in-class or LoCoMo accuracy. GLiClass performed poorly with these labels and remains unselected. Selected weights/dependencies were obtained for isolated benchmark use; no engine dependencies/defaults changed. BI-01..12 runtime work remains PLANNED, provider 136-ID totals remain unchanged (103 DONE / 5 OPEN), and PA-107 evaluation migration remains deferred. Research OPEN rows above are not new provider IDs.

## 48-candidate and 2K/3K kernel follow-up - 2026-10-18

This refines the preceding research milestones; it does not add provider IDs or mark runtime implementation done.

| Work item | Status | Owner | Result / next milestone |
|---|---|---|---|
| Controlled realistic message lengths | DONE (kernel research) | Performance engineering | 64/128/256 exact common-tokenizer message lengths, 16 conditions each; synthetic neutral padding, not real conversation sampling |
| 48-candidate scoring, batch16 | DONE (kernel research) | Model engineering | BM25, Model2Vec8M/32M, MiniLM embedding, TinyBERT and MiniLM cross-encoders; three warmed repeats; no truncated pairs |
| 2K/3K evidence retention | DONE (kernel research) | Retrieval engineering | 1,728 packing outcomes independently replayed; flat/full, linked/full and manually annotated-span bound; exact source offsets, costs, budget ceilings and linked completeness validated |
| Conservative comparison recommendations | PROPOSED | Architecture owner | 3K full-message evidence; 2K requires incomplete handling or validated compact spans; optional TinyBERT reranking; accepted 4K design unchanged |
| Independent natural-message / automatic-link validation | OPEN | Ingestion + retrieval engineering | Measure relation/identity/time/polarity and unresolved handling without supplied correct links/spans; include missing candidates and inaccessible/deleted evidence |
| Deployment performance envelope | OPEN (partially measured) | Performance engineering | Remaining: thread sweep, contention, batching variants, target reader tokenizer/reserves, fresh-load memory and ONNX/int8 parity |
| Packing optimisation | PROPOSED | Retrieval engineering | Obligation-aware selection, token-cost caching and read-time source-span dedup; preserve durable full excerpt copies and erasure rules |

[Full measurements](CPU_EVIDENCE_BUDGET_BENCHMARK.md); [portable outcomes](schema_review/cpu_evidence_budget_results.json). Six models x48 conditions, 288 ranking conditions total; quality is 16 unique templates repeated across lengths, not 288 independent questions. Runtime BI steps remain PLANNED; provider counts unchanged (136 IDs; 103 DONE /5 OPEN). PA-107 evaluation migration stays deferred.
