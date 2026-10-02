# `evals/` — the memspine evaluation harness

**No benchmark has been run and this directory contains no results.** Any number you see
about memspine anywhere is, as of today, unmeasured. This is machinery, not evidence.

Two seams are still unimplemented and both are named below (§ *What is not built*).
Everything else — configuration, the ablation bridge, both dataset adapters, the build
cache, the cost ledger, the result schema, the frontier ladder — is complete and tested.

This harness lives **outside the wheel** (D-19/D-35). It is not shipped, is not
importable from `memspine`, and may depend on things the slim core cannot. Nothing in
`src/memspine` imports anything here.

---

## What it is for

memspine must run LoCoMo and LongMemEval at an operating point comparable to lightweight
research systems, **and** report what each governance layer costs. The headline result is
a **cost-versus-capability frontier**, not a single score:

```
        accuracy
           ▲
           │      ● full     entity extraction + graph extraction + rerank + compression
           │    ●   graph    + link evolution and graph traversal
           │  ●     consolidation
           │ ●      firewall
           │●       lean     ← the row that sits beside peer systems
           └────────────────────────────────────►  tokens / latency per cycle
```

Only a system whose layers are toggles can produce that curve. Producing it is this
harness's whole job, and it is why every mechanism is a flag and every flag is priced.

Two outcomes are possible and **both are publishable**: the lean profile may score below
peer systems, and the full profile almost certainly costs more per cycle. They are only
embarrassing if we claim to be lightweight and are not.

---

## Quick start

Run from the repository root, in the dev environment (`uv sync --all-extras`).

### 1. See what you can toggle, without running anything

```bash
uv run python -m evals.run --list-ablations
```

Prints all 22 toggles, the exact memspine config key each one binds to, every caveat, and
the frontier ladder. A toggle marked `UNAVAILABLE` names the config option memspine would
need for it to be priceable — selecting it is an error, never a silent no-op.

### 2. Resolve a run without spending a token

```bash
uv run python -m evals.run \
    --dataset locomo --data-path data/locomo10.json \
    --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \
    --dry-run
```

Prints the complete `RunConfig`, both digests, the resolved memspine config, and every
caveat. **Do this first for any run you intend to publish**: if the protocol is wrong,
this is where it is cheapest to find out.

### 3. A lean baseline

```bash
uv run python -m evals.run \
    --dataset locomo --data-path data/locomo10.json \
    --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \
    --profile base --ablation no_firewall,no_evolve_links,no_graph_retrieval,no_consolidation \
    --out evals/runs/locomo-lean.json
```

Generation writes the result file **before** the judge spends a token, so a judge failure
never costs you the generation pass. Re-run the judge with §4.

> `--profile base` rather than the default `benchmark` template — see *Blockers* below.

### 4. Re-judge a stored run — the harness's most valuable trick

```bash
uv run python -m evals.run --score-only \
    --input-results evals/runs/locomo-lean.json \
    --judge-model openai/gpt-4o \
    --out evals/runs/locomo-lean.gpt4o.json
```

**The engine is never constructed.** The generation seam is imported lazily inside the
generation path, so that guarantee is structural rather than a promise. The result file
is self-contained — question, gold answers, dataset-native metadata, prediction, raw
generation, and the retrieved evidence *with its text* — so re-judging never needs the
corpus.

