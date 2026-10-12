# Offline memory (consolidation and personalisation)

This area decides what MemSpine v2 derives away from the hot path, and when and how: the tiers that run
derivation (window, session close, sleep), what starts them, how they lock, restart and spend, and each
derived artefact: session summaries, stitched episodes, entity resolution, contradiction handling,
deduplication, value histories, observations, entity summaries, communities, the profile card,
reflections, standing answers, decay, foresight, procedural lessons, implicit feedback and per-observer
views. In the paper's terms it covers $\mathcal{K}_C$ (consolidate) and $\mathcal{K}_P$ (personalise).

**26 questions (O-01 to O-26).** 25 took the recommendation. One chose differently: **O-15** chose
**A, no observation layer**, where B (code-grouped, LLM-worded observations) was recommended. Nine notes
combine options: O-07, O-09, O-10, O-14, O-18, O-23, O-24 ("A + B") and O-11, O-12 ("A + B + C"). Each is
defined below as a staged or fallback design. In every case the recommended option stays the default and
the added option becomes a first stage, a fallback or a kept base layer. None of the combinations is
incompatible.

Three answers from other areas shape this one:

- **M-02 chose D, two layers (raw + facts), with the note "B + D".** Read literally, D drops summaries,
  the graph and observations, which would remove O-08, O-09, O-16 and O-17. The owner's other answers keep
  the graph and communities (M-18 A, M-19 "add extra, not remove"), the profile card (M-20 B) and mixed
  evidence bundles (R-02 C). The closest compatible reading is this: B's explicit layer field is on every
  record; raw (L0) and facts (L1) form the default-on core; every other derived layer exists but is opt-in
  per template. Confirmed by the owner (Q1, 2026-10-12).
- **G-19 chose A, a library-first inline runner.** Background work runs inside the host process. Triggers
  (O-02) work only while the engine is alive. Restart safety (O-03, O-10) therefore has to come from state
  persisted in the store (watermarks, leases, the backlog and the W-13 outbox), not from a durable runner.
- **M-19 keeps communities and adds entity summaries as an extra grouping.** This supports O-16 and O-17.

## Design in one page

