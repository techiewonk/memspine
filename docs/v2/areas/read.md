# Retrieval and composition

This area decides how a v2 read turns a question into a bounded context: read tiers, query analysis,
routing, the retrieval legs, fusion and the protected pool, reranking, lifting derived hits to their
sources, packing, windows, rendering, the relevance gate, the deep tier, external knowledge, the time
view, latency and caching. It covers the retrieve operator $\mathcal{R}$ and the compose operator
$\mathcal{C}$ (26 questions, R-01 to R-26).

The owner took the recommendation on 24 of 26. Two answers differ:

- **R-15** chose A, "hit only, windows after (today)", over C, whole-window rerank. G-W28
  (contextualised window rerank, MemMachine style) is therefore **not taken**.
- **R-22** chose B, "sufficiency check plus one rewrite round", over D, "no engine loop, tools only".
  The only measured deep loop (E02 slot loop, f-slot-i2) was net -3 at about 8x wall time, so B needs
  its own screen and gate before it ships.

Two notes combine options: R-01 "A + B" (config sets the deployment default, the caller picks depth
per call) and R-11 "A + C" (RRF k=60 stays the formula, applied after merging correlated legs).

Standing owner decision: **jina-reranker-v3.5 is the reranker**, with no further reranker testing.
Two answers in other areas shape this one. M-02 keeps two layers by default (raw + facts) and O-15
has no observation layer, so the derived notes, episode and graph legs that some read
recommendations assumed are now optional.

Measured anchors cited below:

- **clean-jina-full**, the pool-protect per_leg arm with Jina at commit 888b888: 81.4% on 1,540
  LoCoMo cat 1-4. By category: multi-hop 66.7, temporal 83.2, open-domain 42.7, single-hop 90.0.
  Mean context 2,097 tokens, p50 764 ms.
- **A03 typed query contract**, focus slice f-contract-i2: net +9 (+15/-6) at 2.04 vs 2.03 calls and
  2,168 vs 2,162 tokens. The full run (contract-jina-full) is pending.
- **f-chain-i2** (read-time fact chain): net -1.
- **f-slot-i2** (slot loop): net -3, fired on 150 of 437, 3 h 25 min against about 25 min.

All other effect sizes are estimates.

## Design in one page

| # | Stage | v2 design as decided | Op |
|---|---|---|---|
| 1 | Tier | The per-call `depth` is `fast`, `standard` or `deep`. The config sets the deployment default (fast) and which tiers are allowed (R-01 A+B). | R |
| 2 | Analyse | The heuristic typed query contract is on by default and makes zero calls. The LLM contract stays an opt-in for multilingual use (R-03). | R |
| 3 | Route | A rule router driven by the contract. LLM rewrites run only in the deep tier (R-04). | R |
| 4 | Legs | Vector, BM25 and temporal always run. Entity, graph, episode and session legs run when the router switches them on (R-05). BM25 uses language-aware tokenisers (R-06). The temporal leg uses a window anchored to the question time and matches event intervals once M-10 lands (R-07). The entity leg uses an inverse-degree penalty (R-08). The graph leg is a one-hop convergent expansion (R-09). The episode leg lifts its hits to parent turns (R-10). | R |
| 5 | Fuse | Legs are grouped into source families and merged within each family. RRF k=60 then fuses across families. Plain RRF is the fallback (R-11 A+C). | R |
| 6 | Pool | Slots are reserved per source family (R-12). Pool size is set by the tier, and floors are calibrated per store (R-13). | R |
| 7 | Lift (re-admit, dedupe, cap) | Derived hits are lifted to their parent turns, re-admitted, deduplicated and capped (at most 64, R-Q4) before reranking; derived text appears only as capped notes (R-17; R-Q1, ER-21 B). | R |
| 8 | Rerank | Jina v3.5 scores the **single hit** (R-14, R-15 A) on the lifted, capped set, so all hits share one reranker score. A bounded multiplicative band for recency and reinforcement is built but off until screened (R-16). | R |
| 9 | Gate | A store-calibrated relevance gate with a bypass for named references. When it fires, the read returns an empty context with a reason (R-21). | R |
| 10 | Time view | The contract's time scope picks current state, history or as-of. A compact value-history line is the fallback (R-24). | R |
| 11 | Windows | Each hit expands into a token-sized window after reranking. Windows shrink as the budget fills and merge when they overlap (R-19). | C |
| 12 | Pack | A rank-order packer that skips oversize items and continues, with exact reader-token counts. Dynamic block caps redistribute unused share (R-18). | C |
| 13 | Render | Typed sections with provenance tags, a one-line contract header and integer aliases (R-20). | C |
| 14 | Deep tier | Opt-in only: one sufficiency judge, then at most one rewritten search, then answer (R-22 B). It is gated by a new screen. | R, C |
| 15 | External | Opt-in and gated, behind a privacy filter, in a separate `[public knowledge]` block (R-23). | R |
| 16 | Speed | Legs run in parallel with a per-leg timeout. Each tier has a p95 SLO that CI tests (R-25). | R |
| 17 | Cache | Embeddings and snapshots are cached, and prompts use a provider prompt-cache layout. Snapshot cache keys include effective scope, viewer/purpose, time view, policy/model versions, record versions and invalidation generations (ER-31 B); result-content caches (rerank-score, composed-context) stay disabled until ER-31 keys exist (R-26). | C |

Interface sketch (read entry, v2 `memspine.read`):

```python
class ReadRequest(BaseModel):
    query: str
    depth: Literal["fast", "standard", "deep"] | None = None   # None -> config default (R-01)
    as_of_valid: datetime | None = None; as_of_known: datetime | None = None  # M-11
    viewer: str  # bound from handle principal; required in multi-party namespaces (ER-29 C; G-12)
    budget_tokens: int | None = None

class Leg(Protocol):            # one retrieval leg (R-05)
    name: str; family: Literal["dense", "lexical", "structured", "derived"]
    async def run(self, q: QueryContract, fetch_k: int, deadline: float) -> list[LegHit]: ...
```

## Decisions

### R-01 Read tiers and who chooses depth: B, per-call depth (note "A + B")