This is what turns judge sensitivity into a *result* ("X under a graded reference-based
judge, Y under a binary reference-free one") rather than an apology in the limitations
section. It is also the single largest cost saver in an LLM-judged evaluation.

`--score-only` refuses any flag it would ignore, rather than accepting it and changing
nothing. Only the judge may change.

### 5. The whole frontier — one command, one row per governance layer

```bash
uv run python -m evals.run \
    --dataset longmemeval --data-path data/longmemeval_s_cleaned.json \
    --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \
    --profile base --frontier \
    --out evals/runs/frontier.json
```

Every rung is resolved and pre-flighted **before any of them runs** — a ladder that fails
on its last rung after paying for the first four is a wasted run — and the frontier file
is checkpointed after each rung. Each rung is a delta on the one below:

| Rung | Turns on | Delta measures |
|---|---|---|
| `lean` | — | the comparable operating point |
| `firewall` | `firewall` | the trust-threshold limb (see the caveat it prints) |
| `consolidation` | `consolidation` | episodic → semantic consolidation (N6) |
| `graph` | `graph_retrieval` + `evolve_links` | associative memory, both sides at once |
| `full` | 6 toggles | a bundle, **not** an attribution |

`--rungs lean,firewall` emits a subset; accumulation still walks the whole ladder, so a
subset row still carries everything it inherits.

### 6. A single-factor ablation

```bash
uv run python -m evals.run ... --ablation no_hybrid_retrieval --single-toggle
```

`--single-toggle` refuses to run unless the ablation deviates from the reference in
exactly one field, so a half-applied or accidentally-compound ablation cannot be reported
as a single-factor result. Read-side ablations reuse the cached build (§ *Caching*).

---

## What is not built

Two seams. Both are resolved lazily by dotted `module:attribute` and both are
overridable, so you can point them at your own implementation today.

| Seam | Default | Contract |
|---|---|---|
| generation | `evals.harness.generation:generate` | `generate(config: RunConfig)` → `RunResults` \| `(items, builds)` \| `[ItemResult]`; sync or async |
| judge | `evals.harness.judge:score` | `score(results: RunResults)` → `RunResults` \| `[ItemResult]`; sync or async |

Neither module exists. Running without them fails with a message that states the full
contract. **The harness does not substitute a stub, a placeholder score, or a mock**:
producing a number nobody measured is the one failure mode this directory exists to
prevent.

The generation seam is the engine adapter, and it is the larger of the two. Its job:

1. Load the corpus — `evals.datasets.base.load_dataset(name, path)` yields
   `BenchmarkSample` for either benchmark.
2. Build or reuse each sample's memory — `BuildCache` keyed on `config.build_digest()`;
   for LoCoMo, `evals.datasets.locomo.build_or_load_memory` already does this end to end
   through the real write door.
3. Retrieve and assemble per question, honouring `config.budget`.
4. Call the backbone named in `config.generation.backbone`.
5. Record per-stage cost with `evals.harness.accounting.CostLedger`, bridged into the
   result schema by `evals.harness.results.stage_costs_from_report`.
6. Stamp `RunSummary.resolved_system_config` from the started engine (the driver already
   stamps the pre-flighted resolution; the engine's own is better).

Aggregation is **not** a seam: `RunResults.save()` recomputes every breakdown, including
the per-category and per-memory-type views, so a stored file cannot disagree with its own
items.

---

## The discipline it enforces

These are not conventions. Each is enforced by the schema or by an import-time check, so
a run that violates one either fails validation or is visibly incomplete in its own file.

### 1. Every result carries (accuracy, tokens, latency)

A score without a cost is not a result. Every `ItemResult` records per-stage `TokenCounts`
and latency keyed by loop stage; every `BuildRecord` records what constructing a sample's
memory cost; the summary carries a `CostPerCycle` block.

Peer harnesses commonly report accuracy and no cost at all. Inheriting that omission
would forfeit both memspine's stated discipline and the framework's cost-per-cycle metric.

The judge's own token spend is tracked **separately** and never enters cost per cycle. The
instrument's cost is not the system's cost.

### 2. Every run records its full configuration

Unreported protocol variation is the single largest source of incomparable scores in this
field — two papers reporting "LoCoMo accuracy" may differ in judge model, rubric,
backbone, sampling, token budget, retrieval depth, and whether memory was rebuilt per
sample, and none of it is written down.

So if it can move a number, it is a typed field: sample selection **and the corpus's
SHA-256**, backbone and judge model with temperature and seed, the answer prompt's and
rubric's SHA-256, the token budget and its `B = system + history + output + retrieved`
split, the embedder and its quantisation, the enabled memory types, the event-log mode,
the engine-internal LLM role, **the user config file's SHA-256**, and **concurrency**
(which does not change accuracy but certainly changes measured latency).

Two digests fall out of this:

| Digest | Covers | Used for |
|---|---|---|
| `protocol_digest()` | everything that can change a number | comparability — same digest means same protocol |
| `build_digest()` | only what changes the *constructed memory* | the per-sample build cache key |

Exclusions from the protocol digest are explicit and audited at import, so a newly added
field is fingerprinted **by default** — the safe direction.

### 3. One flag = one toggle

`AblationConfig` is a flat set of independent booleans **whose defaults are the reference
configuration**. Three import-time checks keep it honest:

- every toggle is classified write-side or read-side, exactly once — an unclassified one
  would drop out of the build cache key, and a run with it flipped would reuse a build
  made without it;
- every toggle has a declared memspine config seam — one without would be accepted on the
  command line and change nothing, putting an unearned row on the frontier;
- every `Dataset` the CLI offers has a loader behind it.

Where memspine has no seam, the driver **refuses the run and names the missing option**.
Where a binding is an approximation, it carries a caveat that is printed to stderr and
recorded in the run's notes, so the published number says what was actually switched off.
`no_firewall` is the sharpest example: it neutralises one of `should_quarantine`'s three
limbs, and the caveat says so.

### 4. Generation is separable from scoring

See Quick start §4. Consequently the result file is self-contained.

### 5. Breakdowns are first-class, not derived later

`per_category` and `per_memory_type` are computed at write time and stored. A breakdown
that is nobody's job does not get reported.

Per-memory-type attribution rule, stated because it is a choice and not a fact: an item
belongs to type *X* when at least one retrieved item came from *X*. Items appear under
several types and the counts do not sum to the item total; `evidence_share` and
`top1_share` give the complementary non-overlapping view.

### 6. No fabricated numbers

Where a statistic is undefined the field is `None`, never `0.0`:

- no items, no judged items, no recorded turns → `None`;
- **no stage cost recorded anywhere** → `cost_recorded: false` and every token *rate* is
  `None`. A run that was not instrumented did not cost zero;
- **any unattributed tokens** → `tokens_per_cycle` is `None`, not a lower bound. Cost that
  arrived outside every phase belongs to no leg of the cycle.

Totals stay integers, because a total is a count of what was recorded. Rates are the field
a reader mistakes for a measurement.

---

## Layout

```
evals/
  README.md              this file
  run.py                 the run driver: CLI, run protocol, toggle→config bridge, frontier
  harness/
    config.py            RunConfig — everything that can change a number, and the digests
    results.py           ItemResult / BuildRecord / RunSummary / RunResults — the file format
    accounting.py        span-level token + latency ledger (stdlib only, imports nothing)
  datasets/
    base.py              the shared contract: sample model, build cache, registry
    locomo.py            LoCoMo parser + ingestion through the real write door
    longmemeval.py       LongMemEval parser, session ordering, abstention
  tests/                 117 tests, no corpus, no keys, no engine
```

`evals/` deliberately has **no `__init__.py`** — it is an implicit namespace package, so
it can never be mistaken for part of the distribution.

### Running the checks

```bash
uv run ruff check evals/ && uv run ruff format --check evals/
MYPYPATH=. uv run mypy --explicit-package-bases --namespace-packages evals/
uv run pytest evals -q
```

`mypy` needs those two flags here: `evals/` is a namespace package, so a bare
`mypy evals/` resolves `evals/harness/accounting.py` under two module names at once and
refuses. Note that **CI runs none of these on `evals/`** — `[tool.mypy] files` is
`["src/memspine"]` and `[tool.pytest] testpaths` is `["tests"]`. Closing that gap is a
pyproject change, which is outside this directory's ownership.

---

## Caching

Construction is the expensive half of a benchmark run; read-side ablations are the cheap
half. Caching the built store per sample is what makes the ablation matrix affordable at
all (MAGMA_HARVEST §2.3). One cache serves both benchmarks, keyed on

```
(dataset, sample_id, profile, config_hash, options_hash, adapter_version)
```

plus a content hash checked inside the manifest. Every component is load-bearing:

- **`config_hash`** — the engine configuration. Without it, a run under one governance
  configuration silently reuses a memory built under another.
- **`options_hash`** — the *ingestion* options: channel, timestamping, **sleep cadence**,
  speaker-role mapping. Sleep cadence is a governance layer this project exists to price;
  absent from the key, pricing it measures nothing and reports a number.
- **content hash** — an edited or re-downloaded corpus invalidates the build.

None of these fails loudly when it is wrong. They just report. `--rebuild` is the escape
hatch when the write path itself changed; the cache lives at `evals/.cache/` and is
gitignored, as are `evals/runs/`.

The cache holds the engine's own on-disk store plus a bookkeeping manifest. It is **not**
a second source of truth for memory content — the event log remains that.

`IngestionStats` is replayed from the manifest on a hit, so a cached run still reports its
write cost instead of a blank cell. Note that `wall_seconds` on a hit is the *original*
build's; `BuildOutcome.reused` disambiguates.

---

## What each result field means

A result file is `{schema_version, summary, items[], builds[]}`.

### `summary`

| Field | Meaning |
|---|---|
| `config` | the complete `RunConfig`. What was **asked for** |
| `resolved_system_config` | the **effective** memspine config the run actually used. `config.system` is a template name plus overrides — a pointer to a mutable file; this closes the gap |
| `protocol_digest` | comparability key. Same digest ⇒ same protocol |
| `build_digest` | what the constructed memory depended on. Runs sharing it share builds |
| `environment` | harness/memspine/python versions, platform, git commit |
| `n_samples` / `n_items` / `n_judged` / `n_errors` | errored items are counted, never silently dropped |
| `overall` | `ScoreBreakdown` over every item |
| `per_category` | one `ScoreBreakdown` per dataset-native category. Uncategorised items land under `"uncategorised"` rather than vanishing |
| `per_memory_type` | `MemoryTypeBreakdown` per memory type, plus `evidence_share` and `top1_share` |
| `costs` | per-loop-stage totals, items **and** builds |
| `judge_cost` | the instrument's spend. Never enters cost per cycle |
| `cost_per_cycle` | see below |
| `ablation_label` / `ablation_deviations` | e.g. `no_hybrid_retrieval`; round-trips back into `--ablation` |

### `overall` / `per_category` / `per_memory_type` (`ScoreBreakdown`)

`n`, `n_judged`, `n_errors`, `judge_score_mean`, `pass_rate` (fraction at or above
`judge.pass_threshold`), `exact_match`, `f1`, `tokens_total` (a count),
`tokens_mean` (`None` when nothing was measured), `latency_ms_mean` / `_p50` / `_p95`
(nearest-rank, so a p95 is always a latency that actually happened), `retrieved_mean`.

### `cost_per_cycle`

LoCoMo and LongMemEval are snapshot QA: the history is frozen and the agent answers probes
over it, which is not the live multi-session episode the framework's cost-per-cycle metric
is defined over. So the harness reports the two halves separately **and** a combined
figure, and does not pretend the combined figure is the episode metric.

| Field | Meaning |
|---|---|
| `cost_recorded` | whether *any* per-stage cost reached this summary. `false` ⇒ every rate below is `None` |
| `questions` / `turns_ingested` | the two denominators |
| `query_tokens_total` | retrieve + compose + generate |
| `build_tokens_total` | deposit + synthesise, from builds **and** from items |
| `query_tokens_per_question`, `build_tokens_per_turn`, `build_tokens_per_question` | the three rates |
| `tokens_per_question` | query plus amortised build, per question. The single-figure cost of a run |
| `query_latency_ms_per_question` | mean item wall time |
| `per_stage_tokens`, `per_stage_tokens_per_question` | the R/C/G/D/K split |
| `cached_prompt_tokens` | prompt tokens served from a provider prefix cache (E2's headline claim, never yet measured in this codebase — this is the field that would measure it) |
| `builds_reused` | cached builds. Their cost is *not* counted as work done by this run |

### `items[]` (`ItemResult`)

`item_id`, `sample_id`, `question`, `gold_answers`, `gold_evidence`, `category`,
`question_meta` (dataset-specific fields a judge may need, stored so re-judging never
reopens the corpus), `prediction`, `raw_generation` (pre-normalisation, because answer
extraction is itself a protocol choice), `context` / `context_tokens`, `retrieved[]`
(record id, memory type, rank, score and its named components, **text**, trust,
quarantined, namespace, provenance), `costs` (per loop stage), `latency_ms`, `attempts`,
`seed`, `error`, `judge`, `lexical`, `started_at` / `finished_at`.

`judge` carries the verdict **and its instrument**: score, passed, label, kind, judge
model, rubric id/version/SHA-256, rationale, raw output, its own cost, and
`judge_run_id` when the verdict came from a re-judge rather than the original run. A score
without its rubric is not comparable to anything.

### `builds[]` (`BuildRecord`)

`sample_id`, `cache_key`, `cache_hit`, `turns_ingested` (the denominator for per-turn
cost), `sessions_ingested`, `records_written`, `events_appended`, `costs`, `wall_ms`,
`store_bytes`, `error`. Without this the write path is invisible and the firewall's price
is unknowable, which is the whole point of the frontier.

---

## Run-config file

Everything the CLI can set, `--config` can set too; CLI flags override the file. Unknown
keys are **rejected** rather than ignored, because a typo'd toggle that does nothing is
exactly how an ablation becomes a lie.

```yaml
run_id: locomo-lean-001
seed: 0
dataset:
  name: locomo
  limit: 10
system:
  kind: memspine
  template: base
  enabled_memories: [semantic, episodic, associative]
  event_log_mode: rolling
  memory_llm: null          # lean target: no LLM call on the write path
ablation:                   # omitted keys take the reference value
  entity_extraction: false
budget:
  total_context_tokens: 8192
  retrieved_tokens: 4096
  top_k: 10
generation:
  backbone: { provider: openai, model: gpt-4o-mini, temperature: 0.0 }
judge:
  kind: graded_reference
  model: { provider: openai, model: gpt-4o-mini, temperature: 0.0 }
  rubric_version: v1
  pass_threshold: 0.5
```

`--out` is still recommended even with a config file: `run_id` is regenerated per
invocation (and per rung) so two runs cannot overwrite each other's results.

---

## Blockers — read before the first real run

1. **The `benchmark` template blocks the frontier.** `benchmark.yaml` sets a `graph:`
   block, and memspine rejects a `graph:` block when the memory that projects it is
   disabled — which the `lean` rung does. An override layer cannot unset a key a template
   set, so the driver refuses with the fix spelled out. Until the `graph:` block is
   dropped from that template (its value is already the schema default), use
   `--profile base`. The template is outside this directory's ownership.

2. **Both seams in *What is not built* must exist.** Nothing runs without them.

3. **Corpora must be downloaded.** Nothing here vendors data.
   - LoCoMo: Snap Inc.'s, under its own terms. Pass `--data-path`, or set
     `MEMSPINE_LOCOMO_PATH`.
   - LongMemEval: `xiaowu0162/longmemeval-cleaned` —
     `longmemeval_s_cleaned.json` (~40 sessions/question), `longmemeval_m_cleaned.json`
     (~500 sessions, **~2.7 GB**, parsed eagerly — expect it to need well over 10 GB of
     RAM through the current loader), or `longmemeval_oracle.json`.

4. **API keys** for the backbone and the judge. The harness names models as
   `provider/model` and never reads a key itself; the seam you write owns that.

5. **`reorganize` is an environment change, not a config change.** Leiden self-skips
   unless `memspine[community]` is installed. The `full` rung records intent; install the
   extra or the row overstates what ran.

6. **memspine cannot inject event time.** No public write method takes a timestamp, so
   LoCoMo folds session dates into the turn text and LongMemEval can only convey time
   through ingestion order. Temporal reasoning is 133/500 LongMemEval questions. Decide
   how to report that before publishing a TR number.

---

## What must not be cut, whatever the schedule pressure

- **The (accuracy, tokens, latency) triplet.** Dropping cost to ship a score forfeits the
  only result this harness is uniquely able to produce.
- **Determinism.** Same log ⇒ same result is a claim memspine can make and most peers
  cannot. Temperatures default to `0.0` for this reason.
- **Pricing the firewall rather than hiding it.** The `lean` rung turns it off so the
  `firewall` rung has something to be a delta *from* — a deliberate deviation from
  PLAN §2.2, recorded in `run.py` beside the ladder. What matters is that the rung above
  turns it back on and prices it, and that the caveat says precisely what the off-row
  disabled.
- **Re-running peers under this harness.** A comparison against numbers copied from
  someone else's paper, with a different judge and backbone, is not a comparison.
  `SystemUnderTest.EXTERNAL` exists so peer systems are re-run here, and `full_context` /
  `no_memory` exist as the cost ceiling and the accuracy floor.

## Reading list

- `docs/memspine-structure-plan.md` — decision register, E1–E9 (the ablation matrix)
- `docs/ARCHITECTURE_FLOWS.md` §2 — the write / read / sleep flows the toggles switch
- `docs/adr/` — one decision, one ADR; harness decisions belong there too
