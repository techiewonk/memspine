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
  set after the vector leg (a true pre-vector static-embedding prefilter is
  the E4 model2vec track); an empty filtrate falls back to the unfiltered set.
- **No foreign-hit penalty:** trust-capped grant results get NO artificial
  rank penalty — `TRUST_RETRIEVED_CAP` already carries the signal (E1);
  double-counting it would silently bury legitimately granted knowledge.
- **Deferred:** the D-42 §5 LRU corpus cache for the BM25 leg — `services/
  lexical` (the D-25 port) is not built yet, so there is no BM25 leg to
  cache; the cache lands with that port.

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