- **Decision.** The combined design keeps both behaviours:
  - **Default (A).** The deployment config sets `read.default_depth` (default `fast`) and
    `read.allowed_depths`. It also defines the contents of each tier: which enhancers, call caps and
    token caps.
  - **Per call (B).** `depth` on the call overrides the default, but only within the allowed set.
    A call without `depth` behaves exactly as A does today.
  - **Tier contents.** `fast` makes zero LLM calls. `standard` adds no LLM call either; it only
    widens the pool (R-13). `deep` adds the R-04 rewrite and the R-22 sufficiency round.
  - The combination is compatible.
- **Verdict:** ok-combined.
- **What will happen:**
  - Behaviour is unchanged for existing callers.
  - Agents can ask for `deep` on hard questions, and cost is paid only on request.
  - Quality: 0 at the default, positive only where deep pays, which is unproven (see R-22).
  - Latency and cost: 0 at the default (estimate).
  - Risk: low. The call caps per tier (BD-1) keep the variance bounded.
  - Touches working, episodic and semantic memory; operator R.
- **Where:** v1 `config/schema.py:1154-1222` (`agentic`, `agentic_mode`, `fact_chain`,
  `event_ledger`) and `engine.py:6576` (`read`) -> v2 `read.tiers` (`TierPolicy`, resolved once per
  request).
- **Depends on / affects:** Feeds R-04, R-13, R-22, R-23 and R-25. Consistent with G-16 D (scoped
  handle) and P-03 D (`on_missing_model`: a `deep` request with no model raises an error, or runs as
  `standard` when set to `rules`).
- **Migration:** v1 enhancer switches map onto tier contents. A v1 config with `agentic: true` maps
  to `default_depth: deep`.
- **Test or gate:** A unit test checks that each tier stays within its call and token caps. A
  default-tier run must reproduce clean-jina-full (81.4) within the noise band.

### R-02 Default unit of evidence returned: C, mixed bundle

- **Decision.** Raw windows hold most of the budget. Derived content enters only as labelled notes
  under per-block caps, each pointing to its source.
- **Verdict:** ok. The scope is narrowed by M-02 and O-15.
- **What will happen:**
  - With M-02 (raw + facts by default) and O-15 A (no observation layer), the notes block holds:
    - facts;
    - O-14 deterministic value-history lines;
    - M-20 profile card entries (relevance-gated).
  - No summaries or observation notes appear unless those optional layers are enabled.
  - Quality: +small on aggregation and change questions (estimate).
  - Tokens: up to the note caps, within the R-17 gate of +30%.
  - Touches episodic, semantic and reflective memory; operator C.
- **Where:** v1 `config/templates/base.yaml:19` (`default_mode: replay`), `config/schema.py:427`
  and `engine.py:6037` (`_assemble_core`) -> v2 `compose.bundle` (`EvidenceBundle` with blocks:
  `raw`, `notes`, `profile`, `public`).
- **Depends on / affects:** R-17, R-18, R-20, M-02, M-20, O-14, O-15.
- **Migration:** v1 `replay` maps to a bundle whose notes block is empty, which is identical to
  today.
- **Test or gate:** A notes-on vs notes-off paired screen on the 437-question focus slice at equal
  budget, then a G-01 blind run.

### R-03 Typed query contract adoption: B, heuristic contract on by default

- **Decision.** The rules fill a contract with these fields: subjects, relation, answer type, time
  scope, cardinality and request kind. Building it makes zero calls. The contract feeds the header,
  the rerank hint and the router. The LLM fallback (option C) stays as a multilingual opt-in.
- **Verdict:** needs-measurement.
  - The focus slice gave net +9 (single +6/-2, multi +2/-3, temporal +2/-1, open +5/-0) at no extra
    cost.
  - The full 1,540-question run and the blind sets have not run.
  - Multi-hop was -1 net. That is inside its band, but it is already the weakest category (66.7).
- **What will happen:**
  - Quality: about +0.5 to +1 point on LoCoMo (estimate scaled from +9 of 437).
  - Latency and cost: 0, as measured.
  - Risk: the rules are English-shaped. Non-English text falls back to an "unknown" contract and
    today's behaviour.
  - Touches working memory; operator R.
- **Where:** v1 `core/query_contract.py:86` and `:316-443`, `config/schema.py:970`, and in
  `engine.py`: `:5718` (logged only), `:3244` and `:3306` (header), `:14016-14036` (rules and LLM)
  -> v2 `read.contract` (`QueryContract.build(query, lang)`).
- **Depends on / affects:** R-04, R-05, R-07, R-08, R-20 and R-24 all consume the contract. If the
  contract fails its gate, R-04, R-05 and R-24 fall back to fixed legs.
- **Migration:** Turn on `query_contract: heuristic`. The contract fields are added to the read
  trace.
- **Test or gate:**
  - contract-jina-full (1,540 questions) must beat clean-jina-full 81.4 by more than the band, with
    no category dropping beyond its own band.
  - Then one frozen run on MAB-CR and ConvoMem blind (G-01 C).

### R-04 Routing and query rewriting: D, rule router, rewrites in the deep tier only

- **Decision.** In `fast` and `standard`, a zero-call router maps the contract to legs and weights:
  - a time scope sends the question to the temporal window;
  - many-cardinality sets list mode;
  - a named subject turns on the entity leg.

  In `deep` only, an LLM writes 2-3 rewrites in the wording of stored records. The rewrites are
  fused, and R-22 reuses them.
- **Verdict:** ok.
- **What will happen:**
  - The base template no longer bypasses the planner: v1 routes only under `mode=auto`.
  - Quality: positive where routes are screened. Latency: 0 in the hot path. Deep tier: about +1
    call.
  - Risk: a misroute is silent, so every route logs its reason.
  - Operator R.
- **Where:** v1 `config/schema.py:503` (`planner`), `engine.py:13976` (`_plan_read_mode`),
  `engine.py:2339` (`_probe_legs`) and `engine.py:6064-6171` (probes and subqueries) ->
  v2 `read.router` (`Router.plan(contract, tier) -> LegPlan`).
