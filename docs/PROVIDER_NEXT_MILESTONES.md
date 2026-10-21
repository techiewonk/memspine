# Provider work packages, accountable roles and next milestones

Reviewed **2026-10-18** against [PROVIDER_TODO.md](PROVIDER_TODO.md), the
[GLiNER ingestion acceptance](GLINER_INGESTION_ACCEPTANCE.md),
[performance follow-up](PROVIDER_PERFORMANCE_FOLLOWUP.md),
[delivery packet](PROVIDER_PROPOSED_DELIVERY.md) and
[Temporal operations](TEMPORAL_RESONATE_OPERATIONS.md). The tracker controls
current status; older reports describe the evidence available at their run date.

The selected work is **PA-038, PA-090 and PA-091 only**. This document assigns
accountable *roles*, not named people or running agents. The engineering lead
maps each role to an actual maintainer when activating its package. A role
assignment does not authorize remote operations, paid calls or deployment.

## Selected scope and completion gates

| Item | Scope selected by the user | Accountable role | Gate | Excluded from this package |
|---|---|---|---|---|
| PA-038 | Inventory all active test skip declarations (including evals and documentation checks), compare retained skip evidence, explain every discovered skip and its prerequisite, and run focused inventory checks | Verification lead; individual callsites delegate to the owning subsystem maintainer | Machine-readable and human-readable inventory, reason/owner/environment/disposition per callsite, parse and completeness checks, explicit historical evidence limits | Full unit sweep, starting every optional service, removing justified skips merely to improve counts, claiming runtime branch coverage from a static inventory |
| PA-090 | Select the above local verification package and record its gates, sequencing and exclusions | Engineering lead | This scope is explicit; PA-038 result is linked from the tracker; future work remains separately gated | Startup/load benchmarking, deployment acceptance, quality evaluation, release publication and unrequested feature changes |
| PA-091 | Assign an accountable role and a next review milestone to each selected and remaining unresolved/disposition item | Engineering lead | Every ID below has an owner, concrete next action, acceptance gate, prerequisite and review trigger; check table coverage against tracker IDs | Invented individual ownership, delivery dates, production approval or reopening excluded/deferred work |

Milestone review date is **2026-10-18** for the selected three gates. Every
remaining unresolved/disposition item has a target review date of
**2026-10-25**, or earlier when its specified trigger occurs. This is a planning
checkpoint, not a completion ETA, scheduled job or authorization to run its gate.
For an inactive package the trigger means “review before activating this work,”
not a background monitoring commitment. No recurring automation is created.
PA-090's older dependency list (PA-046/056) records broader sequencing; those
items are not prerequisites for this local documentation/skip-review package.

## Local and release follow-ups

Statuses here describe the work at the start of this pass. PA-038/090/091 may
be marked DONE in the tracker only after their gates above have been checked.

| ID | Starting status | Accountable owner | Next action | Acceptance gate | Prerequisite | Next review milestone |
|---|---|---|---|---|---|---|
| PA-038 | OPEN, selected | Verification lead | Reconcile current skip callsites and retained runtime evidence; attach subsystem owners | Every discovered skip has a reason, owner, prerequisite and disposition; inventory checks pass | Current test source and retained logs | Target review **2026-10-18**; earlier on trigger: This pass, 2026-10-18; thereafter whenever a skip declaration or relevant optional gate changes |
| PA-039 | OPEN | Verification lead | Plan one final-revision unit sweep in resource-safe chunks and retain all outcomes | One coherent release-candidate evidence packet closes the historical failure/retest split | PA-038, stable candidate revision, explicit authorization for the broader sweep | Target review **2026-10-25**; earlier on trigger: Before declaring the next release candidate verified; do not run in this focused-only pass |
| PA-046 | OPEN | Performance maintainer; budget approval by application owner | Agree an absolute cold-start budget, then accept or address comparable measured overhead | Written budget and evidence-backed acceptance or fix; historical +12.1% is not presented as current overhead | Budget decision; representative measurement scope | Target review **2026-10-25**; earlier on trigger: When the owner selects a startup budget; broader Engine measurements require authorization beyond kernel-only scope |
| PA-051 | NEEDS_INPUT | Performance maintainer; host supplied by application owner | Specify a representative controlled host, load isolation and trial plan | Host conditions, repetitions and variability recorded before latency guarantees | PA-046 budget and host access; benchmark authorization | Target review **2026-10-25**; earlier on trigger: On receipt of host/budget details, before any new latency claim |
| PA-053 | OPEN | Ingestion/worker maintainer | Define queue capacity/overload policy and a derivation-lag probe, then request the scoped load gate | Foreground p95, queue saturation, derivation lag and burst recovery meet agreed budgets | PA-052 evidence, selected queue configuration/budgets, explicit exception to kernel-only performance scope | Target review **2026-10-25**; earlier on trigger: Before selecting concurrency/backpressure acceptance; no load run in this pass |
| PA-090 | OPEN, selected | Engineering lead | Record selected scope, gates, exclusions and sequence | Selected package is concrete and reviewable in this document and tracker | User selection of PA-038/090/091 already supplied | Target review **2026-10-18**; earlier on trigger: This pass, 2026-10-18; again when the next package is selected |
| PA-091 | OPEN, selected | Engineering lead | Check owner and milestone coverage against every unresolved/disposition tracker ID | No covered ID missing an accountable role, prerequisite or next milestone | PA-090 scope and current tracker | Target review **2026-10-18**; earlier on trigger: This pass, 2026-10-18; whenever unresolved IDs/statuses change |
| PA-095 | OPEN | Release maintainer | Refresh diff/evidence review packet; create PR only for the requested destination | Reviewer-ready change summary, evidence, limitations and requested PR | PA-094 packet; selected final revision; PR destination/request | Target review **2026-10-25**; earlier on trigger: At release-candidate review, or when PR destination is supplied |
| PA-113 | OPEN | Dependency maintainer | Run the public metadata audit at release preparation and review intentional caps | Dated Python 3.13/extras audit with each upgrade, retained cap or exception explained | PA-101 audit tooling; network available for metadata; focused checks for chosen updates | Target review **2026-10-25**; earlier on trigger: Before the next dependency/release update; no scheduled automatic upgrade |

