# Memory types and layers: deeper forensic review

Date: 2026-10-15. Checkout: `memspine-restructure`, commit `aac4b5705d32b42908ecd294d715b85435b6c8f7`.

This is an independent Codex code review and targeted reproduction. It is not certification of every engine feature or of the earlier other-engine survey. No production engine code changed. No external model, GPU, production database or external service was used. The table lists all findings from this bounded trace; it does not claim that every possible defect in the repository has been found.

## What was actually verified

| Evidence level | Meaning | Work completed |
|---|---|---|
| Reproduced | Executed relevant code on synthetic input | Real engine session deposit, SQLite projection, event payload, rebuild, layer counts and episode leg; helper serialization; graph leg and classification predicates |
| Code-confirmed | Inspected implementation and call sites | Legacy/v2 summary producers, lineage switch, registry, migrations, profile storage, fact adapters |
| Risk / design gap | Mechanism identified, consequence still needs integration or workload measurement | PostgreSQL upgrade with populated older tables, profile-card read integration, feedback between derived representations, scale and retrieval quality |

Reproduction script: [probe_memory_layers_2026_10_15.py](probe_memory_layers_2026_10_15.py). Captured results: [probe_memory_layers_2026_10_15.json](probe_memory_layers_2026_10_15.json).

**Correction to the earlier deep dive:** its session-close helper probe used `semantic` as the route. The real call site uses `summary`. Actual session-close records are **L1 with lineage off and L0 with lineage on**, not L2. The first report is amended accordingly.

## Complete findings register from this trace

Priority means recommended investigation/fix order, not measured benchmark loss. Related rows describe different consequences or controls; they are not independent bug counts.