- **Depends on / affects:** R-01, R-03, R-05, R-22.
- **Migration:** Rule probes (pseudo-relevance feedback, multi-intent, bridge, cluster) become
  router actions. Unscreened probes go to the G-22 retirement list.
- **Test or gate:** Each route is screened on the focus slice, and its fired rate is reported
  (G-01).

### R-05 Default retrieval leg set: D, core three plus router-activated legs

- **Decision.** Vector, BM25 and temporal always run. Entity, graph, episode and session legs run
  only when the router switches them on. The perspective leg, on by default in v1, leaves the
  default set because it replays the vector order (I75).
- **Verdict:** ok. Each routed leg needs its own screen.
- **What will happen:**
  - Fewer correlated voters in the default fusion.
  - Quality: 0 to +small (estimate).
  - Latency: flat at the default, and bounded by R-25 when legs are routed.
  - Touches episodic, semantic and associative memory; operator R.
- **Where:** v1 `engine.py:2397-2504` (leg build; entity and view tags at `:2417-2486`),
  `engine.py:5310` (`_search`) and `config/schema.py:919` (`perspective_leg=True`) ->
  v2 `read.legs` (registry of `Leg`, each tagged with a family).
- **Depends on / affects:** R-04, R-06 to R-11, R-25. The graph and episode legs exist only when
  those optional layers do (M-02).
- **Migration:** Set `perspective_leg` to False in v2. Built-but-off legs (maxsim, bridge, cluster,
  person-time and others) are registered as opt-in or retired under G-22.
- **Test or gate:** Dropping the perspective leg must stay within the band of clean-jina-full. Each
  routed leg needs a focus run plus a blind run.

### R-06 Lexical leg, BM25 vs learned sparse: D, BM25 with language-aware tokenisers

- **Decision.** Okapi BM25 stays as an independent leg. The tokeniser is pluggable per namespace
  language (CJK bigrams, stemming), and literal codes are kept.
- **Verdict:** ok.
- **What will happen:**
  - No change for English.
  - Non-Latin text gains a working lexical leg (today it gets almost nothing).
  - Latency and cost: 0. Complexity: one tokeniser port.
  - Consistent with M-23 B (multilingual embedder).
  - Operator R.
- **Where:** v1 `engine.py:5196` (`_lexical_leg`), `engine.py:15020-15027` (lexical projector
  variant) and `config/schema.py:860` (`word_vector_leg`) -> v2 `read.legs.lexical`
  (`Tokeniser` port per namespace).
- **Depends on / affects:** R-05, M-23, P-10.
- **Migration:** Existing indexes keep the default tokeniser. Changing the language triggers a
  lexical index rebuild from the log.
- **Test or gate:** An English focus run must be byte-identical. A small non-English behavioural
  probe (G-04) must show lexical recall above zero.

### R-07 Temporal leg and relative-time anchoring: D, anchored window plus event-time index

- **Decision.** Relative phrases resolve against the question's reference time: the caller's value,
  defaulting to now, in the source time zone (W-04 B+C). Candidates inside the window are ranked by
  similarity. Once M-10 B gives event intervals with precision, the leg matches event intervals by
  overlap. Until then it falls back to option B, utterance time.
- **Verdict:** ok.
- **What will happen:**
  - Questions like "what did Bram do last week" stop resolving against the read clock on
    backfilled stores.
  - Quality: positive on temporal questions (today 83.2), estimate +small. The earlier B4 screen
    showed no gain on one dataset, so a gain is not assured.
  - Latency and cost: 0.
  - Touches episodic and prospective memory; operator R.
- **Where:** v1 `core/temporal_query.py:106` (`query_interval`) and `:181` (`temporal_leg`),
  `config/schema.py:363-385`, `engine.py:2430-2462` (`temporal_relative` / `as_of`) and
  `config/templates/base.yaml:21-23` -> v2 `read.legs.temporal` (`TimeWindow` with precision).
- **Depends on / affects:** R-03 (time scope), M-10, W-04, R-24.
- **Migration:** `temporal_relative` becomes the default, anchored to `reference_time`. The
  event-interval index is built from the M-10 fields during migration.
- **Test or gate:** The temporal category must not drop beyond its band on full LoCoMo, and the
  same holds on the blind sets. A metamorphic test shifts the read clock and the answer must not
  change.

### R-08 Entity leg and hub damping: B, inverse-degree penalty

- **Decision.** The Mem0 curve sets the boost: sim x 0.5 x 1/(1 + 0.001(n-1)^2). The boost is
  capped and takes the max over the query's entities. The leg runs only when the contract names a
  subject.
- **Verdict:** ok.
- **What will happen:**
  - Names reach the pool without hub flooding. Ilse, who appears in most records, is damped.
  - Quality: +small on named multi-hop questions (estimate).
  - Latency: one indexed lookup.
  - Touches associative and semantic memory; operator R.
- **Where:** v1 `core/temporal_query.py:745` (`entity_leg`), `config/schema.py:1033` and
  `engine.py:2481-2486` -> v2 `read.legs.entity`.
- **Depends on / affects:** R-05, M-15, W-19. The curve constant is calibrated per store size
  (RV17).
- **Migration:** No data change. The leg reads the existing entity links.
- **Test or gate:** A router-activated focus run plus a blind run. Report its fired rate.

### R-09 Graph traversal method at read: C, one-hop convergent expansion

- **Decision.** The leg expands one hop from up to 20 semantic seeds. Each candidate's score sums
  its shared-entity, semantic and causal link weights. BFS depth 2 and PPR stay as advanced arms.
- **Verdict:** conflict (M-02).
  - M-02's chosen option D says "drop summaries, graph and observations", while the owner's note
    "B + D" also keeps the explicit layer field.
  - Closest compatible reading: the graph is an optional projection, off by default. It is compiled
    from assertions (M-16 C) into the M-18 A store. The graph leg is router-activated only when that
    projection is enabled.
  - Needs owner confirmation.
- **What will happen:**
  - Default reads are unchanged.
  - With the projection on: multi-hop recall gains (estimate), with bounded latency through a
    timeout (R-25).
  - Touches associative memory; operator R.
