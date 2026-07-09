# ADR-017 — P7 infrastructure: REST, taskiq runner, E5/E8 stages

**Status:** accepted · **Date:** 2026-07-07 · **Register:** D-51

## Context

P7's memory-type leg landed in ADR-016 (watches + grants). This ADR records
the infrastructure leg: the REST protocol (D-06), the taskiq brokered runner
(D-16/D-42 §3), assembly-time compression (E5), and the opt-in
retrieval-quality stages (E8/D-42 §5). Everything here is additive and
config-gated: `profile="simple"` behavior is bit-identical with the defaults.

## Decisions

### 1. REST namespace binding is the deployer's job (ADR-016 open question 2)

`protocols/rest` ships **no authentication in v0.1**. `create_app(engine)`
wraps ONE caller-owned engine (lifecycle stays with the caller — no lifespan
magic); the caller's namespace comes verbatim from the `X-Memspine-Namespace`
header (default `"default"`) through the `resolve_namespace` dependency.
Binding caller→namespace is **out of scope for v0.1 and explicitly the
deployer's job**: run the app behind a reverse proxy / auth middleware and
override the dependency
(`app.dependency_overrides[resolve_namespace] = ...`) — that override hook IS
the seam. The module docstring carries the same warning loudly. Error
mapping: `ConflictError`→409, `MissingServiceError`→501, `MemspineError`→400,
unknown→500 with a generic body — internal messages and stack traces never
leak on the unknown path. Responses are orjson (D-38) and reuse the universal
`MemoryRecord` shape (no second wire schema). The fastapi import lives only
in `protocols/rest/app`; the `create_app` entry point guards it and raises
`MissingServiceError(extra="rest")` (D-10).

### 2. Taskiq runner = per-scope streams + priorities + claim-recovery

`workers/taskiq_runner.py` adopts the MemOS `SchedulerRedisQueue` pattern
(D-42 §3): stream key `memspine:<scope>:<label>` per (namespace-scope,
task_label); priority labels mirroring **exactly** the pipeline set
(`check_watches` most urgent → `event_log_prune` last; a test pins the label
set to `PIPELINES`); consumer-group delivery with **XAUTOCLAIM
claim-recovery**. Each `run()` appends a durable work marker (XADD), claims
it (XREADGROUP) and acknowledges on success (XACK); a crashed run leaves the
marker pending for `claim_stale` — safe because pipelines are idempotent
(D-17). Honesty note (same posture as the DBOS runner): pipeline bodies
execute in-process because `PipelineContext` holds live connections; the
worker-fleet/push-delivery story (ADR-016) consumes these same streams later.
v0.1 pipelines are engine-wide, so the default scope is `engine`;
per-namespace scoping joins when pipelines take a namespace. Broker outages
degrade loudly to inline execution — maintenance never depends on broker
health. Lazy import gate: `MissingServiceError(extra="taskiq")` (D-10);
config: `workers.runner: taskiq` + `workers.broker_url`.

Testing (D-41): taskiq-redis cannot run against fakeredis cleanly (it owns
its connection from a URL), so the stream mechanics are tested against an
injected fakeredis client end-to-end (enqueue→claim→ack, dead-letter→
XAUTOCLAIM recovery) and the key/label/priority logic as pure functions;
taskiq stays out of the dev environment.

### 3. E5 assembly-time compression: ordered fallbacks, never-touch rules

`CompressionPolicy` gains the E5 assembly stage (config `read.compression`,
master switch `assembly: false` — **default OFF**). Ordered fallbacks, each
tried only while the selection still exceeds the budget:
`drop_lowest_score` → `llmlingua` (`[compress]`, lazy import, ONE info log
when absent) → `provider_compaction` (no-op seam for provider-side context
editing). **Never dropped or compressed:** persona blocks (E2 stable prefix),
instruction-flagged content (the E1 `INSTRUCTION_FLAG_WRAP` framing must
reach the model verbatim — the flag survives the wrap copy, so the guard
holds), and disputed (`RESOLVING`) records. If protected blocks alone exceed
the budget the result stays over — the E1/E2 guarantees beat the budget. With
E5 on, MMR orders the full candidate set and the fit stage trims; the M12
never-empty rule survives.

### 4. E8 retrieval stages: `[static_prefilter?] → hybrid → [rerank?] → score → MMR → assemble`

All opt-in, **default OFF** (`read.rerank: off`, `read.static_prefilter:
false`) — off means bit-identical results to the plain pipeline.

- **Rerank providers:** `fastembed` (ONNX `TextCrossEncoder`; rides the core
  fastembed dep where the installed version ships rerankers) and `flashrank`
  behind the **new `[rerank]` extra** (added because fastembed's reranker
  availability is version-dependent). Lazy construction on first search
  (models must not load at engine start); an unavailable provider is
  skip-logged once and the stage self-disables — retrieval degrades, never
  fails. Cross-encoder logits are min-max normalized into [0,1] so they
  compose with the M1 composite exactly like cosine relevance.