| ID / priority | Finding and status | Memory types / layers | Evidence | Practical impact and limit | Recommended solution and verification |
|---|---|---|---|---|---|
| MF-01 / P1 | Session-close episodes and summaries receive the wrong layer. **Reproduced** | Semantic records representing episodic summaries; intended L2 | `derive/pipeline/integrate.py:521`; `derive/write.py:171`; real deposit probe | Lineage off produces L1; on produces L0. Both are persisted. This affects optional session-close output, not every episodic turn | Pass L2 explicitly, independently of lineage. Test both kinds and both flag states through deposit, storage and read |
| MF-02 / P1 | Legacy summary stamping replaces a correct constructor layer with L0. **Code-confirmed + helper reproduction** | Consolidation L2; entity/community legacy summaries L3 | `derive/pipeline/stages/offline_door.py:63`; callers `consolidate.py:262`, `communities.py:564`, `entity_summaries.py:300` | Semantic consolidation starts L2; entity_summary/reorganize start L3; `stamp_summary` changes them to L0 when its lineage callback exists. Full legacy stage execution was not reproduced here | Derive from the record's representation, not the plain-write route. Preserve per-producer L2/L3 distinctions; integration-test each caller |
| MF-03 / P1 | Invalid layer arguments silently fall back. **Reproduced** | All derived types | `derive/write.py:167-180` | `episode`/`summary` are accepted as strings but ignored as layers. A typo can silently change retrieval classification | Typed layer plus runtime validation; reject invalid explicit values. Separate `write_route` from `memory_type` and `representation_kind` |
| MF-04 / P1 | Layer correctness depends on whether lineage storage is enabled. **Code-confirmed** | Observation L4, standing L5, community L3, multires L2 | `derive/write.py:130-153`; `integrate.py:421-434`; v2 summary callers | The early return bypasses even valid layer overrides. Generic constructors for observation, standing, community_summary and multires yield L1. The prerequisite table does not require lineage for these stages | Stamp representation before the lineage branch. Keep lineage-edge persistence conditional. Test producer x lineage-on/off rather than enabling lineage merely to hide the problem |
| MF-05 / P1 | Rebuild preserves explicit wrong layer labels. **Reproduced** | All persisted derived records | `store/records.py:348-393`; `services/storage/sql_base.py:298`; probe `eng.rebuild()` | Session L0 remains L0 after replay; L1 stays L1 when its layer was omitted and re-derived. Fixing a writer alone does not fix historical rows | Versioned correction/upcast or audited migration keyed by producer, channel and schema. Preserve event-log integrity; compare replay and live projections. Never rewrite every L0 row |
| MF-06 / P1 | Episode leg excludes mislabelled session episodes. **Reproduced** | Episodic summary / L2 | `read/legs/episode.py:59-66`; probe | Returns `no_records:L2` despite a stored session episode. General vector/lexical search may still retrieve it; this is not proof that every answer loses it | Producer fix plus end-to-end episode lookup and source-turn lift. Report wrong-layer records distinctly from genuinely empty layers |
| MF-07 / P1 | Graph readiness is incorrectly inferred from L3 record population. **Leg reproduction; producer code traced** | Associative edges backed by semantic L1 assertions | `read/legs/graph.py:107-117`; `derive/pipeline/stages/edge_compiler_v2.py:104-108,180-208` | Compiler accepts ordinary semantic facts and creates links, not an L3 record requirement. A leg with L1 supporting records and valid convergent edges returns `no_records:L3`; an L3 count lets the same edges yield a hit. Full compiler-to-read integration remains to test | Gate on eligible compiled edges and live supporting records; track graph projection readiness separately. Do not relabel all L1 facts L3 to satisfy the gate |
| MF-08 / P2 | PostgreSQL envelope migration does not backfill older populated rows. **Code-confirmed conditional upgrade risk** | Every pre-envelope type | `services/storage/postgres/alembic/versions/pg0007_record_envelope.py:22-31`; compare SQLite `0007_record_envelope.py:86-91` | PostgreSQL adds nullable columns but no matching data backfill. A fresh database is unaffected. An old populated database can retain NULL layers; no live PostgreSQL upgrade was run | Backfill/upcast policy and migration fixture containing real pre-upgrade rows; verify data parity, not only DDL parity |
| MF-09 / P2 | NULL-layer inventory can disagree with hydrated records. **Code-confirmed** | Semantic/reflective/procedural and other non-L0 records | `services/storage/sql_base.py:400-415,633-642`; `store/records.py:348-393` | Counts coalesce NULL to L0, whereas hydration omits NULL and derives layer from type/channel. Thus the same legacy row can count as L0 but load as L1/L4/L5 | One authoritative backfill/classification contract; assert grouped counts equal counts of hydrated live records after upgrades |
| MF-10 / P2 | Fact eligibility predicates disagree about derived views. **Reproduced predicate inconsistency; downstream risk** | Semantic, reflective observations, standing answers, summaries | `derive/pipeline/stages/envelope_adapter.py:32-55`; `offline_common.py:41-94` | `is_fact` accepts all six probed derived channels. `is_input_fact` rejects observation, standing and entity_summary but accepts keyed session_close, community_summary and multires. Some real summary producers are unkeyed, so the synthetic keyed case does not prove a current feedback loop | Central representation/evidence eligibility policy with explicit exceptions. Test actual producers against reconcile, histories, graph compiler and pattern inputs; preserve source lineage and avoid counting restatements as independent support |
| MF-11 / P2 | Episode selection is layer-only rather than episode-kind-aware. **Code-confirmed semantic ambiguity** | L2 episodes, consolidation summaries, anticipation cues, multires summaries | `read/legs/episode.py:36,63-66`; `store/layers.py` channel map | Any L2 record is eligible for this leg. This can be intentional broad summary retrieval, but the name and source-lift contract imply episodes. Quality cost was not measured | Choose explicitly: narrow by representation kind, or rename/document as summary-evidence leg. Test cues and non-episode summaries, including source lift and evidence rendering |
| MF-12 / P2 | Episode vector retrieval filters after a global top-k search. **Code-confirmed recall risk** | Sparse L2 among many L0/L1 records | `read/legs/episode.py:72-73` | Only `max(4*fetch_k,50)` global vector hits are considered before filtering to episodes. Dense raw content can crowd all episodes out; lexical retrieval may rescue some | Layer-filtered vector query where supported, otherwise bounded adaptive widening. Measure sparse-layer recall, latency and token cost before selecting limits |
| MF-13 / P2 | Profile-card storage and existing profile retrieval are different representations. **Code-confirmed split; integration risk** | Profile MARKERs vs reflective L4 records and standing L5 answers | `derive/pipeline/stages/profile_card.py`; `api/engine.py:2721-2751`; `read/router_rules.py:336` | New cards are marker-backed. The legacy header describes reflective records; the router's profile label requires L5. This trace found no direct load_card consumer in read/compose. This is not proof that every personalisation path is broken | Name separate consumers: profile-card block, reflective evidence, standing-answer cache. Add a card-only namespace read test with policy, freshness and erasure checks |
| MF-14 / P2 | Registry reports conflate capability, population and effective activation. **Code-confirmed design gap** | All layers; especially graph and marker-backed profile | `store/layer_registry.py:38-59,89-108` | Worker/graph availability enables labels; any populated layer is treated as enabled. Counts omit links and MARKER artefacts. Reports do not prove that the intended stage or leg is running | Expose configured producer, adapter availability, populated artefact count, freshness and selected read route separately; use explicit runtime configuration for execution |
| MF-15 / P2 | Existing session-close tests validate content/lineage but miss the layer-to-consumer contract. **Observed coverage gap** | Session summaries, episodes and derived layers | `tests/unit/wiring/test_ow_a_close_call2_profile.py`; `test_fu_close_followups.py`; execution results below | The targeted suite passes while real deposited episodes miss the L2 leg. Unit success alone cannot establish complete functionality | Add producer-to-storage-to-replay-to-registry-to-consumer contract tests and negative controls. Keep content/idempotency/erasure tests |
| MF-16 / P3 | Selected aggregate evaluation cannot validate nine memory types or the full stack. **Evaluation scope limitation** | Working, episodic, semantic, associative, reflective, procedural, prospective, shared, resource | `evals/arms/v2-PICK3-j.json`; `docs/v2/FINAL_PICK.md`; first deep dive | Selected retrieval arm is not a full derived-stack activation. No accuracy loss or gain from these fixes was measured in this review | Mechanism-specific synthetic suites, then held-out task suites and matched-budget ablations. Report enabled features and coverage with every result |