- **Where:** v1 `engine.py:2568-2675` (`_graph_seeds`, `_graph_walk`, `_graph_leg`),
  `engine.py:3681` (`_graph_rerank`, PPR) and `config/schema.py:699-719` -> v2
  `read.legs.graph` (`OneHopConvergent`).
- **Depends on / affects:** M-02, M-16, M-18, G-W49 (freshness), R-05.
- **Migration:** BFS stays as the arm `graph_traversal: bfs2`. The default arm is `onehop`.
- **Test or gate:** The G-C3 graph arm runs on the focus slice and a blind set, and must pass G-01
  before the graph template turns it on.

### R-10 Episode and view-tag legs: B, episode leg lifted to parent turns

- **Decision.** Episode and view-tag hits are retrieval keys only. Each hit maps to its source
  turns, and the turns enter the pool.
- **Verdict:** conflict (M-02).
  - M-02 D drops derived layers beyond facts, yet M-14 B and O-09 B keep episodes and stitched
    narratives.
  - Closest compatible reading: episodes are an optional layer under the M-02 layer field, and the
    episode leg runs only when that layer exists.
  - Needs owner confirmation.
- **What will happen:**
  - Recall gains without unverified text in context, because the episode text itself is never
    shown.
  - Quality: estimate +small on multi-hop and open-domain.
  - Latency: one extra vector search.
  - Touches episodic and reflective memory; operator R.
- **Where:** v1 `config/schema.py:875` (`session_leg`), `:1003` (`view_tag_leg`), `:1014`
  (`facts_to_sources`), `core/temporal_query.py:854` and `engine.py:6210` -> v2
  `read.legs.episode` plus `read.lift`.
- **Depends on / affects:** R-17, M-02, M-14, O-09.
- **Migration:** v1 fragments that competed as raw text are re-tagged with layer=episode and leave
  the raw pool.
- **Test or gate:** Must beat facts-only on the focus slice or add value on a blind slice (G-W1),
  with tokens within +30%.

### R-11 Fusion method across legs: C, RRF with leg de-correlation (note "A + C")

- **Decision.** The combined design keeps the RRF formula:
  - First, legs in the same source family (dense, lexical, structured, derived) are merged, taking
    the max rank per record.
  - Then RRF k=60 with equal weights fuses across families. That is A, applied to family lists.
  - When only one leg per family runs, which is the default core three, the result is identical to
    plain RRF.
  - `fusion: rrf` stays available as a mode.
  - The combination is compatible.
- **Verdict:** ok-combined.
- **What will happen:**
  - Correlated legs stop double-voting (I75).
  - Quality: 0 at the default, positive once routed legs are added (estimate).
  - Latency and cost: 0.
  - Operator R.
- **Where:** v1 `services/lexical/base.py:80` (`rrf_fuse`), `:119` (`minmax_fuse`),
  `engine.py:5473-5555`, `config/constants.py:12` (`RRF_K`), `config/schema.py:833-842`, and the
  family map at `engine.py:877-878` -> v2 `read.fusion` (`FamilyRRF`).
- **Depends on / affects:** R-12 shares the same family map. R-05.
- **Migration:** `leg_weights` and `leg_weights_by_shape` are deprecated (G-22) unless screened.
- **Test or gate:** With the core three, output must be byte-identical to plain RRF. A routed-leg
  run must show no loss against plain RRF.

### R-12 Protected candidate pool policy: C, per source family

- **Decision.** The top hits of each source family always enter the pool, with de-duplicated votes.
- **Verdict:** needs-measurement.
  - The measured baseline is B: per_leg N=3 gave +15 net, and clean-jina-full itself runs
    `scr-pool-protect-jina`.
  - C (`source_family`) is built but unscreened.
- **What will happen:**
  - The pool stays bounded as legs are added.
  - Quality: expected about equal to B (estimate). Rerank cost below B's +30%.
  - Operator R.
- **Where:** v1 `config/schema.py:447` and `:453`, and `engine.py:5386`, `:5500-5509`, `:5634` ->
  v2 `read.pool` (`FamilyPool`).
- **Depends on / affects:** R-11, R-13, R-14.
- **Migration:** The v2 default is `pool_protect_mode: source_family`. If C fails its gate, ship B
  (per_leg 3).
- **Test or gate:** C vs B paired on the focus slice. C must be non-inferior within the band, with
  rerank pairs at or below B's. Then run the full 1,540 questions.

### R-13 Leg floors and pool widening: C, pool by read tier

- **Decision.** The tier sets the pool per leg, for example fast about 20, standard about 40 and
  deep about 80 (numbers to be calibrated). Floors are calibrated per store and per embedding model
  instead of raw constants. Regrowing the pool inside the gate stays as it is.
- **Verdict:** ok.
- **What will happen:**
  - Callers get a predictable cost knob.
  - Jina runs at 196 pairs/s, so 40 pairs cost about 0.2 s on GPU (estimate).
  - Large stores keep recall at `standard`.
  - Operator R.
- **Where:** v1 `config/schema.py:566` (`candidate_pool`), `:1024` (`leg_min_scores`) and `:1253`
  (`relevance_gate_regrow`); `core/relevance_probes.py:80` (`calibrate`) -> v2 `read.pool`
  (`PoolPolicy(tier, n_records)`).
- **Depends on / affects:** R-01, R-12, R-14, R-25.
- **Migration:** v1 `candidate_pool` maps to the fast-tier value. Floors are recalibrated at first
  sleep.
- **Test or gate:** A per-tier focus run: `standard` must be at least `fast`, and latency must stay
  within the R-25 SLO.

### R-14 Default reranker model and placement: B, Jina v3.5 default

- **Decision.**
  - jina-reranker-v3.5 is the reranker under the standing owner decision.
  - The Qwen3-Reranker-4B "quality tier" in option B is dropped, since no further reranker testing
    is planned. The 4B model remains only as a historical reference for older results.