## Deployment acceptance remains a separate workstream

Local development does not wait for all production details. These checks require
the actual target, controller and operator authority; local tests are starting
evidence, not a substitute. The deployment owner supplies credentials via a
secret mechanism and approves destructive drills against disposable/synthetic
target data. Do not record secrets in this document or test logs.

| ID | Status | Accountable owner | Next action | Acceptance gate | Prerequisite | Next review milestone |
|---|---|---|---|---|---|---|
| PA-056 | DEPLOYMENT_CHECK | Application/deployment owner | Supply deployment ID, environment, controller owner and runbook | Target ownership and operational boundaries documented | Target deployment selected | Target review **2026-10-25**; earlier on trigger: Before activating any target-specific check |
| PA-057 | DEPLOYMENT_CHECK | Platform maintainer | Pin selected service/model versions and credential-free effective config identity | Reproducible deployment manifest | PA-056 | Target review **2026-10-25**; earlier on trigger: At first target-manifest review and each target revision |
| PA-058 | DEPLOYMENT_CHECK | Platform/security maintainer | Check least-privilege TLS, credentials and network routes | Authorized connections pass without secret disclosure | PA-057 and target access | Target review **2026-10-25**; earlier on trigger: Before first target integration or credential/network change |
| PA-059 | DEPLOYMENT_CHECK | Application integration maintainer | Connect the actual service-change controller to native actions | Each phase returns actual native action evidence | PA-056; application controller integration | Target review **2026-10-25**; earlier on trigger: Before exercising a controlled service change |
| PA-060 | DEPLOYMENT_CHECK | Controller maintainer | Verify atomic epoch fencing, persisted idempotency and ambiguous HTTP reconciliation | Stale epochs refused; unknown outcomes resolve without duplicate effects | PA-059; durable controller storage | Target review **2026-10-25**; earlier on trigger: Before first controller cutover and on fencing protocol changes |
| PA-061 | DEPLOYMENT_CHECK | Model serving maintainer | Exercise LLM/reranker revision activation against actual servers | One activation; subsequent work uses the selected revision | PA-060 and pinned model services | Target review **2026-10-25**; earlier on trigger: Before any target model-revision switch |
| PA-062 | DEPLOYMENT_CHECK | Cache maintainer | Exercise isolated cache revision activation/cold start | Old state cannot contaminate new serving revision | PA-060 and target cache | Target review **2026-10-25**; earlier on trigger: Before any target cache generation switch |
| PA-063 | DEPLOYMENT_CHECK | Orchestration maintainer | Exercise old-admission stop, leases and acknowledgements with actual workers | Safe handover supported by admission and lease evidence | PA-060 and selected worker environment | Target review **2026-10-25**; earlier on trigger: Before worker-generation handover |
| PA-064 | DEPLOYMENT_CHECK | Application/deployment owner | Supply enforceable quiescence proof for prior owners | Previous owners cannot resume during recovery/retry | PA-056 and operational stop/fencing mechanism | Target review **2026-10-25**; earlier on trigger: Before permitting hard-crash cleanup recovery |
| PA-065 | DEPLOYMENT_CHECK | Recovery maintainer | Run authorized killed-worker and stale-owner drills | Recovery refuses stale writes and records guard evidence | PA-064; disposable drill scope | Target review **2026-10-25**; earlier on trigger: After guard acceptance, before recovery sign-off |
| PA-066 | DEPLOYMENT_CHECK | Security/tenant isolation maintainer | Probe synthetic tenants across selected native providers | No forbidden search, overwrite or erasure across scopes | PA-058; synthetic tenant identities/data | Target review **2026-10-25**; earlier on trigger: Before production tenant acceptance or scope-routing changes |
| PA-067 | DEPLOYMENT_CHECK | Platform/security maintainer | Separate bootstrap and serving privileges natively | Serving cannot perform privileged provisioning | PA-058; distinct target identities | Target review **2026-10-25**; earlier on trigger: Before serving-role approval or privilege changes |
| PA-068 | DEPLOYMENT_CHECK | Storage/backup maintainer | Restore a deployment-owned synthetic backup | Expected records and boundaries verified after restore | PA-057; isolated restore destination and authorization | Target review **2026-10-25**; earlier on trigger: Before backup readiness is claimed; repeat after backup topology changes |
| PA-069 | DEPLOYMENT_CHECK | Platform reliability maintainer | Exercise loss/restart/reconnect for selected dependencies | Bounded documented failure and recovery behavior | PA-058; authorized fault-injection scope | Target review **2026-10-25**; earlier on trigger: Before dependency availability sign-off |
| PA-070 | DEPLOYMENT_CHECK | Data retention owner | Check record/index erasure and document WAL/replica/backup limits | Target retention/deletion behavior matches approved policy without false physical-erasure claims | PA-067/068; approved retention policy | Target review **2026-10-25**; earlier on trigger: Before deletion-policy acceptance or storage-policy changes |
| PA-071 | DEPLOYMENT_CHECK | Observability maintainer | Route and test provider health, projection lag and recovery alerts | Actionable thresholds and real delivery evidence | PA-056; alert destination/owner supplied | Target review **2026-10-25**; earlier on trigger: Before operational monitoring sign-off |
| PA-072 | DEPLOYMENT_CHECK | Application acceptance owner | Run the explicitly authorized representative workload | Measured latency/error/capacity/model-quality gates pass | PA-061/062/063/066/069; workload, budgets and authority | Target review **2026-10-25**; earlier on trigger: After prerequisites pass and before deployment acceptance |
| PA-073 | DEPLOYMENT_CHECK | Application/deployment owner | Review all required evidence and record acceptance separately from health | Explicit owner sign-off on measured gates | PA-065/068/070/072 | Target review **2026-10-25**; earlier on trigger: At target acceptance review; local tests never close this row |
| PA-082 | DEPLOYMENT_CHECK | Prospective memory/worker maintainer | Exercise actual watch consumer execution and handover | No duplicate work or forbidden-scope firing; delivery boundaries documented | PA-063/074; actual worker/consumer protocol | Target review **2026-10-25**; earlier on trigger: Before enabling prospective watches in the target |