## Producer and consumer matrix

This matrix separates observed labels from intended meaning. A record's memory type is not its retrieval layer.

| Producer | Stored type / artefact | Initial layer | Lineage-enabled deposit | Intended consumer / issue |
|---|---|---|---|---|
| Raw conversation | episodic record | L0 | L0 | Timeline, raw search and source evidence |
| Ordinary assertion | semantic record | L1 | Usually L1; channel can refine | Fact search and edge compiler |
| Session close: summary/episode | semantic record | L1 | **L0** | L2 leg misses it in either flag state |
| Legacy consolidation | semantic record | L2 | **L0** through legacy stamper | Summaries; wrong after stamping |
| Legacy entity/reorganize summary | semantic record | L3 | **L0** through legacy stamper | Entity/community derived views |
| V2 entity summary | semantic record | L3 | L3 explicit | Correct layer in inspected path |
| V2 community summary | semantic record | L1 | L3 explicit | L1 when derived door skips stamp |
| V2 multires summary | semantic record | L1 | L2 explicit | L1 when derived door skips stamp |
| Pattern observation | semantic record | L1 | L4 explicit | L1 when derived door skips stamp |
| Standing answer | semantic record | L1 | L5 explicit | L1 when derived door skips stamp |
| Compiled relationship | LINK / graph projection | Not a record layer | Links, backed by fact IDs | Must not require unrelated L3 record population |
| Profile card | MARKER / session state | Not a record layer | Marker-backed | Needs explicit card consumer and inventory |
| Reflective insight | reflective record | L4 | Depends on path | Legacy profile/insight retrieval |
| Procedure / future watch | procedural / prospective record | L5 | Depends on path | Dedicated lifecycle/trigger APIs; not ordinary factual evidence |
| Working / resource / shared bookkeeping | respective record types | L0 | Depends on path | Hot context / documents / access metadata: distinct purposes despite same layer |

## Reproduced persistence results

| Session-close kind | Lineage | Returned | SQL loaded | Event payload layer | After rebuild | Episode leg |
|---|---|---|---|---|---|---|
| episode | off | L1 | L1 | omitted, derived on load | L1 | no_records:L2 |
| summary | off | L1 | L1 | omitted, derived on load | L1 | no_records:L2 |
| episode | on | L0 | L0 | explicit L0 | L0 | no_records:L2 |
| summary | on | L0 | L0 | explicit L0 | L0 | no_records:L2 |

The graph reproduction constructs valid synthetic HopEdges rather than running the whole compiler pipeline. It proves the leg's population gate can reject usable L1-backed edges. It does not measure how often that condition occurs in existing runs.