- **Verdict:** conflict (P-18).
  - P-18 chose C, a gated reranker that runs only when the score spread is flat or the pool exceeds
    the budget, with off as the default. That contradicts "on by default".
  - Proposal: Jina runs on every `standard` and `deep` read, and on every read wherever the model
    is installed. This matches the measured 81.4 baseline. P-18's gate becomes the mode
    `rerank: gated` for CPU-only profiles (P-17 C hardware profile).
  - Needs owner confirmation.
  - Needs research: the jina v3.5 licence terms for commercial use.
- **What will happen:**
  - Reader tokens fall by about half: 1,571 to 672 per question was measured (I84).
  - Latency: +0.1 to 0.3 s on GPU (estimate).
  - The baseline quality is the measured 81.4.
  - Operator R.
- **Where:** v1 `config/schema.py:333-336` (`rerank: off`, `rerank_model`), `engine.py:11676`
  (`_rerank_provider`) and `engine.py:5703-5750` -> v2 `read.rerank` (`RerankStage(model, mode)`).
- **Depends on / affects:** R-12, R-13, R-15, R-16, R-21, R-25, P-18, P-19.
- **Migration:** The v2 default is `rerank: jina-v3.5`. v1 4B configs keep working as a named
  override.
- **Test or gate:** The default config must reproduce clean-jina-full within the band. A licence
  check comes before release.

### R-15 What the reranker scores: A, hit only, windows after (owner chose differently)

- **Decision.**
  - Jina scores the single hit, and neighbour windows (R-19) are built after reranking.
  - G-W28 is not taken: the gap plan marks it "declined, not built".
  - `rerank_context` (option B) stays an opt-in flag, unscreened, and is listed for G-22
    retirement.
  - Chunk-and-max (option D) stays opt-in for long document records, where it complements A.
- **Verdict:** ok. Nothing must be revisited.
- **What will happen:**
  - Behaviour is unchanged from today.
  - Rerank cost is lower than C because a pair holds one turn, not a window.
  - Known weakness: context-dependent short turns ("yes, that one") score low and may not surface.
    This bears most on multi-hop, which at 66.7 is the weakest category. The R-10 episode lift
    partly compensates.
  - Quality: 0 against today, and forgoes the estimated small gain of C.
  - Touches episodic memory; operator R.
- **Where:** v1 `config/schema.py:440` (`rerank_context` 0..3), `:469` (`rerank_chunk_chars`) and
  `:429` (`rerank_date_prefix`); `engine.py:5744-5750` -> v2 `read.rerank` (unit = hit).
- **Depends on / affects:** These recommendations had assumed C:
  - **R-17.** "One reranker score meaningful across layers" still holds: lifted parent turns are
    scored as hits.
  - **R-18.** Packing ranks a window by its hit's score, and window tokens are counted after
    reranking. That is still consistent: the shown unit is no longer the scored unit, but the
    packer counts real tokens.
  - **R-19.** Its rationale ("pairs with window reranking") weakens. Token windows are still the
    right shape and are built after reranking; overlapping windows merge and take the higher hit
    score.
  - **R-13.** The pool counts hits, which is cheaper.
  - **P-18.** Lists G-W28 among its gaps; its gate does not depend on the unit.

  None of these needs revisiting.
- **Migration:** None. This is v1 behaviour.
- **Test or gate:**
  - A regression test checks that `rerank_context=0` is the default.
  - Optional later: if multi-hop stays the weakest category, a G-W28 window-unit arm can be
    screened as research without changing the decision.

### R-16 Recency, importance and reinforcement signals: B, bounded multiplicative boosts

- **Decision.**
  - After reranking, recency and the reinforcement count multiply the score within a band. Example
    band: 0.92-1.10, fixed and generic.
  - The feature is built but off until screened.
  - The reinforcement count comes from O-13 B consolidation. O-25 A means there is no implicit
    feedback signal.
- **Verdict:** ok.
- **What will happen:**
  - The boost cannot override relevance.
  - Freshness helps assistants with live preferences.
  - Quality: 0 to +small (estimate). Latency and cost: 0.
  - Touches semantic and reflective memory; operator R.
- **Where:** v1 `config/templates/base.yaml:24-27` (weights 0.0), `core/policies/scoring.py`,
  `config/schema.py:1494` (`trust_weighted_ranking`) and `:1040` (`recency_leg`) -> v2
  `read.rerank.boosts` (`BoundedBoost(lo, hi)`).
- **Depends on / affects:** R-14, O-13, O-25, ADR-030.
- **Migration:** The additive weights are removed (ADR-030 stands) and `recency_leg` is retired.
- **Test or gate:** Screen on PrefEval and OP-Bench (preferences) and LoCoMo (no loss).

### R-17 Evidence lifting and one score: B, lift to parent turns, one reranked list

- **Decision.** Every derived hit (fact, and optionally episode or edge) maps to its parent turns
  through lineage. The parent turns rank under one Jina score. Derived text appears only as capped
  notes (R-02, R-18).
- **Verdict:** ok.
- **What will happen:**
  - The context stays grounded.
  - With O-15 A there is no observation dedup step (no "prefer observations"), and lifting covers
    facts by default.
  - Quality: estimate +small. Tokens within +30%.
  - Operator R.
- **Where:** v1 `config/schema.py:1014` (`facts_to_sources`) and `engine.py:6210-6213` -> v2
  `read.lift` (`lift(hits) -> parent turn ids`, using M-09 lineage).
- **Depends on / affects:** R-02, R-10, R-12, R-15, M-09, G-08.
- **Migration:** `facts_to_sources` is on by default. Derived rows without lineage are excluded from
  lifting.
- **Test or gate:** Focus slice with tokens within +30% (G-W8), then a blind run.

### R-18 Context budget and block shares: D, dynamic shares with redistribution

- **Decision.**
  - **Step 1, adopt now.** This is B, a correctness fix (RV08). The packer skips an oversize item
    and continues instead of breaking. An oversize bundle is omitted or replaced by a validated smaller complete bundle; it is never truncated mid-evidence (ER-24).
    It counts headers and provenance, and validates the final token count with the reader's
    tokenizer.
  - **Step 2.** Per-block caps (raw, notes, profile, public), with unused capacity flowing to the
    next evidence in global rank.