- **Strategy rerank** (D-42 §5): the reranker scores `concat_background`
  text — record content prefixed with `type/entity/attribute/channel/doc`.
- **Static prefilter:** a cheap lexical-overlap gate applied to the candidate
  set after the vector/hybrid leg (a true pre-vector static-embedding prefilter
  is the E4 model2vec track); an empty filtrate falls back to the unfiltered set.
- **No foreign-hit penalty:** trust-capped grant results get NO artificial
  rank penalty — `TRUST_RETRIEVED_CAP` already carries the signal (E1);
  double-counting it would silently bury legitimately granted knowledge.
- **BM25 corpus cache:** the D-42 §5 LRU cache now lands with the lexical port —
  a bounded (`LEXICAL_CACHE_MAX_ENTRIES`) query-result cache in the FTS5 store,
  invalidated on every index mutation.

### 5. E8 hybrid RRF is IMPLEMENTED, opt-in (see ADR-019)

> The full decision — port surface, RRF normalization, NUL/length hardening,
> and the cleartext-FTS tradeoff — now lives in **ADR-019 (hybrid retrieval)**,
> which supersedes this section. The summary below is retained for continuity.

The `services/lexical`/D-25 port now exists (SQLite **FTS5/BM25** core default,
`rrf_fuse` implemented once in the port, standalone Tantivy adapter implemented
behind `[tantivy]` — later recorded in full by ADR-019/D-53),
so hybrid RRF fusion of the vector + lexical legs is **built and wired into
`Engine.search`** — this reverses the earlier "DEFERRED until that port lands"
posture. It is **opt-in via `read.hybrid` (default FALSE)**: off, the retrieval
leg is vector-only and results are **bit-identical** to before (no lexical
store or projector is even constructed — `simple` stays inert at the write/read
path). **Default-on is the intended v0.2 flip** (D-25's core-default intent),
held back in v0.1 only for strict backward-compat.

> **Caveat (migration 0008):** the `memory_fts` table is created empty for
> **all** profiles by migration 0008, `simple` included. This is harmless — it
> is an empty structure with no write-path cost (the projector that would fill
> it is registered only when `read.hybrid` is on), so "`simple` stays inert"
> refers to the write/read pipeline, not the schema.

- `Engine.search` is documented **"vector/hybrid (opt-in)"**. With
  `read.hybrid: true` the lexical leg is fused via `rrf_fuse` and a record only
  BM25 would surface can enter the results; the E1 status/quarantine gate runs
  on the **fused** candidates, so held content never crosses the lexical leg.
- The lexical index is a **rebuildable projection** (`LexicalProjector`),
  registered only when hybrid is on, so `rebuild()` replays it and turning
  hybrid on for an existing DB backfills the index from the log via catch-up.
- `static_prefilter` still runs **after** the vector/hybrid leg (a cheap
  lexical-overlap gate over the candidate set), not as a true pre-vector
  static-embedding prefilter (that is the E4 model2vec track).
- **FTS5-missing fallback:** if the SQLite build lacks the FTS5 module the store
  degrades to a `LIKE` scan and logs the downgrade once (the D-25 ILIKE path).

### 6. REST deployment guidance (amended ADR-018)

The no-authn posture (§1) has a blast radius the deployer must contain:

- `/sleep`, `/rebuild`, and `/audit/taint` are **engine-global or
  cross-cutting** (they run maintenance over every namespace or walk the whole
  log). They MUST sit behind an **internal-only network boundary** — never
  reachable by tenant callers.
- REST ships with **NO authentication**; the deployer owns BOTH the
  caller→namespace binding (via the `resolve_namespace` override) AND the fact
  that role-based trust is neutralized at the REST boundary (writes are forced
  onto the external `rest` channel — ADR-018/SEC-C1).
- A **request-body size cap** (`REST_MAX_BODY_BYTES`, default 1 MiB → 413)
  guards the trivially-abusable unbounded-body path (ADR-018/SEC-M3).

## Register row (D-51)

See the structure-plan register. Extras touched: `rerank` (new, flashrank);
`rest`/`taskiq`/`compress` unchanged in pyproject but now implemented;
fastapi added to the dev group (REST is dev-tested in-process via httpx
ASGITransport, no server).

## Alternatives rejected

- REST authn (API keys/JWT) in v0.1 — half a security model is worse than a
  loud, documented seam; deployments already own an auth layer.
- `create_app(config)` lifespan factory — hides engine ownership and breaks
  the ONE-engine-per-process clarity of D-06; callers manage start/stop.
- Running taskiq's decorated-task machinery now — `PipelineContext` is not
  payload-addressable; pretending to broker it would serialize live handles.
- Rank penalty for cross-grant hits — trust already carries it (above).
- Compressing flagged content "because it is wrapped anyway" — compression
  can destroy the inert framing itself; protected means untouched.