## Decisions and options

| Decision | Recommended option | Alternative / trade-off | Acceptance gate |
|---|---|---|---|
| Separate route and representation | Keep plain WRITE routing for summaries; classify from record and producer contract | Changing route back to semantic reintroduces the documented dedup/extension problem | Extension content retained; correct layer in every flag state |
| Layer validation | Reject invalid explicit layers; typed producer-kind mapping | A warning plus fallback preserves compatibility but hides mistakes | Every registered producer passes; unknown input fails predictably |
| Where to stamp | Unconditional envelope stamping before optional lineage persistence | Require lineage for all producers: simpler but couples unrelated semantics and storage | Same representation with lineage on/off; trust and erasure remain correct |
| Graph readiness | Compiled edge/support inventory | L3 sentinel row: convenient but brittle and misleading | Valid L1-backed graph works; dead/hidden/tentative edges still excluded |
| Historical correction | Audited producer-specific migration/upcast with version | Projection-only repair disappears on replay unless replay understands it | Pre/post rebuild parity and rollback manifest |
| Fact/view eligibility | Shared explicit capability policy | Additional independent deny-lists are easy to drift | No summary-as-independent-support; retain deliberate graph/view features |
| Profile integration | Explicit marker-backed profile read block with provenance and budget | Mirror cards into records adds synchronisation and erasure work | Card-only read works; stale/erased/private support never rendered |
| Sparse-layer retrieval | Filter vector retrieval by layer/kind | Adaptive widening costs more latency and still needs a bound | Recall and p95 cost at 1%, 5%, 20% derived-layer density |
| Reporting | Separate enabled, available, populated, fresh and selected | One enabled Boolean is compact but cannot explain missing results | Studio can explain why every producer/leg ran or skipped |

### Proposed API direction, not implemented

```python
# Illustrative design; not a drop-in patch.
async def deposit_derived(record, *, write_route, representation, lineage_enabled):
    layer = PRODUCER_CONTRACT[representation].layer
    stamped = stamp_envelope(record, layer=layer)  # independent of lineage switch
    stored = await write_record(stamped, route=write_route)
    if lineage_enabled:
        await persist_lineage(stored)
    return stored
```

`representation` should describe assertion, episode, session_summary, entity_summary, observation, standing_answer, procedure, watch, source_chunk, etc. It must not be a second ungoverned list of strings. Define producer, permitted type, layer, evidence eligibility, lineage requirements, consumers, invalidation and migration together. Keep graph links and profile markers in this inventory without pretending they are record rows.

## Verification and rollout

| Stage | Required checks | Why |
|---|---|---|
| 1. Contract repair | Invalid explicit layers; actual summary route; all producer x lineage combinations | Prevent silent new misclassification |
| 2. Persistence | SQL roundtrip, event serialization, rebuild, old-event upgrade | Avoid live/replay disagreement |
| 3. Retrieval | Episode source lift; L1-backed graph; profile-card-only reads; negative evidence controls | Verify a produced memory is usable by its intended consumer |
| 4. Lifecycle | Correction, erasure, expired support, hidden parent, tenant isolation, stale caches | Preserve existing safety and history behaviour |
| 5. Quality/cost | Held-out tasks by memory type; paired wins/losses; context tokens, write calls, p95 read time | Establish benefit without overfitting the earlier examples |

Executed: **49 passed** across `test_fu_close_followups.py`, `test_ow_a_close_call2_profile.py`, `test_ow_c_patterns_standing.py`, `test_lib_23_router_rules.py`, with `-p no:deepeval -o addopts='' -q`. Warnings: pytest-asyncio fixture deprecation and LanceDB cleanup warning in existing erasure tests. No full-suite or model benchmark claim.

The forensic script ran successfully using hash embeddings and in-memory SQLite engines. It performed four real rebuilds and serialized seven producer/stamper cases; it also exercised graph and classification predicates. PostgreSQL, full legacy-stage execution, full compiled-graph integration, and actual answer-quality effects remain unmeasured.

Recommended first batch: MF-01/02/03/04 contract repair, MF-05 migration plan, MF-06/07 consumer tests. Then address classifier and profile representation contracts before expanding the number of memory layers. These fixes should be screened for regressions; no architecture can guarantee that every answer improves simultaneously.