- **Verdict:** needs-measurement for the shares in step 2. Step 1 is a fix and needs no screen.
- **What will happen:**
  - A long item no longer evicts shorter useful ones.
  - In exact mode the rendered context, headers, citations and reserves never exceed the reader-tokenizer budget; estimated mode is labelled and makes no hard guarantee (ER-24).
  - Quality: +small from the fix (estimate). Tokens: equal or lower.
  - Touches working memory; operator C.
- **Where:** v1 `core/policies/assembly.py:179` (`assemble`) and `:288-309` (the early `break`),
  `config/constants.py:210-212` (`ASSEMBLE_TOP_K` 16, `ASSEMBLE_BUDGET_TOKENS` 2,048) and
  `engine.py:5910` (`_assemble`) -> v2 `compose.packer` (`BudgetPacker(blocks, tokenizer)`).
- **Depends on / affects:** R-02, R-17, R-19, R-20, R-23.
- **Migration:** The default budget is unchanged. The block caps start equal to "raw takes all" when
  the notes block is empty.
- **Test or gate:**
  - A unit test checks that output never exceeds the budget, using the reader tokenizer; multilingual and long-token probes; estimated mode asserted as labelled (ER-24).
  - Shares vs no shares, paired at equal budget on the focus slice (RV08).

### R-19 Neighbour window expansion shape: C, token-sized, budget-scaled windows

- **Decision.**
  - Windows are measured in tokens, with a 1:2 before:after allowance per hit.
  - Windows shrink as the budget fills, and overlapping windows merge.
  - Windows are built after reranking (R-15 A).
  - The forward bias in turns stays a chat preset.
- **Verdict:** ok.
- **What will happen:**
  - The same shape works for long document chunks and short chat turns.
  - Quality: 0 to +small (estimate). Tokens: more predictable.
  - Touches episodic memory; operator C.
- **Where:** v1 `config/schema.py:574-594` (`replay_window_before/after`, `replay_window_unit`,
  `replay_budget_scaling`, `replay_hits_first`), `engine.py:9319` (`_expand_neighbours`) and
  `:9428` -> v2 `compose.windows` (`TokenWindow`).
- **Depends on / affects:** R-15, R-18, D1.
- **Migration:** `replay_window_unit: tokens` and `replay_budget_scaling: true` become defaults. The
  turn windows stay as a preset.
- **Test or gate:** A focus run at equal budget against turn windows must be non-inferior, then a
  document-shaped behavioural probe.

### R-20 Rendering format, order and dates: D, typed sections, contract header, integer aliases

- **Decision.**
  - The engine renders labelled sections: raw evidence, notes, profile and public knowledge.
  - Each line carries:
    - an alias such as `[3]`, strict per P-09 B;
    - the speaker;
    - an absolute date with weekday and precision;
    - the modality.
  - A one-line contract header comes first.
  - Low-trust lines use the G-09 D wrapper.
- **Verdict:** ok.
- **What will happen:**
  - Every integrator gets the same dates and provenance.
  - The header carries A03's measured gain.
  - Tokens: about +7% for date prefixes (D1, measured).
  - Risk: lower, because the reader can tell inference from observation.
  - Operator C.
- **Where:** v1 `config/schema.py:625` (`render: plain`) and `:1087` (`present_order`),
  `core/policies/assembly.py:80-85` (stable prefix) and `engine.py:3244-3306` (contract header) ->
  v2 `compose.render` (`Renderer(sections, alias_map)`).
- **Depends on / affects:** R-03, R-18, R-23, P-09, G-09.
- **Migration:** The harness date adapter is retired. Tracking for the aliases lives in code.
- **Test or gate:** A behavioural test checks zero invented aliases (G-W36). A focus run must be
  non-inferior to dated lines.

### R-21 Relevance gate and abstention: C, store-calibrated gate with named bypass

- **Decision.**
  - About 20 off-topic probes per store set the bar: top score > p95 + 1.0 sd.
  - Named references bypass the gate.
  - When the gate fires, the read returns an empty context with a reason.
  - The bar is calibrated on the Jina score in tiers that rerank, and on the raw dense score
    otherwise.
- **Verdict:** needs-measurement. The gate is built but has never been screened, and it may
  suppress multi-hop evidence.
- **What will happen:**
  - Irrelevant memory stops leaking into unrelated requests. OP-Bench-type sets gain (the decider
    form gave +22).
  - Risk: recall loss on all-on-topic sets.
  - Latency: 0 per read (probes at sleep).
  - Touches working and semantic memory; operator R.
- **Where:** v1 `config/schema.py:1231`, `engine.py:8038` (`_relevance_gate`) and `:14144`
  (`calibrate`), `core/relevance_probes.py:80`, `config/constants.py:125` (`THETA_ABSTAIN`) and
  `engine.py:6230-6285` (`abstain_on_raw`) -> v2 `read.gate` (`StoreCalibratedGate`).
- **Depends on / affects:** R-13, R-14, M-20 (the profile is also relevance-gated).
- **Migration:** The probes are computed at first sleep per namespace. `theta_abstain` is retired.
- **Test or gate:** OP-Bench gain, and no recall loss beyond the band on MAB-CR, ConvoMem and
  LoCoMo. The fired rate is reported per slice.

### R-22 Agentic multi-step read design: B, sufficiency check plus one rewrite round (owner chose differently)

- **Decision.** Only in the `deep` tier (R-01):
  - One judge call decides whether the composed context is sufficient.
  - If it is not, one rewritten search runs (reusing the R-04 rewrite) and is fused into the pool.
    Then the reader answers.
  - The cap is 2 extra calls and one round.
  - Judge thinking is allowed under a token cap (P-15 B).
  - With no model, `on_missing_model` applies (P-03 D).
  - The slot loop (A, f-slot net -3) and the read-time fact chain (f-chain net -1) are retired
    under G-22.
  - Host agents can still loop through the public tools (G-16, G-17), but that is not an engine
    loop.