## Input, toolchain and preserved dispositions

| ID | Status | Accountable owner | Next action | Acceptance gate or preserved boundary | Prerequisite | Next review milestone |
|---|---|---|---|---|---|---|
| PA-088 | NEEDS_INPUT | Evaluation maintainer | Propose held-out cases, reader/judge configuration, privacy and call/token limits for approval | Clean model-quality evidence without gold leakage; quality claims match evaluated scope | PA-085/087; explicitly budgeted endpoint/models and evaluation scope | Target review **2026-10-25**; earlier on trigger: When the owner selects quality evaluation and supplies its budget |
| PA-096 | NEEDS_INPUT | Release maintainer; authorization by repository owner | Record requested destination/workflow/revision before publishing | Only the explicitly authorized push/release is performed | PA-095; destination and publication authorization | Target review **2026-10-25**; earlier on trigger: On receipt of release instructions; no push in this pass |
| PA-109 | NEEDS_TOOLCHAIN | Windows/native platform maintainer | Prepare supported compiler/CMake toolchain and native inference gate | Optional llama-cpp-python builds and inference pass on Windows Python 3.13 | PA-102; toolchain installation selected/available and test model | Target review **2026-10-25**; earlier on trigger: When native Windows build support is selected and prerequisites exist |
| PA-107 | DEFERRED | Evaluation maintainer | Preserve original failure evidence; do not migrate until explicitly resumed | Active generators/fixtures migrated and relevant failures resolved only in a separately selected package | Owner explicitly resumes deferred evaluation migration | Target review **2026-10-25**; earlier on trigger: On explicit resumption or before relying on affected evaluation tooling |
| PA-097 | EXCLUDED | Storage architecture maintainer | Preserve exclusion; no automatic authoritative DB copying | Reopen only under a new explicit owner decision | New owner decision | Target review **2026-10-25**; earlier on trigger: Only if that decision is requested |
| PA-098 | EXCLUDED | Model/data architecture maintainer | Preserve exclusion; no automatic historical re-embedding | Embedder selection alone is not permission to migrate history | New owner decision | Target review **2026-10-25**; earlier on trigger: Only if that decision is requested |
| PA-099 | EXCLUDED | Provider architecture maintainer | Reject compatibility registries, aliases and RETIRED maps | Direct breaking provider schema remains the contract | New owner decision to change architecture | Target review **2026-10-25**; earlier on trigger: On proposed compatibility changes; otherwise no active work |
| PA-100 | EXCLUDED | Application acceptance owner | Preserve prohibition on inferring production approval from local health/tests | Only actual deployment evidence and owner sign-off support approval | PA-073 evidence for any future production claim | Target review **2026-10-25**; earlier on trigger: Whenever a release/deployment acceptance claim is prepared |