| # | Element | v2 design as decided |
|---|---|---|
| 1 | Tiers (O-01) | **Window (call 1)**: facts and edges, one call per window. **Session close (call 2)**: one structured call for summary, stitched episode and profile ops. **Sleep (call 3)**: reconcile, maintain and personalise. Each artefact has one owning tier, and sleep may re-run a session task only as its fallback executor (O-07). |
| 2 | Runner (G-19) | Inline, in-process. No separate worker. dbos and taskiq stay as extras. |
| 3 | Triggers (O-02) | Per namespace, a sleep job fires on **N new deposits AND M minutes idle AND at least K hours since the last run**. Manual `sleep()` shares the same lease. Off by default, so evals stay deterministic. Trigger checks run on deposit commit, on an idle timer when the engine is long-lived, and at engine open (catch-up). |
| 4 | Unit of work (O-03) | One job per dirty namespace. It holds a persisted advisory lease, processes the changed records and their neighbours stage by stage, and advances a per-stage watermark only after that stage commits. |
| 5 | Stage order (O-04) | retention, then catch-up extraction for underived sources, reconcile backlog and resolve entities, contradiction and invalidation, near-duplicate merge, value histories, session catch-up (call-2 fallback), entity summaries, communities, profile ops catch-up and reflection, observer views, standing answers, watches and cues, procedural cases, decay, compress, event-log prune. No observation stage (O-15 A). A dependency graph (O-04 C) can come later. |
| 6 | Spend (O-05) | A token budget per namespace per cycle, spent in stage priority (reconcile, then summaries, then personalise, then maintain). Deferred work stays dirty and is counted, never dropped. No-call fast paths (fingerprints, under-cap summaries) come first. Shared with the P-22 and W-26 ledger. |
| 7 | Freshness (O-06) | Every derived record carries `covers_seq`, the newest source sequence number it covers. The read path reports how many newer sources exist, and raw evidence outranks a stale derivation. |
| 8 | Session close (O-07) | Call 2 is the default. The sleep-time session path (today's consolidate and reflect_profile) is the fallback for sessions whose call 2 failed, was skipped (no model, budget spent, tier off) or never ran because the session was never explicitly closed. |
| 9 | Summaries (O-08) | Incremental, with a full rebuild every N folds, plus a code guard against degenerate output (repetitive, over cap, or dropping named entities). A rejected output keeps the old summary. |
| 10 | Episodes (O-09) | Window fragments stay as searchable episode keys (A). Call 2 stitches them into one session episode (B). Both lift to the source turns at read (R-10). |
| 11 | Backlog (O-10) | Pending merges are persisted. An early reconcile pass fires when the backlog passes a size or age cap (B), and every regular sleep drains whatever remains (A). Items are acknowledged after commit. |
| 12 | Entity resolution (O-11) | A staged ladder: (1) rules, which are exact match, alias, MinHash and the entropy gate; (2) candidates scoped by type and co-mentions; (3) a batched LLM call with one evidence line per side for names still unresolved. A low-confidence answer gives a possible-same link, not a merge. A rules-only mode applies when no model is configured or the budget is spent. |
| 13 | Contradictions (O-12) | Stage 1 is the deterministic same-key ladder. Stage 2 builds candidates as the union of word-overlap matches and subject + predicate-family + time + embedding neighbours. Stage 3 sends them for batched adjudication, which returns supersede, contest or coexist. History is kept. |
| 14 | Near-duplicates (O-13) | Cosine merge above a threshold calibrated per embedding model. The richer text wins. Repeats count as reinforcement, with a bounded boost (R-16). A code guard blocks merges that differ in numbers, dates or negation. |
| 15 | Value histories (O-14) | Superseded records stay the source of truth (A). A deterministic, rebuildable per-(subject, attribute) view is derived from them (B). |
| 16 | Observations (O-15) | **None.** Overlap in context is handled by O-13 merges, O-14 value lines, R-17 lifting and the R-02 caps. |
| 17 | Entity summaries (O-16) | At sleep, only for changed entities. A no-call fast path applies under 2,000 characters; longer ones go in batches of 30. Off by default (M-02 reading). |
| 18 | Communities (O-17) | Leiden/LPA with summary parents, off by default, and no read leg until a global-question workload exists. Entity summaries are an extra grouping (M-19). |
| 19 | Profile (O-18, O-19) | The caller-set persona stays a separate pinned block (A). Call 2 proposes guarded add, update, delete or none ops against a typed, capped card (B). The card is stated-only by default and injected only when relevant. Reflections are kept as cited records and feed the ops path; they are not a second header. |
| 20 | Reflection (O-20) | One cited reflection per closed session, depth-capped at 2. Off by default. |
| 21 | Standing answers (O-21) | Opt-in questions registered by the host. A section is refreshed when a cited source moves past its watermark. Sources are facts, value histories and summaries (there are no observations). Later scope. |
| 22 | Decay (O-22) | Lossless access tiers (hot, warm, cold, dormant) plus zstd for cold content. Nothing is deleted except by retention or forget. |
| 23 | Foresight (O-23) | Watches from stated plans only, delivered by pull (A, always on). Anticipated-question cues are retrieval keys, never content (B, opt-in). |
| 24 | Procedural (O-24) | Template lessons from outcome receipts (A, always on). Slot-filled procedure cases from trajectories when capture is on (B, advisory only). |
| 25 | Implicit feedback (O-25) | None. Explicit feedback only. |
| 26 | Observer views (O-26) | "About X as seen by Y" views built at sleep, for the multi-agent template only (matches M-24 C). |

Interface sketch for the scheduler and the job (illustrative):

```python
class DerivationScheduler:                     # memspine.derive.scheduler
    async def on_commit(self, ns: str, seq: int) -> None: ...      # counts deposits, checks trigger
    async def on_session_closed(self, ns: str, session: str) -> None: ...  # runs call 2
    async def run_sleep(self, ns: str | None = None, *, manual: bool = False) -> CycleReport: ...

class NamespaceJob:                            # memspine.derive.job
    lease: NamespaceLease                      # persisted row, expires; shared with manual sleep()
    watermarks: dict[StageName, int]           # advanced per stage after commit
    async def run(self, stages: Sequence[Stage], budget: DerivationBudget) -> CycleReport: ...
```

## Decisions

### O-01 Where offline derivation runs: B, three tiers (window, session close, sleep)
- **Decision**: Three tiers, as ADR-066 sets out. Call 1 runs per window, call 2 once per closed session, and call 3 in the background. Each artefact has one owning tier. With G-19 A, every tier runs inline in the host process.
- **Verdict**: ok.
- **What will happen**: Session artefacts land at close, not at the next sleep, and sleep passes get smaller. Quality +1 for freshness of summaries and the profile. Hot-path latency is unchanged, because tiers 2 and 3 run after the write returns. Cost is neutral overall: work moves, it does not grow. Risk: a second scheduling path to test. Touches episodic, semantic, reflective, associative and working (profile); $\mathcal{K}_C$, $\mathcal{K}_P$.
- **Where**: `workers/schedule.py:29` (SLEEP_CYCLE_ORDER), `workers/schedule.py:80` (run_sleep_cycle), `workers/pipelines.py:3669` (session_close stub), `engine.py:13753` (`_window_session_closed`), `engine.py:13771` (close_session) -> `memspine.derive.scheduler.DerivationScheduler` with tiers `window`, `session`, `sleep`.
- **Depends on / affects**: G-19 A (inline runner), W-12 D (async call 1), W-13 C (outbox); feeds O-02, O-03, O-04, O-07.
- **Migration**: The v1 sleep() keeps working and calls the sleep tier. Session artefacts move from consolidate to call 2, with consolidate as the fallback (O-07).
- **Test or gate**: A unit test checks that each artefact has exactly one owning tier (registry check); an integration test runs close then sleep and expects no duplicate summary.

### O-02 Sleep trigger policy: C, volume + idle + minimum spacing per namespace
- **Decision**: Fire a namespace job when it has at least N new deposits AND has been idle for M minutes AND at least K hours have passed since the last run. Manual `sleep()` takes the same lease. Off by default. Under G-19 A the check runs (1) on deposit commit, (2) from an in-process idle timer when the engine is long-lived, and (3) once at engine open, so a short-lived process catches up.
- **Verdict**: ok.
- **What will happen**: Derived layers stay maintained without a host cron. Work follows real change and does not run mid-conversation. Quality +1 for long-lived deployments; eval runs are unchanged (off). Cost +1 against a fixed interval, since idle namespaces cost nothing. Risk: three knobs, and a process that exits before the idle timer fires defers its work to the next open. Touches all derived types; $\mathcal{K}_C$.
- **Where**: `workers/scheduler.py:37` (SleepScheduler, fixed interval), `config/schema.py:285` (sleep_interval_seconds), `core/policies/consolidation.py:30` (ConsolidationTrigger; HEAT unwired, `workers/pipelines.py:416`), `engine.py:13314` (sleep) -> `memspine.derive.triggers.TriggerPolicy`.
- **Depends on / affects**: O-01, G-19 A; O-03 (shared lease), O-06, O-10 (backlog caps also fire the trigger).
- **Migration**: `sleep_interval_seconds` maps onto TriggerPolicy with N=0 and M=0, so old configs keep their behaviour. HEAT is retired.
- **Test or gate**: A fake-clock unit test checks fire, no-fire and spacing; a concurrency test checks that a manual call and a trigger never run the same namespace twice.

### O-03 Unit of work, locking and restart: B, per-namespace job, dirty set, advisory lock
- **Decision**: One job per namespace that has changes past its watermark. The job takes a persisted lease (a store row with an expiry, so it survives the inline runner's process). It processes changed records and their neighbours stage by stage, and advances each stage's watermark only after that stage commits. Dependency re-dirtying applies: for example, a resolved entity re-dirties its summary.
- **Verdict**: ok.
- **What will happen**: Cost scales with change, not with store size. A crash resumes from the last committed stage watermark. Latency +1 and cost +1 against today's O(store) sweep. Risk +1, mitigated by idempotent stage effects (done markers are already there). Touches all derived types; $\mathcal{K}_C$.
- **Where**: `workers/schedule.py:80` (global sweep), `workers/pipelines.py:2947` (stage_marker) -> `memspine.derive.job.NamespaceJob`, `NamespaceLease`, `StageWatermark`.
- **Depends on / affects**: O-01, O-02, G-19 A, W-13 C (the outbox lives in the same store); enables O-04 C later.
- **Migration**: Watermarks start at 0, so the first v2 sleep is a full pass. After that, runs are incremental.
- **Test or gate**: RV01 fault injection (kill after inference, and after a partial deposit): no duplicate effects, and a resumed run completes. A lease-expiry test covers a dead holder.

### O-04 Call 3 stage order: B, reconcile, then summarise, then personalise, then maintain
- **Decision**: A fixed list in the order shown in the design table (row 5). The O-04 B list is amended for O-15 A: the observation stage is removed. Session catch-up (the call-2 fallback) runs after reconciliation and before the profile. The dependency graph (C) is the later upgrade once O-03 dirty sets exist.
- **Verdict**: ok.
- **What will happen**: Profile, reflection and summaries read reconciled facts, so the hazard of building the profile on facts the same cycle later invalidates goes away. Quality +1 (estimate). Latency and cost are unchanged. Touches all derived types; $\mathcal{K}_C$, $\mathcal{K}_P$.
- **Where**: `workers/schedule.py:29-51` (today: reflect_profile before extract_graph) -> `memspine.derive.stages.STAGE_ORDER`.
- **Depends on / affects**: O-01, O-03, O-15 A (no observation stage); orders O-10 to O-26.
- **Migration**: Reorder the stage list. Stage names keep their v1 aliases for config compatibility.
- **Test or gate**: An order test asserts that profile and reflection read only post-invalidation state; the LoCoMo+cat5 focus slice shows no loss.

### O-05 Cost caps for background work: C, token budget per namespace per cycle, priority order
- **Decision**: An optional token budget per namespace per cycle, spent in stage priority. Work over budget stays dirty and is reported as deferred coverage (RV11). No-call fast paths (D) always run first. The budget lives in the same persisted ledger as P-22 C and W-26 D. Off by default.
- **Verdict**: ok.
- **What will happen**: Predictable spend and fairness across tenants. Cost +2 (bounded). Quality 0 under normal load; under a tight budget, low-priority layers (reflection, standing answers) lag first. Risk: silent staleness unless deferred counts are surfaced, so they are. Touches all derived types; $\mathcal{K}_C$.
- **Where**: `engine.py:13788` (window_stats, call-1 counters only), `config/constants.py:439` (ENTITY_SUMMARY_FREE_CHARS fast path) -> `memspine.derive.budget.DerivationBudget`, writing to the P-22 ledger.
- **Depends on / affects**: O-03, O-04; P-22 C, W-26 D, G-05 D (cost per cycle as a guard).
- **Migration**: New counters for every sleep layer (closes G-W13's open half). The budget is off, so v1 behaviour is unchanged.
- **Test or gate**: Under an exhausted budget, deferred items are counted, re-dirtied and finished next cycle; the per-stage counters appear in `describe()`.

### O-06 Freshness guarantee for derived layers: B, staleness watermark on every derived item
- **Decision**: Each derived record stores `covers_seq`. The read path reports how many newer source records exist and lets raw evidence outrank a stale derivation. A maximum-lag SLO (C) can be added on top of O-02 later. On-demand refresh (D) is excluded because it breaks the zero-LLM read.
- **Verdict**: ok.
- **What will happen**: Readers can tell missing from pending from stale. Quality +1 (fewer stale-profile contradictions). Latency 0 (one integer comparison). Risk +1. Touches semantic, reflective, associative and working (profile); $\mathcal{K}_C$, and $\mathcal{R}$ on the reading side.
- **Where**: No v1 equivalent (summaries and cards carry no source sequence) -> a `covers_seq` field in the derived-record schema plus `memspine.read.freshness`.
- **Depends on / affects**: O-02, O-03; O-21 (refresh test), M-25 B and G-13 C (generation check), R-02 C.
- **Migration**: Existing derived records get `covers_seq = null` and are treated as stale until re-derived.
- **Test or gate**: Metamorphic test (G-04): add a newer contradicting raw turn, and the reader must rank raw above the stale summary and report staleness above zero.

### O-07 Call 2 shape at session close: B, one structured call with three sections (note "A + B")
- **Decision**: **Default (B)**: at session close, one call with the sections `summary`, `episode` and `profile_ops`. Each section is validated and deposited independently, so a failed section does not lose the others. **Fallback (A)**: the sleep-time session path (today's consolidate and reflect_profile) stays. It runs at sleep only for sessions with no committed call-2 result: call 2 failed or was skipped, there was no model (P-03 rules or skip), the budget was spent, or the session ended by idle timeout without an explicit close. Both paths use the same idempotency key per (session, membership fingerprint), so an artefact is never produced twice. This keeps O-01's one-owner rule: the session tier owns the artefact, and sleep is its catch-up executor.
- **Verdict**: ok-combined. M-02 has to be read as B + D, otherwise the summary and episode sections would have no layer to land in.
- **What will happen**: Summary, episode and profile land at close. About +1 call per session: with LoCoMo's 4.14 windows per session (measured, WINDOW_STATS_2026-10-11), that is roughly +0.05 calls per turn on top of call 1's 0.192 (estimate). One 9B call is about 2.7 s (G-W49 measurement), off the hot path. Quality +1 (estimate). Risk: the sleep fallback hides call-2 failures unless they are counted, so they are. Touches episodic, semantic and working (profile); $\mathcal{K}_C$, $\mathcal{K}_P$.
- **Where**: `workers/pipelines.py:3669` (session_close stub), `workers/pipelines.py:435` (consolidate), `workers/pipelines.py:3484` (reflect_profile), `engine.py:13771` (close_session) -> `memspine.derive.session.SessionCloser` (call 2) and `SessionCatchUp` (the sleep stage).
- **Depends on / affects**: O-01, O-08, O-09, O-18, O-23; M-02 (layer reading), P-01 C (worker tier), P-03 D.
- **Migration**: Sessions closed under v1 have sleep-made summaries, which are kept. New sessions use call 2.
- **Test or gate**: G-C9-style screen (LoCoMo+cat5): no loss outside open-domain, plus a summary spot audit. Fault test: a failed `profile_ops` section still deposits `summary`, and sleep fills only what is missing.

### O-08 Session summary update policy: C, incremental with periodic rebuild plus degenerate-output guard
- **Decision**: Fold new turns into the previous summary, rebuild fully every N folds, and keep the archived `evolve_to` chain. A code guard rejects output that is repetitive (Honcho uses fewer than 35% distinct 4-grams), longer than its cap, or missing named entities the previous version had. A rejected output keeps the old summary and is counted. Hierarchies (D) are deferred.
- **Verdict**: conflict (M-02). Under the literal M-02 D there are no summaries; under the proposed B + D reading, summaries are an opt-in L2 layer and this design stands.
- **What will happen**: A cheap guard against small-model loops. Quality +1 (estimate), cost unchanged, risk lower. Touches episodic and semantic; $\mathcal{K}_C$.
- **Where**: `workers/pipelines.py:721` (`_incremental_summary`), `config/constants.py:175` (CONSOLIDATION_SUMMARY_MAX_CHARS), `config/constants.py:709` (SESSION_SUMMARY_REBUILD_EVERY) -> `memspine.derive.session.SummaryGuard`, inside SessionCloser.
- **Depends on / affects**: O-07, M-02, R-02 C (summaries as capped notes).
- **Migration**: None for stored summaries. The guard applies to new folds only.
- **Test or gate**: Unit tests feed loop, over-cap and entity-drop outputs and expect each to be rejected; the rejection rate is reported in counters.

### O-09 Stitched episodes across windows: B, stitch at session close in call 2 (note "A + B")
- **Decision**: **Base (A)**: window fragments from call 1 are kept as live, searchable episode keys (not only archived), each lifted to its source turns (R-10 B). **Stitch (B)**: call 2's `episode` section joins a session's fragments into one dated third-person narrative that cites its windows. The fragments become its parents and are never deleted. The stitched episode is a session-level key. At read, both lift to the same turns, so they collapse into one evidence group and are not double-counted. **Fallback**: when call 2 is off, fragments alone serve, which is today's behaviour. Cross-session merge (C) stays out (BD-4).
- **Verdict**: ok-combined. Dependency: under M-02, episodes are retrieval keys lifted to raw (R-10), so they fit even a minimal layer reading.
- **What will happen**: Events that span windows become one retrievable unit, and fragments keep line precision. Quality +1 (estimate). No extra call (it is part of call 2). Index size grows by about one key per session (estimate). Touches episodic; $\mathcal{K}_C$.
- **Where**: `workers/windows.py:883` (`_store_episode`, keeps title and narrative and drops the summary) -> `memspine.derive.session.EpisodeStitcher`, with the episode leg in `memspine.read.legs.episode`.
- **Depends on / affects**: O-07, R-10 B, R-17 B, M-02.
- **Migration**: v1 fragments stay as-is. Stitching applies to sessions closed under v2, and a re-derivation backfill is optional.
- **Test or gate**: Episode leg vs facts-only on the focus slice (the G-W1 gate); a test checks that a fragment and its stitched parent lift to one group.

### O-10 Reconciling the online deposit backlog: B, persisted backlog with age and size caps (note "A + B")
- **Decision**: **Base (A)**: every regular sleep drains the whole pending-merge backlog. **Early pass (B)**: the backlog is persisted (in the W-13 outbox in the store, which survives the inline runner). When it exceeds a size cap or its oldest item exceeds an age cap, it fires an early reconcile-only job (the reconcile stages of O-04) through the O-02 trigger, which bypasses the idle condition but not the spacing guard. Items are acknowledged only after the merge commits.
- **Verdict**: ok-combined.
- **What will happen**: The window in which reads see duplicate entities becomes bounded rather than "until the next sleep". Quality +1 (estimate). Cost neutral (the same merges, earlier). Risk +1: it survives restart (RV01). Touches associative and semantic; $\mathcal{K}_C$.
- **Where**: `workers/pipelines.py:1983` (extract_graph consumes the in-memory window_edges buffer; RV01 take_edges clears before ack) -> `memspine.derive.reconcile.BacklogReconciler`, reading the outbox.
- **Depends on / affects**: O-02, O-03, O-11; W-13 C, W-19 B (pending_merge), W-25 B, G-19 A.
- **Migration**: The in-memory buffer is replaced by persisted pending rows. In-flight v1 buffers are lost at upgrade, and sleep re-extracts those turns (the existing fallback).
- **Test or gate**: RV01 fault injection after inference and after a partial deposit; backlog age and size exported (P-21 C).

### O-11 Entity resolution and LLM confirmation: C, type and context scoped, ambiguity preserved (note "A + B + C")
- **Decision**: One staged ladder. **Stage 1 (A, rules)**: exact, alias, MinHash Jaccard 0.9 and the entropy gate. This also runs online with `llm=None` (W-19 B). **Stage 2 (C, scoping)**: candidates are restricted by entity type and co-mentioned entities. **Stage 3 (B + C, LLM)**: names still unresolved go to one batched call per 50 names, showing one evidence line per side. A low-confidence verdict creates a possible-same link (no merge), and the trust guard records a contested merge. **Mode**: rules-only (stages 1 and 2) when `on_missing_model: rules` (P-03) or the O-05 budget is spent. Each stage only narrows what the next one sees.
- **Verdict**: ok-combined. This is compatible because C already contains A and B as its first stages.
- **What will happen**: Homonyms (two people called Ilse) stay apart, and nicknames (Bram for Abraham) are caught by stage 3. Quality +1 (estimate). Cost -1 against B, from the evidence lines and the scoping pass (estimate: about +20 to 40% tokens per resolve call). Risk +1. Touches associative; $\mathcal{K}_C$.
- **Where**: `memories/associative/resolution.py:164` (EntityResolver), `config/constants.py:460-481` (ENTITY_RESOLVE_*) -> `memspine.derive.resolve.EntityResolver` (v2) with a `ResolutionStage` list.
- **Depends on / affects**: O-10, O-12, O-16, O-17; W-19 B, P-03 D, RV17.
- **Migration**: Existing alias tables and contested records carry over. Possible-same links are a new link kind.
- **Test or gate**: Thresholds calibrated separately for MinHash, cosine and the LLM on held-out alias and homonym pairs (RV17). Merge precision must not drop against v1.

### O-12 Cross-key contradiction and invalidation: C, subject + predicate family + time + embedding candidates (note "A + B + C")
- **Decision**: **Stage 1 (A)**: the deterministic 13-rung same-key ladder always runs first and settles same-key conflicts without a call. **Stage 2 (B ∪ C)**: for a new live fact, the candidate set is the union of the top word-overlap facts on the same subject (B) and the facts from the same subject + predicate family, or embedding neighbours, inside the validity window (C). The set is deduplicated and capped (estimate: 5 per fact). **Stage 3**: batched adjudication returns supersede, contest (kept open) or coexist, and retraction closes validity while keeping history. **Mode**: stage 1 only when there is no model or the budget is spent.
- **Verdict**: ok-combined.
- **What will happen**: Cross-key contradictions are caught (for example "moved to Ghent" against "lives in Utrecht"). Candidate recall improves (RV17). Quality +1 (estimate). Cost -1: more adjudications, bounded by the cap and O-05. Risk +1 (a wrong verdict is reversible through history). Touches semantic and associative; $\mathcal{K}_C$.
- **Where**: `workers/pipelines.py:1902` (`_retract_contradicted`), `workers/pipelines.py:1983` (extract_graph contradictions), `config/constants.py:869` (CONTRADICTION_CANDIDATES = 3) -> `memspine.derive.contradict.ContradictionSweep`.
- **Depends on / affects**: O-11, O-04, O-14; W-21 B (integration operator), M-10, M-16.
- **Migration**: The opt-in flag becomes default-on whenever a model is bound. Past facts are not re-swept unless a backfill is requested.
- **Test or gate**: Candidate recall of B ∪ C against B on a labelled contradiction set; MAB-CR blind with no loss and calls per new fact reported.

### O-13 Near-duplicate consolidation and reinforcement: B, cosine merge plus reinforcement count
- **Decision**: Merge above a threshold calibrated per embedding model; the richer text wins. Repeats increment a reinforcement count, which gives a bounded multiplicative boost (R-16 B, off until screened). A code-only qualifier guard blocks a merge when numbers, dates or negation differ. The observation-layer alternative (D) is unavailable under O-15 A.
- **Verdict**: needs-measurement (threshold calibration, RV17).
- **What will happen**: Fewer paraphrased repeats in context. Quality +1, and less risk of merging two different numeric facts. Latency and cost +1 (no call). This is the main overlap control now that O-15 has no observations. Touches semantic; $\mathcal{K}_C$.
- **Where**: `config/constants.py:55` (DEDUP_COSINE_THRESHOLD = 0.92), the semantic write merge (corroborations) -> `memspine.derive.dedupe.NearDuplicateMerger` with a `QualifierGuard`.
- **Depends on / affects**: O-04, O-15 A, O-25; R-16 B, M-07 C (proof count).
- **Migration**: Existing corroborations seed the reinforcement count.
- **Test or gate**: Calibrate the threshold per embedding model on held-out paraphrase and numeric-variant pairs, with zero wrong numeric merges; MAB-CR shows no loss.

### O-14 Value histories for keyed state: B, deterministic history view per (subject, attribute) (note "A + B")
- **Decision**: **Source of truth (A)**: superseded and contested records with validity intervals stay the authoritative history, and as-of reads use them directly. **Derived view (B)**: a code-built, rebuildable view per (subject, attribute), listing values in effective-time order with source dates, the current value and any open contests. The view is a projection; it holds no new facts and has no LLM. Readers choose either, and the view is the default note for "what was it before" questions.
- **Verdict**: ok-combined. It matches M-13 D's value-line half.
- **What will happen**: "Before and after" questions get one compact note. Quality +1 (estimate). Cost +1 (no call). Rebuild cost is O(changed keys). Touches semantic; $\mathcal{K}_C$.
- **Where**: No v1 view (G-W29 "missing"). Records and as-of reads stay as they are -> `memspine.derive.history.ValueHistoryView`.
- **Depends on / affects**: O-12, O-13, O-21; M-13 D, G-W50 (key identity), RV04.
- **Migration**: Built from existing records on the first v2 sleep. No schema change to the records.
- **Test or gate**: Property test that the view rebuilt from records is identical to the view maintained incrementally; MAB-CR blind with context tokens down and no loss.

### O-15 Observation layer (consolidated beliefs): A, no observation layer (owner differs; B was recommended)
- **Decision**: No maintained set of consolidated observations. Readers combine facts at answer time, helped by O-13 merges, O-14 value lines, R-17 lifting and the R-02 per-block caps.
- **Verdict**: conflict (M-13). M-13 D chose "value lines for keyed state + observations for many-valued and behavioural patterns". The pattern half has no layer to live in. One question for the owner: should M-13 drop its observations half, or should observations be allowed for patterns only?
- **What will happen**: This follows from the choice: no L4 belief objects, no observation stage in O-04, and no grouping thresholds to calibrate. The forget and taint cascade is simpler (one fewer derived layer for G-13 and M-25). Expected effect against B: context tokens stay higher on preference-heavy and pattern questions, and the MAB-CR "context tokens down" gain of G-W29 is not available. Quality 0 to -1 on pattern questions (estimate), cost +2, risk lower (no inference creep). Decisions whose recommendations assumed B: **O-04** (stage removed; no revisit needed), **O-13** (D option unavailable; B chosen, so no revisit needed), **O-21** (standing answers must refresh from facts, value histories and summaries; revisit only if the owner wants observation-backed answers), **M-13** (must be revisited), **M-19** (its D alternative named "observation scopes"; under M-19's "add extra" only entity summaries apply), **M-02** (D also drops observations, so it is consistent). Touches semantic and reflective; $\mathcal{K}_C$.
- **Where**: Nothing to remove. `workers/schedule.py:59-71` (mine_facts, predict_calibrate) stay fact miners -> no `memspine.derive.observations` module.
- **Depends on / affects**: O-13, O-14 (now the only consolidation surfaces), O-21, M-13, M-19, R-02.
- **Migration**: None.
- **Test or gate**: Keep MAB-CR and PrefEval context-token baselines so a later observation layer can be judged against them.

### O-16 Entity summaries: B, sleep-time with fast path
- **Decision**: Rebuild summaries at sleep for changed entities only. The no-call path applies when the dated lines fit in 2,000 characters; longer ones go to the LLM in batches of 30. A membership watermark is used, and trust is the minimum over members. Off by default. Kept as M-19's "extra" grouping.
- **Verdict**: conflict (M-02). Under the literal D (no graph) there are no entity summaries; under the B + D reading, entity summaries are an opt-in L3 layer. M-19's note keeps them.
- **What will happen**: As in v1. It is now ordered after resolution and invalidation (O-04), so summaries no longer describe merged-away duplicates. Quality +1 (estimate), cost +1, stale between sleeps (reported by O-06). Touches associative and semantic; $\mathcal{K}_C$.
- **Where**: `workers/pipelines.py:2621` (summarize_entities), `config/constants.py:439-440` -> `memspine.derive.entity_summary.EntitySummariser`.
- **Depends on / affects**: O-11, O-04, O-06; M-02, M-19.
- **Migration**: Existing summaries carry over with `covers_seq = null` (stale until refreshed).
- **Test or gate**: Watermark-skip unit test; a forget cascade re-derives the summary (G-13 C).

### O-17 Communities and reorganisation: B, graph communities with summary parents (today)
- **Decision**: Keep Leiden plus LPA refinement, the collapse guard and incremental refresh. Off by default, and no investment until a read leg consumes it. M-19 "A + D add extra, not remove": communities stay, and entity summaries (O-16) are added as an extra grouping. D's "observation scopes" do not exist under O-15 A.
- **Verdict**: ok. M-19 overrides M-02's "drop graph" for this layer.
- **What will happen**: No change from v1. It runs after entity summaries. Quality 0 until a global-question read leg exists. Touches associative; $\mathcal{K}_C$.
- **Where**: `workers/pipelines.py:1176` (reorganize), `config/constants.py:94-119` -> `memspine.derive.communities.Reorganiser`.
- **Depends on / affects**: O-11, O-04, O-16; M-18 A, M-19 A + D, M-02.
- **Migration**: Partitions are replayed from MARKER deltas (ADR-043). No change.
- **Test or gate**: A deterministic replay test (exists). Any read leg is gated on a global-question workload.

### O-18 How the profile is updated: B, guarded operations against a card (note "A + B")
- **Decision**: **Kept base (A)**: the caller-set persona (`set_persona`) stays a separate pinned block that ops never edit, and per-session reflections are still written as cited reflective records. **Default maintenance (B)**: call 2's `profile_ops` proposes add, update, delete or none per card entry, with an observation date and stated or inferred. Code applies an op only if it is newer, cited and within the caps; a delete becomes a supersede with history. Reflections enter the card only through the same guarded ops path (O-20). When the card is on, the `PROFILE NOTES` header packing is retired, so nothing is injected twice. **Fallback**: with call 2 off or no model, the profile is the persona plus reflections, which is today's behaviour. The O-07 sleep fallback replays `profile_ops` for sessions with no committed ops.
- **Verdict**: ok-combined.
- **What will happen**: A bounded, auditable profile instead of an accumulating pile of insights. Quality +1 (estimate). Cost 0 (ops ride on call 2). Risk +1: wrong ops are possible, so the guards matter. Touches working (profile), reflective and semantic; $\mathcal{K}_P$.
- **Where**: `engine.py:9611` (set_persona), `engine.py:8544` (profile_header_packing), `workers/pipelines.py:3484` (reflect_profile), `config/constants.py:327` (PROFILE_HEADER_TOP_K) -> `memspine.derive.profile.ProfileOps` applying to `ProfileCard`.
- **Depends on / affects**: O-07, O-04, O-19, O-20; M-20 B, G-11 C (purpose scopes).
- **Migration**: The persona carries over unchanged. The card starts empty and fills from new sessions; an optional backfill replays stored reflections as ops.
- **Test or gate**: Hand audit of applied ops with precision of at least 90%; a guard test rejects older, uncited and over-cap ops.

### O-19 Profile card schema, caps and inferences: B, typed card, code caps, stated-only by default
- **Decision**: Typed entries (identity, attribute, relationship, instruction) with caps per entry and in total, each entry citing its source facts and marked stated. Inferred entries are off unless enabled. Injection is relevance-gated. This matches M-20 B.
- **Verdict**: ok.
- **What will happen**: A small, safe card that does not leak into unrelated answers (the OP-Bench lesson). Quality +1 (estimate), risk +2 (safer). Touches working (profile) and semantic; $\mathcal{K}_P$.
- **Where**: `config/constants.py:705` (PROFILE_PACK_MAX_SHARE), the relevance gating in profile packing -> `memspine.memories.profile.ProfileCard` (schema).
- **Depends on / affects**: O-18, O-26; M-20 B, P-08 C (cache-friendly layout).
- **Migration**: New record type, with no v1 data to convert.
- **Test or gate**: OP-Bench irrelevance no worse; PrefEval blind no worse; hand-audit precision of at least 90%.

### O-20 Reflection and insight generation: B, per session, cited, depth-capped
- **Decision**: One reflection call per closed session at sleep. Insights carry evidence ids, inherit taint and are depth-capped at 2. Off by default. Accepted insights feed the O-18 ops path and are not injected separately. Threshold triggering (C) waits for G-P7 importance labels.
- **Verdict**: ok.
- **What will happen**: As in v1, but reflection now runs after reconciliation (O-04) and no longer competes with the card in the header. Quality 0 to +1 (estimate). Cost about one call per session when on. Touches reflective; $\mathcal{K}_P$.
- **Where**: `workers/pipelines.py:3484` (reflect_profile), `memories/reflective/reflections.py:59` (reflect), `config/constants.py:122` (REFLECTION_DEPTH_CAP) -> `memspine.derive.reflect.Reflector`.
- **Depends on / affects**: O-18, O-04, O-05 (lowest priority under budget).
- **Migration**: Existing reflections carry over.
- **Test or gate**: Depth-cap and taint-inheritance tests (exist); an op-from-reflection test checks that it is cited.

### O-21 Standing answers (mental models): B, opt-in standing questions with delta refresh
- **Decision**: The host registers questions per namespace. An answer is refreshed by section edits when a cited source moves past its `covers_seq` (O-06). Citations are pruned when parents are forgotten. With O-15 A, the sources are facts, value histories (O-14) and summaries, not observations. Later scope; an opt-in L5 layer under the M-02 reading.
- **Verdict**: ok. O-15 A changes its input set, not its design.
- **What will happen**: Stable answers with no LLM call at read, for registered questions. Latency +2 on those reads. Cost is one refresh call per changed answer. Risk: staleness and the forget cascade, both handled by O-06 and M-25. Touches reflective and semantic; $\mathcal{K}_P$.
- **Where**: No v1 code (G-W39 missing) -> `memspine.derive.standing.StandingAnswers`.
- **Depends on / affects**: O-06, O-14, O-15 A; M-25 B, G-13 C.
- **Migration**: New and opt-in.
- **Test or gate**: Forget a cited parent and check the answer is re-derived or pruned with no residue (forgetting compliance, G-05); unchanged sections stay byte-identical.

### O-22 Decay, forgetting and compression: B, access tiers plus cold compression (today)
- **Decision**: Idle records move hot to warm (7 days), warm to cold (30 days) and cold to dormant (90 days); an access restores hot. Cold content is zstd-compressed with no change in meaning, and legal holds are respected. Nothing is deleted except by retention (G-14 D) or forget. Pinned facts are exempt from any future eviction.
- **Verdict**: ok.
- **What will happen**: As in v1. The stage runs last in O-04. Tiers do not change ranking (R-16 handles recency). Quality 0, cost +1 (storage), risk low. Touches episodic, semantic and resource; $\mathcal{K}_C$.
- **Where**: `workers/pipelines.py:868` (decay_sweep), `workers/pipelines.py:921` (compress), `config/constants.py:131-133` -> `memspine.derive.lifecycle.TierSweep`.
- **Depends on / affects**: O-04; G-14 D, R-16 B.
- **Migration**: None.
- **Test or gate**: Compress and then read gives byte-identical content; legal hold is respected (existing tests).

### O-23 Prospective memory and foresight: B, stated plans plus anticipated-question cues (note "A + B")
- **Decision**: **Always on (A)**: watches are created only from stated plans and delivered by pull through `Engine.due()`, and `check_watches` stays a sleep stage. **Opt-in addition (B)**: the `anticipate` stage writes likely future questions as retrieval cues on the turns that answer them. Cues are firewall-screened and never assembled as content (H8). The two are independent and both run at sleep: watches first, cues after. No predicted future states (C, BD-6) and no push delivery (D, later).
- **Verdict**: ok-combined.
- **What will happen**: Better recall for indirectly phrased questions when cues are on. Quality +1 (estimate). Cost about one call per session. Speculation is never stored as fact. Touches prospective; $\mathcal{K}_C$.
- **Where**: `workers/pipelines.py:977` (check_watches), `workers/pipelines.py:3446` (anticipate), `engine.py:12827` (due) -> `memspine.derive.prospective.Watches` and `CueWriter`.
- **Depends on / affects**: O-07, O-05 (cues are low priority); G-06 C (firewall).
- **Migration**: None. Cue records carry over.
- **Test or gate**: Cue screen on the LoCoMo+cat5 blind slice (gain or no loss); a test checks that cues never appear in the assembled context.

### O-24 Procedural memory: skills and lessons: B, slot-filled procedure cases from trajectories (note "A + B")
- **Decision**: **Always on (A)**: template lessons from structured outcome receipts, advisory, never executable (ADR-060). **Added when trajectory capture is enabled (B)**: the background extracts steps, preconditions and outcome into fixed slots, rendered by the same template machinery and kept at the ADVISORY stage only. Both share lesson keys, so a receipt and a trajectory about the same action reinforce one lesson. No model-written skills (C, BD-2).
- **Verdict**: ok-combined.
- **What will happen**: Agents learn from traces without free model text. Quality +1 for the agent template (estimate). Cost one extraction call per trajectory. Risk +1 (template-bound). Touches procedural; $\mathcal{K}_C$.
- **Where**: `memories/procedural/lessons.py:52` (OutcomeReceipt), `memories/procedural/lessons.py:73` (lesson_text), `memories/procedural/lessons.py:117` (lesson_key) -> `memspine.derive.procedural.LessonBuilder` and `CaseExtractor`.
- **Depends on / affects**: O-04; W-01 D (trajectory as a source envelope), G-06 C.
- **Migration**: Existing lessons carry over.
- **Test or gate**: Slot-validation and injection tests (a case cannot carry instructions outside its slots); there is no benchmark in scope, so behavioural tests only.

### O-25 Implicit feedback from conversation: A, explicit feedback only (today)
- **Decision**: Only the caller's like, dislike and note feedback. A local classifier (C) can be revisited once the decision port and labelled data exist.
- **Verdict**: ok.
- **What will happen**: No change. No self-reinforcement risk, little signal. Touches semantic and procedural; $\mathcal{K}_P$.
- **Where**: `engine.py:11620` (feedback) -> unchanged, under the v2 scoped handle (G-16 D).
- **Depends on / affects**: O-13 (the reinforcement count comes from repeats, not feedback); G-W47.
- **Migration**: None.
- **Test or gate**: None.

### O-26 Per-observer views (theory of mind): B, derived "about X as seen by Y" views at sleep
- **Decision**: For the multi-agent template only, build at sleep, per (observer, subject) pair, the facts the observer heard or stated about the subject, with a capped card that uses the O-19 schema. Built from perspective tags. The single-user default is unchanged. This matches M-24 C.
- **Verdict**: ok.
- **What will happen**: Theory-of-mind answers in multi-agent settings. Cost scales with observer pairs, bounded by O-05. Quality +1 for that template (estimate). Touches shared and semantic; $\mathcal{K}_P$.
- **Where**: `core/perspective.py:163` (Perspective tags) -> `memspine.derive.views.ObserverViews`.
- **Depends on / affects**: O-19, O-04; M-24 C, M-08, G-11 C.
- **Migration**: New and template-scoped.
- **Test or gate**: Metamorphic perspective test (what Bram told Ilse is not in Ilse's view of a third party); grant isolation tests (N1).

## Conflicts and open points

| id | Issue | Proposal | Needs |
|---|---|---|---|
| O-15, M-13 | O-15 chose no observation layer; M-13 D chose observations for many-valued and behavioural patterns. | Keep M-13's value lines (= O-14 B) and drop its observations half, with value lines plus capped fact notes serving patterns. | needs-owner: drop M-13's observation half, or allow observations for patterns only? |
| M-02 vs O-08, O-09, O-16, O-17, O-21 | M-02 D literally drops summaries, the graph and observations; the owner's other answers (M-18, M-19, M-20, R-02) keep graph, communities and card. | Read "B + D" as: an explicit layer field on every record; raw and facts as the default-on core; summaries, episodes, graph, communities, entity summaries, profile and standing answers as opt-in layers per template. | needs-owner confirmation of this reading |
| G-19 vs O-02, O-03, O-10 | The inline runner has no durable worker; triggers fire only while the process lives. | Persist the leases, watermarks and backlog in the store (the W-13 outbox); check triggers at commit, on an idle timer and at engine open. | Confirm with the write area that the W-13 outbox lives in the store under G-19 A |
| O-07, O-01 | "A + B" could look like two owners of a session artefact. | The session tier owns it; sleep re-runs the same task only for sessions without a committed result, under one idempotency key. | none |
| O-18, O-20 | Reflections plus a card could double-inject the profile. | Reflections feed card ops only; the `PROFILE NOTES` header is retired when the card is on. | none |
| O-11, O-12, P-03 | Rules-only modes need a model-less path, but P-03's default is `error`. | The rules-only stages run when `on_missing_model: rules` is set; the default stays fail-fast. | none |
| O-12 | Unioning the B and C candidates raises adjudication calls. | Cap at about 5 candidates per new fact, batch the calls, and report calls per fact under O-05. | measurement (below) |
| O-21, O-15 | Standing answers assumed observations as an input. | Refresh from facts, value histories and summaries. | none (later scope) |

## Measurements to run before building

| What | Screen or dataset | Gate |
|---|---|---|
| Call 2 (O-07, O-08, O-09) | G-C9-style screen on LoCoMo+cat5 (focus and blind), plus a summary spot audit | No loss outside open-domain; guard rejections counted; calls per session reported |
| Profile ops and card (O-18, O-19) | OP-Bench (irrelevance), PrefEval blind, hand audit of ops | Irrelevance no worse; PrefEval no worse; op precision of at least 90% |
| Near-duplicate threshold (O-13) | Held-out paraphrase and numeric-variant pairs per embedding model; MAB-CR | Zero wrong numeric merges; MAB-CR no loss |
| Resolution thresholds (O-11) | Held-out alias and homonym pairs (RV17), separate for MinHash, cosine and LLM | Merge precision no worse than v1; homonyms kept apart |
| Contradiction candidates (O-12) | Labelled cross-key contradiction set; MAB-CR blind | Recall of B ∪ C above B; MAB-CR no loss; calls per new fact reported |
| Value histories (O-14) | MAB-CR blind, ConvoMem | Context tokens down, no loss |
| Restart and lease (O-02, O-03, O-10) | RV01 fault injection (kill after inference, after partial deposit) | No duplicate effects; resume completes; backlog age within cap |
| Budget (O-05) | Synthetic backfill with a tight budget | Deferred work counted and finished next cycle; cost per cycle within the G-05 tolerance |
| Freshness (O-06) | Metamorphic suite (G-04) | Raw outranks a stale derivation; staleness reported |
| Anticipated cues (O-23) | LoCoMo+cat5 blind slice | Gain or no loss; cues never in the assembled context |
| Stitched episodes (O-09) | Focus slice, episode leg against facts-only (G-W1 gate) | Beats facts-only on focus, or adds value on the blind slice |


## Owner follow-up (2026-10-12)

- **Q1 Layer field, two layers on** (as proposed): Every record has an explicit `layer` field; raw (L0) and facts (L1) are populated by default; episodes, summaries, graph, profile, observations and communities are opt-in layers.
- **Q2 Durable outbox** (as proposed): Library-first inline runner draining a durable outbox in the same store; leftover work resumes on the next open; dbos and taskiq are optional drainers.
- **Q3 Value histories plus pattern observations** (changed): Build the deterministic value-history view AND an observation layer for recurring patterns only (code-grouped, model-worded only for groups that changed). O-15 is reopened from 'none' to 'patterns only'; observations are an opt-in L4 layer (Q1). New stage `observe_patterns` after value histories in O-04; budgeted under O-05; observations are cited records (L4, opt-in), refreshed only for changed groups; O-21 standing answers may cite them.

## Change log

- 2026-10-11: First version. 26 offline decisions verified against the owner's answers. Combinations defined for O-07, O-09, O-10, O-11, O-12, O-14, O-18, O-23 and O-24. O-15 A consequences traced (conflict with M-13). M-02 "B + D" reading proposed (needs owner). G-19 A inline-runner effects on O-02, O-03 and O-10 resolved through state persisted in the store.
- 2026-10-12: owner follow-up answers recorded (Q1, Q2, Q3).
- 2026-10-12 (later): stale owner-confirmation markers resolved from the Q1-Q8 answers.