- **Verdict:** needs-measurement.
  - B is unmeasured. Its closest relative, E02, fired on 150 of 437 with net -3 at about 8x wall
    time (3 h 25 min vs 25 min).
  - B is cheaper by design (1 judge plus 1 search, against up to 2 typed steps), but the gain is
    unproven.
- **What will happen:**
  - Default reads are unaffected.
  - Deep reads: about 2-3x the latency of `standard` (estimate), with a ceiling on cost.
  - Quality: 0 to +small where it fires (estimate). RV12 calls the deep-tier evidence inconclusive.
  - Touches working and episodic memory; operators R and C.
- **Where:** v1 `config/schema.py:1154-1222`, `engine.py:6762-7228` (`_agentic_fires`,
  `_agentic_extend`, `_agentic_slot_loop`, `_slot_expand`), `core/slot_loop.py` and
  `core/fact_chain.py:239` -> v2 `read.deep` (`SufficiencyRound(judge, rewriter, cap=1)`).
- **Depends on / affects:** These recommendations had assumed D (no engine loop):
  - **R-01.** `deep` now contains an engine round. Compatible.
  - **R-04.** The rewrite is reused. Compatible.
  - **R-25.** Needs a deep-tier SLO.
  - **P-15.** Judge role thinking cap.
  - **P-21.** Latency budgets.
  - **G-01.** Gate.

  None must be revisited. R-25 gains a `deep` target.
- **Migration:** `agentic_mode: query` maps to B. `slot` and `fact_chain` become deprecated
  research arms.
- **Test or gate:** A new screen `f-suff` on the 437-question focus slice, paired against
  f-jref2-i2 (or the contract arm if R-03 is adopted first).
  - Net must exceed the band (about +7).
  - No category may drop beyond its band.
  - Report the fired rate.
  - Wall time must be at most 2x the reference.
  - Then the G-01 blind run.
  - If it fails, the `deep` tier ships with the rewrite only (R-04 C), and the owner is asked
    whether to fall back to D.

### R-23 External public knowledge at read: B, opt-in, gated, separate block

- **Decision.**
  - External knowledge is used only for invited-inference questions.
  - The query is privacy-filtered, results are cached, and the call budget is 20.
  - Results go in a separate `[public knowledge]` section with a clause that it never proves
    private facts.
  - It applies only in tiers the config allows.
- **Verdict:** ok.
- **What will happen:**
  - Recommendation-style questions can draw on world knowledge.
  - Latency and cost: + on the small share of reads where it fires.
  - Risk: query leakage, mitigated by the privacy filter and the G-09 wrapper.
  - Touches resource memory; operator R.
- **Where:** v1 `config/schema.py:1300` and `engine.py:3147-3178` (`_external_broker`,
  `_external_overlay`) -> v2 `read.external` (block `public` in `compose.bundle`).
- **Depends on / affects:** R-01, R-18, R-20, G-09, G-10.
- **Migration:** No change; the feature stays off by default.
- **Test or gate:** The E03 screen, with external results kept separate in benchmark modes (RV13).
  A privacy-filter unit test.

### R-24 As-of reads and default time view: C, contract-driven time view

- **Decision.** The contract's time scope selects one of three views:
  - **current** (default): valid now, not retracted;
  - **history:** "used to", "before", "changed";
  - **as-of:** a date in the question maps to `as_of_valid`.

  `as_of_known` is only ever set by the caller (M-11 B, two axes). When the scope is unclear, the
  current value is shown with a compact value-history line from the O-14 deterministic view.
- **Verdict:** ok.
- **What will happen:**
  - Questions about change see the transitions, including A to B to A states (RV05).
  - Quality: estimate +small on change questions. Tokens: + one line per keyed value.
  - Risk: a wrong contract scope, mitigated by the fallback line.
  - Touches semantic and episodic memory; operator R.
- **Where:** v1 `core/read_filters.py:245-260`, `engine.py:1097` (`_current_at`) and
  `engine.py:6595-6602` -> v2 `read.timeview` (`TimeView.select(contract, request)`).
- **Depends on / affects:** R-03, R-20, M-10, M-11, M-13, O-14.
- **Migration:** The v1 `as_of` maps to `as_of_valid`.
- **Test or gate:**
  - A behavioural suite for supersession (G-04).
  - Temporal and change categories must not drop beyond the band.
  - A correction-latency guard (G-05).

### R-25 Read latency targets and parallel legs: C, parallel legs plus tier SLOs

- **Decision.**
  - Legs run concurrently, in a deterministic merge order, each with a timeout. A leg that times
    out is dropped and logged.
  - Each tier publishes a p95 target, measured in CI on pinned hardware:
    - `fast` < 150 ms without reranking, or about < 350 ms with Jina if R-14 runs it in `fast`;
    - `standard` < 500 ms with Jina on GPU;
    - `deep` < 3 s.

    All numbers are to be confirmed.
- **Verdict:** ok.
- **What will happen:**
  - Latency stays near the slowest leg instead of the sum of all legs.
  - Today's reference is p50 764 ms per question in clean-jina-full (the timer's scope still needs
    confirming).
  - Risk: thread safety of the stores. Inline execution in the library (G-19 A) is fine with a
    thread or task pool.
  - Touches working memory; operator R.
- **Where:** v1 `engine.py:5310` (sequential legs in `_search`), reranker batches of 8 in a worker
  thread, ADR-015 (graph p95) and `GAP_TO_SOLUTION_PLAN.md:217` -> v2 `read.exec`
  (`LegExecutor(gather, timeouts)`).
- **Depends on / affects:** R-01, R-05, R-13, R-14, R-22, P-21, G-19, G-20.
- **Migration:** None in data. The results must be byte-identical to sequential execution.
- **Test or gate:** A determinism test (parallel vs sequential give the same output). An SLO check
  in CI with p50 and p95 per tier (RV10).

### R-26 Read-path caching and invalidation: D, input caches plus provider prompt-cache layout

- **Decision.**
  - Keep the query-vector, snapshot and vocabulary caches.
  - Keep stable content first and variable content last in every prompt, with cache hints when a
    hosted reader is bound (P-08 C).
  - The rerank-score cache (B) and the composed-context cache (C) are deferred until the cache key
    includes effective scope, viewer/purpose, time view, policy/model versions, record versions and
    invalidation generations; admission rechecked at use (ER-31 B). Until then no result-content cache
    is enabled (disabled until ER-31 keys exist).