These tables cover **36 IDs** unresolved or dispositioned at the start of this
pass: 8 OPEN, 19 DEPLOYMENT_CHECK, 3 NEEDS_INPUT, 1 NEEDS_TOOLCHAIN, 1 DEFERRED
and 4 EXCLUDED. Completing the selected three changes their tracker status;
it does not erase their ownership or the remaining 33 rows.

## Skip-review repair dispositions

Reviewing a skip closes the inventory obligation, not the defect or missing
coverage it exposes. The [skip review](TEST_SKIP_REVIEW.md) and its
machine-readable inventory carry exact callsites and evidence; do not remove a
skip without replacing its intended environment boundary with a meaningful gate.

| Milestone | Priority / state | Accountable owner | Next action | Acceptance / next review |
|---|---|---|---|---|
| M-GUARDS | P1, DONE 2026-10-18 | Test maintainers with subsystem reviewers | All six selected guards repaired: required imports fail normally, temporal assertions execute, absent optional SDK alone skips; legitimate evaluator imports retained | Focused checks 80 passed / 2 live-address skips; installed exporter executed. Source review refreshed to 156 sites, no current defect category; evidence in guard-fixes-final-state.json. Optional/deferred gates remain separate. |
| M-EVAL | P1, DEFERRED with PA-107 | Evaluation maintainer | On explicit PA-107 resumption, narrow three evaluation masks: broad v2-arm import exception, unsupported other-branch prompt, forensics harness ModuleNotFoundError classification | Required internal import failures cannot masquerade as missing optional dependencies; affected focused tests pass. Target review **2026-10-25** for disposition only; implementation waits for explicit resumption. |

## Separate Temporal directive boundaries

The ACD checklist is not part of the 136 PA IDs. Its remaining work still needs
ownership rather than being implied complete by the maintenance-runner gate.

| Directive | Accountable owner | Next action and acceptance | Prerequisite and next review |
|---|---|---|---|
| ACD-04/07 ingestion outbox | Ingestion/orchestration maintainer | Select a separate integration design; retain one canonical durable ingestion route and prove retry/idempotency before changing transport | Not selected in this pass; review when ingestion transport integration is requested |
| ACD-09 operational drain | Application/deployment owner | Establish whether old deployed DBOS jobs exist; stop/resolve them before rollout; no history conversion | Actual old deployment, if any; review before first replacement rollout |
| ACD-11 hard container failover | Orchestration/platform maintainer | Verify journal volume placement, fencing and killed-container recovery on the actual target | Persistent-volume and scheduling environment; review before claiming container failover |
| ACD-12 production rollout | Application acceptance owner | Validate external cluster/auth, journal placement and representative workload | Target deployment and operator approval; review at deployment acceptance |

## Recommended sequence and evidence limits

1. Close PA-038/090/091 against their selected local gates. Inventory completeness
   means known source skip declarations are accounted for; it does not mean all
   optional gates or historical skipped nodes were executed.
2. Select the next package explicitly. Recommended release preparation is
   PA-113 dependency audit, then PA-039 on a stable candidate, then PA-095 review.
   Perform PA-096 only under the requested publication workflow.
3. Address PA-046/051/053 when budget/host/benchmark scope is selected. Existing
   kernel-only authorization does not permit a new whole-Engine load benchmark.
4. Activate deployment rows when an actual target exists. Keep quality evaluation,
   optional native toolchain work and deferred evaluation migration independent.

Current GLiNER evidence includes real cached-model message/window ingestion and
reopen checks. It uses an explicitly deterministic generative fixture, not an
answer-quality evaluation. Typed record/relation candidates remain separate from
automatically asserted facts. Earlier delivery prose using NEEDS_ENV or stating
that Engine mention integration is pending is historical; PA-119/121 and the
current tracker supersede that narrower earlier snapshot. The Engine-wide
learned-alias collection remains a documented limitation; no scoped-isolation
completion is inferred from the newer mention-result cache.

No full test sweep, production drill, remote model call, performance benchmark,
push or rollout is performed by this planning package.