- **Verdict:** ok.
- **What will happen:**
  - Quality: 0. Latency: small savings on hosted readers. Local prefill is about 10% (G-W45).
  - No post-erasure leak risk is added.
  - Touches working memory; operator C.
- **Where:** v1 `engine.py:739-776` and `:1268-1300` (caches), `config/schema.py:1368-1369` (cache
  backend) and `core/policies/assembly.py:80-85` (stable prefix) -> v2 `read.cache` and the
  `compose.render` layout.
- **Depends on / affects:** R-25, P-08, M-25, G-12, G-13.
- **Migration:** The caches are cleared on upgrade, since the key format changes.
- **Test or gate:**
  - Prefix stability: the same bytes before the volatile block across reads.
  - Erasure test: a forgotten record never appears from any cache after restart (G-13).

## Conflicts and open points

| id | issue | proposal | needs |
|---|---|---|---|
| R-14 / P-18 | R-14 B runs Jina on by default. P-18 C runs a reranker only when the spread is flat or the pool exceeds the budget, with off as the default. | Jina runs on every read wherever the model is installed, which matches the 81.4 baseline. P-18's gate becomes `rerank: gated` for CPU-only profiles. | Owner confirmation. Should Jina run on every read, or only when the P-18 gate fires? |
| R-09 / M-02 | M-02's option D drops the graph layer, while R-09 builds a graph leg and M-16, M-18 and M-19 keep the graph. | Read "B + D" as: a layer field on every record, raw + facts on by default, and graph, episodes, summaries and communities as optional layers that are off by default. The graph leg is router-activated only when its layer exists. | Owner confirmation of the M-02 reading. |
| R-10 / M-02 | M-02's option D drops derived layers beyond facts, while R-10, M-14 and O-09 keep episodes. | Same reading: the episode leg exists only when the episode layer is on. | Owner confirmation (same question as R-09). |
| R-02, R-17 / O-15 | There is no observation layer, so "prefer observations" dedup and observation notes do not exist. | The notes block holds facts, O-14 value-history lines and profile card entries. G-W32 is not taken on the read side. | None (recorded). |
| R-15 | G-W28 is not taken. The weakest category is multi-hop at 66.7, where window context would help most. | Keep A. Leave `rerank_context` opt-in pending G-22 retirement, and screen a window-unit arm only as research. | None. |
| R-22 | B is unmeasured. The nearest loop (E02) was net -3 at about 8x time. | Run the `f-suff` screen before building the default deep tier. If it fails, the deep tier ships as rewrite only. | Measurement (see below). If it fails, ask the owner about falling back to D. |
| R-14 | The licence of jina-reranker-v3.5 for commercial use is unchecked. | Check it before the v2 release. If it is restrictive, keep Jina as a preset and flag the default. | Research. |
| R-03 | Adoption hinges on contract-jina-full and the blind sets. | Build behind the flag. Make it the default only after the gate. | Measurement. |

## Measurements to run before building

| what | which screen or dataset | gate |
|---|---|---|
| R-03 typed contract as default | contract-jina-full (1,540 LoCoMo cat 1-4) vs clean-jina-full 81.4; then MAB-CR and ConvoMem blind | Macro above the band. No category beyond its own band, multi-hop included. Frozen blind run (G-01 C). |
| R-12 family pool vs per-leg | Focus slice of 437 questions, paired C vs B (per_leg 3, the clean-jina-full arm) | C non-inferior within ±7. Rerank pairs ≤ B. |
| R-18 dynamic block shares | Focus slice at equal budget, shares vs no shares (step 1 fix in both arms) | Net ≥ 0 within the band. Final prompt ≤ budget by reader tokenizer. |
| R-21 store-calibrated gate | OP-Bench (gain), MAB-CR, ConvoMem, LoCoMo (no loss) | No recall loss beyond the band on all-on-topic sets. Fired rate reported per slice. |
| R-22 sufficiency round (`f-suff`) | 437-question focus slice vs f-jref2-i2 (or the contract arm); then blind | Net > band (about +7). No category loss beyond the band. Wall time ≤ 2x the reference. Fired rate reported. |
| R-05, R-08, R-09, R-10 routed legs | Focus slice per leg (router-activated), then one blind set each | G-01 C per leg. Tokens within +30% (R-10/R-17). |
| R-25 tier SLOs | CI latency job on pinned hardware | p95 per tier within target. Parallel output byte-identical to sequential. |


## Owner follow-up (2026-10-12)

- **Q1 Layer field, two layers on** (as proposed): Every record has an explicit `layer` field; raw (L0) and facts (L1) are populated by default; episodes, summaries, graph, profile, observations and communities are opt-in layers.
- **Q3 Value histories plus pattern observations** (changed): Build the deterministic value-history view AND an observation layer for recurring patterns only (code-grouped, model-worded only for groups that changed). O-15 is reopened from 'none' to 'patterns only'; observations are an opt-in L4 layer (Q1). R-17: observation lines join the capped notes block, lifted to their member records under one score.
- **Q5 Jina on every read if installed** (as proposed): Jina v3.5 reranks every read wherever the model is installed; the pre-run gate is a CPU-only mode; the eval harness forces it on.
- **Q6 Jina if installed, permissive fallback** (as proposed): Jina stays for research and evaluation; the library default is 'Jina if installed', with a permissively licensed reranker named as the commercial fallback (licence check is a research task).

## Change log

- 2026-10-11: First version. All 26 read-area answers were verified:
  - 16 ok, 2 ok-combined, 3 conflict, 5 needs-measurement;
  - R-15 A and R-22 B (choices that differ from the recommendation) were traced to every dependent
    decision;
  - cross-area effects of M-02, O-15 and P-18 were recorded.
- 2026-10-12: owner follow-up answers recorded (Q1, Q3, Q5, Q6).
- 2026-10-13: reconciled with ER amendments and owner decisions (consistency audit)
