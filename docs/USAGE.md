# memspine — usage guide

How to install, construct an `Engine`, and drive every memory type with the real
API. All snippets are grounded in `src/memspine/engine.py`,
`src/memspine/cli.py`, and `src/memspine/protocols/rest/`. Runnable end-to-end
tours are in [`examples/`](../examples).

For *what* each feature is, see [`FEATURES.md`](./FEATURES.md).

---

## Install

The **core** install is slim — SQLite storage + FTS5, **LanceDB vector** (ADR-021),
fastembed embeddings, inline workers. Add capabilities as extras.

```bash
# Users:
pip install memspine
pip install "memspine[kuzu,ingest,rest]"   # pick the extras you need

# Developers (all extras + test/lint/docs tooling):
uv sync --all-extras
just check        # ruff + mypy --strict + pytest
```

Common extras: `kuzu` (graph), `ingest` (markitdown+chonkie), `ner` (gliner2),
`structured` (instructor), `compress` (llmlingua, E5), `rerank` (flashrank, E8),
`community` (graspologic-native), `rest` (FastAPI), `dbos`/`taskiq` (durable/brokered workers). See the
[README extras table](../README.md#-install--extras) for the full set.

> A feature that needs a missing extra raises `MissingServiceError` naming the
> extra (unless `strict_services: false`).

---

## Constructing an Engine

The constructor layers config: **defaults → template → user YAML → env → kwargs**
(D-11). Nothing runs until you `await engine.start()`.

```python
from memspine import Engine

Engine(
    template=None,                 # a shipped template name, e.g. "base", "coding"
    user_config=None,              # path to a YAML file, or a dict
    dotenv_path=".env",            # None to skip .env loading
    **overrides,                   # highest-precedence config, e.g. storage={"path": ...}
)
```

**From a template** (partial overlay on `base`):
```python
engine = Engine(template="personal")           # working+episodic+semantic+reflective+prospective
await engine.start()
```

**Explicit config via kwargs** (override any block):
```python
engine = Engine(
    template="base",
    storage={"backend": "sqlite", "path": "./memspine.db"},   # ":memory:" for ephemeral
    embedding={"provider": "hash"},             # deterministic, offline; default is "fastembed"
    vector={"backend": "lance"},                # lance is the sole store (ADR-021); weaviate reserved
    memories={
        "semantic": {"enabled": True, "policies": {"entity_extraction": "off"}},
        "prospective": {"enabled": True},
    },
    read={"rerank": "off", "hybrid": False},
    strict_services=True,
)
await engine.start()
...
await engine.stop()
```

**From a YAML file:**
```python
engine = Engine(user_config="memspine.yaml")
```

Inspect the effective world any time:
```python
print(engine.describe())   # enabled types, services, event-log mode, projectors, runner, ...
```

### Templates

| Template | Enables |
|----------|---------|
| `base` (profile `simple`) | working + episodic + semantic, **with the measured read-path advantages on** (ADR-033): replay reads, relative dates resolved, time order for ordering questions, relative floor, relevance-first scoring, no access recording, dated answer prompt |
| `core` | the bare configuration (pre-ADR-033 `base`): auto read mode, composite scoring, access recording, base chat prompt |
| `coding` | + procedural (`conflict_bias: newest`) |
| `personal` | + reflective + prospective |
| `voice` | rolling+zstd event log; tighter working window (`page_size: 8`) |
| `multi_agent` | + shared; secure defaults: `integrity.enabled`, `untrusted_wrap_below: 0.5`, `redact_secrets`, `pii: redact` |
| `regulated_financial` | full audit log, strict PII, no forgetting; the same secure defaults as `multi_agent` |
| `assistant` (**default**, ADR-032) | = `base` (since ADR-033 the advantages live in `base`); the name for chat workloads (LoCoMo 70.7 → 78.3%, measured) |

> **Default template (ADR-032).** `Engine()` with no `template` loads `assistant`. Pass `template="base"` for the plain `simple` profile (the previous behaviour). A caller that names a `profile` but no template (`Engine(profile="simple")`, or `profile:` in the user config) gets `base`, never the assistant settings. The CLI `audit taint` / `forget` commands always run on `base`.

---

## The API is async-first

Every verb is a coroutine — `await` it inside an async context, or wrap a
top-level call in `asyncio.run(...)` as the examples do. Thin **sync wrappers**
exist for a small subset only: `start_sync`, `write_sync`, `retrieve_sync`,
`stop_sync`. Calling a sync wrapper from inside a running loop raises.

```python
engine = Engine(template="base", embedding={"provider": "hash"})
engine.start_sync()
engine.write_sync("hello", namespace="demo")
print(engine.retrieve_sync(namespace="demo"))
engine.stop_sync()
```

---

## Worked examples per memory type

Assume `engine` is started. See [`examples/`](../examples) for full versions.

### Semantic — write + search
```python
await engine.write(
    "primary region is eu-west-1",
    namespace="ops", entity="deploy", attribute="region",   # keys the conflict ladder
)
for record, score in await engine.search("where do we deploy?", namespace="ops", top_k=8):
    print(f"{score:0.3f}  {record.content}")
```
`write` returns the materialized `MemoryRecord`; `search` returns
`(record, score)` pairs sorted by the M1 composite score (recency · relevance ·
importance · utility), not raw cosine.

**Date filters (#37).** `search()` and `read()` take keyword-only bounds on the three
time columns: `valid_from_after` / `valid_from_before` (event time),
`valid_to_after` / `valid_to_before` (when a fact stopped holding; an open
`valid_to` counts as later than any date) and `recorded_after` / `recorded_before`
(when memspine stored it), as datetimes, dates or ISO text. `*_after` is inclusive and
`*_before` exclusive, so May 2023 is `valid_from_after="2023-05-01",
valid_from_before="2023-06-01"`. `date_filter_mode="and"` (default) keeps records that
meet every bound, `"or"` those that meet any. The filter restricts every retrieval leg
before fusion and the `top_k` cut (each leg looks over the whole namespace), so it never
costs recall; in `read()` it also applies to the headers' searches, a `full` read's
listing and replayed neighbours (the pinned persona and the lead section are not
filtered). `POST /search` takes the same fields.
```python
may = await engine.search(
    "what did Melanie do", namespace="ops",
    valid_from_after="2023-05-01", valid_from_before="2023-06-01",
)
```

**Answer verification (#39).** `await engine.verify_answer(question, answer, context)`
checks an answer against a read's context (an `AssembledContext`, its records, or text)
with one call to the `verify_answer` LLM role (the `chat` role when that one is not
bound) and returns `{"supported": bool, "evidence_ids": [...], "revised_answer": str |
None}`: the record ids (`L<n>` for a text context) of the supporting lines, and a
corrected answer when the given one is not supported but the context supports another.
No read path calls it; the eval harness exposes it as `--verify-answer`.

### Working — persona + assembly
```python
await engine.set_persona("agent/demo", "You are a concise coding assistant.")
await engine.write("user likes type hints", namespace="agent/demo", memory_type="working")

ctx = await engine.assemble("what does the user like?", namespace="agent/demo", budget_tokens=500)
for i, record in enumerate(ctx.records):
    boundary = "  <-- cache boundary" if i == ctx.boundary_index else ""
    print(i, record.memory_type, record.content, boundary)
print("abstained:", ctx.abstained, "tokens:", ctx.tokens_used)
```
Records before `boundary_index` are the stable prefix (persona, facts) to feed
provider prefix caching; volatile episodic/working content follows.

### Episodic — timeline + sessions
```python
await engine.write("build started", namespace="dev", memory_type="episodic")
events = await engine.timeline(namespace="dev")
sessions = await engine.sessions(namespace="dev", gap_minutes=30)
```

**Session lifecycle (#53, opt-in).** Conversations idle longer than
`memories.episodic.policies.sessions.passive_after` (seconds, or `"30d"`, `"12h"`;
default `null` = off) are marked PASSIVE by the `session_lifecycle` sleep stage:
```yaml
memories:
  episodic:
    enabled: true
    policies:
      sessions:
        passive_after: 30d
```
A session is the `session_id` you pass to `write_messages` / `write_episode`. Its
records stay stored, but `search` / `assemble` / `read` / `retrieve` leave them out
unless you pass `include_passive=True`, name the session (`session_id="..."`) or its
`group_id`. A new write to the session reopens it. Each change is a `memory.session`
event, so `rebuild()` reproduces the same passive set (ADR-037).

**`session_id` on reads is not a session filter (I9).** On `write`, `send`, `search`, `assemble`, `read` and `shared_search`, `session_id` does two things:
- it keys the B0 read ledger: what this caller was shown becomes the implicit parents of its next write;
- it un-hides that session if it is passive.

It does **not** restrict the read to that conversation; the whole namespace is searched. `ledger_id` is the clearer name for the same argument (`ledger_id` wins when both are given).

**Session and role filters (I7 / I8).** `search`, `assemble` and `read` take two filters, applied inside the namespace:
- `sessions=[...]`: the conversation ids given to `write_messages(session_id=...)`;
- `roles=[...]`: `user`, `assistant`, `tool`, and so on.

A record outside the scope never reaches the context. That covers search hits, replay neighbours, `full` mode, and the recent-conversation and digest headers. Both filters combine with each other and with the date filters. Two more filters, `memory_types=[...]` (e.g. `episodic`, `semantic`) and `tags_any=[...]` (at least one tag), work the same way (G-12). Example: `read(q, namespace="u", sessions=["trip-1"], roles=["user"])`.

**One conversation (I6).** `conversation(namespace, session_id, roles=None)` returns the live turns of one conversation in time order. It reads them through the indexed `session_key` column (migration 0005 adds `session_key` and `source_role` and backfills existing rows from their source).

**Per-read listing snapshot (I2).** Within one `search` / `assemble` / `read` call, each listing of a namespace's records is fetched from storage once and shared by every leg and header. Any write during the read clears it, so results are the same as without it.

### Resource — ingest *(needs `memspine[ingest]`)*
```python
chunks = await engine.ingest("docs/runbook.md", namespace="ops")
```

### Procedural — skill ladder + plans
```python
s = await engine.add_skill("run pytest -q then ruff check", name="verify", namespace="dev")
s = await engine.promote_skill(s.record_id, namespace="dev")                       # draft -> staged
s = await engine.promote_skill(s.record_id, namespace="dev")                       # staged -> verified
s = await engine.promote_skill(s.record_id, namespace="dev", dry_run_passed=True)  # -> active
active = await engine.skills(namespace="dev")        # ACTIVE only by default

await engine.record_plan("ship a release", "1. bump version 2. tag 3. push", namespace="dev")
plan = await engine.recall_plan("cut a release", namespace="dev")   # None if nothing clears the floor

# ADR-060 (plan v3.2, no model calls): outcomes, lessons, top-k plans, trajectories,
# task state and a kNN label vote.
await engine.record_outcome("ship a release", "failure", action="push tag",
                            error="TimeoutError: registry", next_action="retry with --wait",
                            used_ids=[plan.record_id] if plan else [], namespace="dev")
plans = await engine.recall_plans("ship a release", k=3, namespace="dev")  # [(record, score)]
lessons = await engine.recall_lessons("ship a release", namespace="dev")
steps = await engine.record_trajectory("ship a release", [{"action": "bump"}, {"action": "tag"}],
                                       "success", namespace="dev")
await engine.set_task_state("rel-42", "ship 1.2", subgoals=["bump", "tag"], namespace="dev")
await engine.update_subgoal("rel-42", "bump", "done", receipt_id="ci-981", namespace="dev")
await engine.add_exemplar("how do I reset my password", "howto", group="intents")
vote = await engine.classify("how can I change my email", group="intents")  # label, margin, label_table
```

### Reflective — derive from records
```python
a = await engine.write("build failed on windows", namespace="dev", memory_type="episodic")
b = await engine.write("build failed on windows again", namespace="dev", memory_type="episodic")
note = await engine.reflect("windows builds are flaky", [a.record_id, b.record_id], namespace="dev")
```

### Associative — links + graph recall
```python
await engine.associate(a.record_id, b.record_id, namespace="dev", rel="related", weight=1.0)
neighbours = await engine.related(a.record_id, namespace="dev", k=10)   # personalized PageRank
```

With `memories.associative.policies.entity_nodes: true`, records also link to the entities
they name, and `read.graph_leg` / `read.cards_include_edges` read the graph from the
entities a query names (see the config-key reference). `related()` ignores entity edges.
`policies.entity_summaries` adds a per-entity summary (sleep stage `summarize_entities`),
shown as `About <Name>: …` with `read.entity_summaries`; `read.graph_communities` lets a
community summary into a read only when an entity the query names belongs to it; and
`semantic.policies.extract_graph.resolve` merges name variants ("Mel" ≡ "Melanie") before
extracted facts are written.

**Raw turns (G-7).** `entity_nodes: {turn_mentions: true}` makes raw episodic turns link to the proper nouns and years they name, found by rule with no model call. The turn's speaker is left out, and so are capitals that only open a sentence. The graph and `read.graph_node_search` can then reach turns no fact was mined from.

`policies.rule_edges` adds model-free `because` links between turns and kinship facts
(sleep stage `rule_edges`); `read.causal_walk: why` walks them from the best hits so a
"why" question reaches the turn holding the cause. `write(..., reply_to=record_id)`
threads a reply to the message it answers; `read.reply_links` shows that message beside
a replayed reply (ADR-061).

### Prospective — watches
```python
from datetime import UTC, datetime, timedelta
now = datetime.now(UTC)

w = await engine.watch("rotate the API key", due_at=now + timedelta(minutes=30), namespace="ops")
fired = await engine.due(namespace="ops", now=now + timedelta(hours=1))
await engine.acknowledge_watch(w.record_id, namespace="ops")

# invalidation watch: fires when the watched fact is superseded
await engine.write("region is eu-west-1", namespace="ops", entity="deploy", attribute="region")
await engine.watch("recheck runbooks", namespace="ops", entity="deploy", attribute="region")
await engine.write("region is us-east-2", namespace="ops", entity="deploy", attribute="region")
fired = await engine.due(namespace="ops", now=datetime.now(UTC))
```

### Shared — grants + cross-namespace search
```python
await engine.grant("analyst", namespace="ops", memory_types=["semantic"])
results = await engine.shared_search("primary region", namespace="analyst")
for record, _ in results:
    if record.namespace != "analyst":
        print("foreign hit:", record.namespace, "trust:", record.trust)   # capped
issued = await engine.grants_from(namespace="ops")
await engine.revoke("analyst", namespace="ops")
```

### Governance
```python
report = await engine.audit_taint(record_id, namespace="ops")   # origin + blast radius
await engine.forget(record_id, namespace="ops")                 # soft: status=DELETED
await engine.forget(record_id, namespace="ops", hard=True)      # hard: row + log payloads redacted
proof = await engine.verify_forget(record_id, namespace="ops")  # {"clean": True, ...}
await engine.approve_quarantined(held_id, namespace="ops", actor="ops:lee", principal="ops:lee")
```

A hard forget cascades to every record derived from the forgotten one: mined
facts, cues, reflections, and the consolidation and reorganize summaries whose
members include it (a summary of an erased member is erased, never re-derived).
It also rewrites the LanceDB vector table and drops its older versions, so the
erased vector cannot be checked out again; `verify_forget` reports
`vector_history_absent` and stays unproven (`clean: False`) when that purge did
not run. `residual_risks` names what the proof cannot cover: a Tantivy segment
keeps a deleted document's terms until it is merged.

Caller tags never include the engine-only tags (`constants.RESERVED_TAGS`:
lead-section and header tags, the cue tag, `taint_archived`,
`quarantine_rejected`); the write door drops them. `approve_quarantined` refuses
a reviewer whose `principal` or `actor` is the held record's `source.principal`.

```python
await engine.sleep()      # run consolidate -> decay -> compress -> prune now
await engine.rebuild()    # replay every projector from seq 0
```

### Data-subject rights and audit (#46-#51, all opt-in)
```python
lines = await engine.export("user/ana", subject="ana", include_events=True)  # JSONL lines
new = await engine.correct(("ana", "city"), "Ana lives in Nice",
                           actor="ana", reason="moved", namespace="user/ana")
await engine.forget(record_id, namespace="user/ana", hard=True, actor="ana", reason="art. 17")
await engine.write("ticket #42 ...", namespace="user/ana", purposes=["support"])
hits = await engine.search("ticket", namespace="user/ana", purpose="support")
await engine.expire_retention()          # retention.classes, also a sleep-cycle stage
assert await engine.audit_chain_ok()     # audit.reads / audit.actions hash chain
```

- **Export** (#46): a header line, then one line per live, archived or held record
  (soft-forgotten ones are left out) in `(recorded_at, record_id)` order, with
  provenance and, by default, archived versions; `include_events=True` adds the
  namespace's log events as stored (hard-erased content is already redacted; the
  content of soft-forgotten records is scrubbed). Same data, same bytes.
- **Correct** (#47): user-direct supersession by record id or `(entity, attribute)`.
  The conflict ladder does not run, so a correction is never CONTESTed under
  `contest_lower_trust` (the `base` template); the firewall still screens the new
  value. The new record's WRITE event carries `correction: {supersedes, actor, reason}`.
- **Read audit / hash chain** (#49), **purposes / remote-LLM gate** (#50) and
  **retention classes** (#48): see `audit.*`, `consent.*` and `retention.classes`
  in the config-key reference. `memspine.core.privacy.principal_scope(name)` binds
  the principal recorded by the read audit outside REST.
- The FORGET event itself still records `actor: user`; under `audit.actions` the
  chained `memory.audit` event carries the real actor and reason.
- `erase_subject(subject, namespace, *, actor=, reason=)`,
  `erase_namespace(namespace, *, actor=, reason=)` and `revoke(..., *, actor=)`
  take the actor recorded in that event. Erasure also renames the erased subject's
  entity node id (`ent:<ns>:<name>`) in the community-partition history, and a
  hard forget does the same for an erased record's entity once no record mentions
  it. Redacting one turn cited by an `entity_resolved` decision redacts every entry
  of that decision (its `group`).
- **Derived records inherit governance**: a record written with `source.parents`
  in its own namespace (summaries, mined facts, list cards, entity and community
  summaries, surprise facts, cues, reflections, graph facts) gets the intersection
  of its parents' purposes and the highest of their PII tiers. Parents with no
  common purpose give `!none`, which no read purpose matches. Read lead blocks
  (standing requests, timelines, cards, profile, entity summaries) and graph blocks
  pass the purpose gate per entry, and each block carries its entries' common
  purposes and highest tier.
`rollback_taint` / `repair_taint` walk the event log from the seed's origin WRITE. With
`event_log.mode: ephemeral` (no events kept, not even in memory) or a `rolling` window
that pruned the origin, they cannot trace descendants: by default they log
`memory.rollback_beyond_window`, archive the seed alone (closing its `valid_to`) and
return `"untraced": [seed]`; pass `strict=True` to get `RollbackUnavailableError`
instead and change nothing (ADR-011 addendum, #64).

### Feedback — like / dislike / note (#54)
```python
await engine.feedback(record_id, "like", namespace="ops")
await engine.feedback(record_id, "dislike", note="moved to Lyon in May", namespace="ops")
rec = await engine.feedback(record_id, "note", note="check with Ana", namespace="ops")
rec.scoring.likes, rec.scoring.dislikes, rec.scoring.notes   # (1, 1, 2)
```
Each call appends one `memory.feedback` event; the record projector keeps the counts.
They affect ranking only through `read.scoring.utility_weight` (0 in the `base`
template): utility then adds `tanh((likes - dislikes) / 3)`, bounded in (-1, 1). Notes
are screened like messages, capped at 2000 characters, kept in the log only, and
erased by a hard forget of the record (ADR-036). REST: `POST /feedback`.

### Cost accounting — per role and per prompt (#33)
```python
engine.model_calls()      # {"extract": 3, ...}            calls per LLM role
engine.model_usage()      # {"extract": {"model", "calls", "prompt", "completion"}}
engine.usage()            # {"extract@2": {"prompt_id": "extract", "roles": ["extract"],
                          #   "calls": 3, "input_tokens": 912, "output_tokens": 140,
                          #   "estimated_calls": 0, "estimated": False}, ...}
engine.usage(reset=True)  # snapshot, then clear the per-prompt counters
```
Every internal LLM call renders a named, versioned prompt (D-43), so `usage()` keys
its counters by `prompt_version` (`<id>@<version>`; `"<unnamed>"` for messages you
send yourself through `engine.llm(role)`). Tokens are the provider's own report when
it gives one (LiteLLM); otherwise a characters/4 estimate, counted in
`estimated_calls`. Each call also logs an `llm.usage` event at DEBUG level. The
counters live in the process only (never persisted). The evals harness reads them
into each result's `meta["engine_prompts"]` and the run summary's
`engine_prompt_usage` (per loop stage, per prompt), which is how cost per cycle is
attributed to stages.

---

## CLI

Installed as `memspine` (see `pyproject.toml [project.scripts]`).

```bash
# Config (D-11/D-12)
memspine config validate -t personal              # dependency closure + effective combination
memspine config validate -c ./memspine.yaml
memspine config resolve                            # merged config, "# source:" per key

# Prompts (D-43)
memspine prompts list                              # id, version, role, format, source layer
memspine prompts show extract                      # full frontmatter + body of one prompt
memspine prompts resolve                           # id@version # source: defaults|override

# Governance (E1 / M7)
memspine audit taint <record_id> --db ./memspine.db -n <namespace>
memspine forget <record_id> --db ./memspine.db -n <namespace>
memspine forget <record_id> --hard --verify        # provable erasure; exits 1 if not clean
memspine export --db ./memspine.db -n user/ana --out ana.jsonl   # subject-access export (#46)
memspine export -n user/ana --subject ana --events --no-history --out ana.jsonl
```

`config`/`prompts` commands accept `-t/--template` and `-c/--config`.
`forget`/`audit` boot a throwaway engine on `--db` (hash embedder, deterministic).

---

## REST protocol *(needs `memspine[rest]`)*

One FastAPI app wraps one `Engine`; **you own the engine lifecycle** — start it
before serving, stop it after.

```python
from memspine import Engine
from memspine.protocols.rest import create_app

engine = Engine(template="personal")
await engine.start()
app = create_app(engine)         # serve with: uvicorn my_module:app
```

`create_app` is the only import safe without fastapi installed — it raises
`MissingServiceError("protocols.rest", extra="rest")` if the `rest` extra is
missing.

### Routes

| Method & path | Verb |
|---------------|------|
| `POST /write` · `POST /search` · `POST /assemble` · `POST /retrieve` | core read/write (`/search` takes the #37 date-filter fields) |
| `DELETE /records/{id}?hard=&reason=` · `GET /describe` | forget · introspect |
| `POST /correct` | #47 correction by `record_id` or `entity` + `attribute` (never contested) |
| `GET /export?subject=&include_history=&include_events=` | #46 subject-access export, `application/x-ndjson` (admin under auth) |
| `DELETE /records/{id}?hard=` · `GET /describe` | forget · introspect |
| `POST /feedback` | like / dislike / note on a record (#54) |
| `POST /skills` · `POST /skills/{id}/promote` · `DELETE /skills/{id}` | procedural |
| `POST /plans` · `GET /plans/recall` | plan cache (E6) |
| `POST /reflect` | reflective |
| `POST /watches` · `GET /watches/due` · `POST /watches/{id}/ack` | prospective |
| `POST /grants` · `DELETE /grants` · `GET /grants` · `GET /shared_search` | shared |
| `POST /subscriptions` · `GET /subscriptions` | standing queries |
| `POST /sleep` · `POST /rebuild` · `GET /audit/taint/{id}` | maintenance / governance |

Errors map cleanly: `ConflictError`→409, `MissingServiceError`→501,
`MemspineError`→400, anything else →500 with a generic body (no stack traces
leak). Request bodies over 1 MiB are rejected with 413.

### Reference auth middleware (#51, ADR-041)

`rest.auth.mode: api_key` or `oidc_jwt` turns on a reference middleware that binds
an authenticated principal and its allowed namespaces to each request:

```yaml
rest:
  auth:
    mode: api_key
    api_keys:
      - {key_env: MEMSPINE_KEY_SUPPORT, principal: support-bot, namespaces: ["tenant-a/*"]}
      - {key_env: MEMSPINE_KEY_OPS, principal: ops, namespaces: ["*"], admin: true}
  rate_limit: {requests_per_second: 5, burst: 20}
```

- No or bad credentials → 401; a namespace outside the principal's globs → 403;
  `/sleep`, `/rebuild`, `/export`, `/quarantine…` without the admin role → 403;
  over the rate limit → 429. The admin check is a dependency on those routes
  themselves, so it holds when the app is mounted under a prefix
  (`outer.mount("/api", create_app(engine))`) or served with a `root_path`.
- Failed authentications are throttled per client address **before** the credential
  is checked: after `rest.rate_limit.burst` failures (10 without `rest.rate_limit`,
  refilling at 0.1/s) the address gets 429 until its bucket refills.
- The principal replaces the caller-claimed `actor` on `/write`, `/write_messages`,
  `/feedback`, `/correct`, `/grants` and forget, and `source.principal` on `/write`
  and `/correct` (a differing claim is overridden and logged), so a writer cannot
  omit itself to approve its own quarantined write. It is also the `actor` of
  `memory.read_audit` events.
- `oidc_jwt` needs `pyjwt` (a clear `ConfigError` otherwise) and reads the principal,
  namespaces and roles from configurable claims; keys come from `jwks_url` or an env var.
  `rest.auth.jwt.issuer` and `audience` are **required** in this mode (config
  validation error otherwise), every token must carry `exp`, `iss` and `aud`, and the
  algorithms are pinned: no `none`, no HMAC (`HS*`) next to an asymmetric family, no
  HMAC with a `jwks_url`. A token naming an unknown `kid` triggers at most one JWKS
  refetch per 60 s.
- `create_app(engine, rest=RestConfig(...))` overrides the engine's `rest` block.

It is a **reference, not a production auth plane**: no key rotation, revocation,
per-route scopes or cross-replica rate limiting. With the default `mode: none` the
rest of this section applies unchanged.

### ⚠️ Authentication is the deployer's job (ADR-017 / ADR-018)

**The REST app has no authentication by default.** The caller's namespace comes from
the `X-Memspine-Namespace` header (default `"default"`) and is **trusted
verbatim** — whoever can reach the app can read and write every namespace.

- Put the app behind a reverse proxy / auth middleware and override the
  namespace seam:
  ```python
  from memspine.protocols.rest.app import resolve_namespace
  app.dependency_overrides[resolve_namespace] = my_authenticated_namespace
  ```
- REST writes are forced onto the low-trust `rest` channel: a caller cannot
  claim `role="operator"` to escalate trust or dodge the firewall (SEC-C1). The
  role is preserved for provenance only.
- `/sleep`, `/rebuild`, `/audit/taint` are engine-global or cross-cutting —
  keep them on an internal-only network boundary, never exposed to tenant
  callers.
- `/quarantine/{id}/approve` and `/quarantine/{id}/reject` are operator-only:
  they return 403 until you override the operator seam, and the identity it
  returns is the decision's actor (the request body cannot name the reviewer):
  ```python
  from memspine.protocols.rest.app import resolve_operator
  app.dependency_overrides[resolve_operator] = my_authenticated_operator
  ```

Never expose this app to an untrusted network without filling the auth seam.

---

## Swap a backend (config alone)

### Encrypt the SQLite file at rest *(needs `memspine[encrypt]`)*
```yaml
storage:
  path: ./memspine.db
  encryption:
    mode: sqlcipher
    key_env: MEMSPINE_DB_KEY     # the NAME of the variable, never the key itself
```
Every connection (and the schema migration) is opened through SQLCipher and keyed
from `$MEMSPINE_DB_KEY`; a wrong key fails `start()` with `StorageError`, and a
missing driver with `MissingServiceError` naming `[encrypt]`. Only the SQLite file is
encrypted: LanceDB vectors, the Tantivy lexical index (it holds record text), disk
caches and a DBOS system database are separate files; protect them with volume
encryption (ADR-035). A lost key is a lost database.

Every store is a port; you pick the adapter by config, and the event-sourced core
stays the single source of truth. Nothing below changes the API you call — only
which backend the same verbs run against. Each swap is a small config diff.

**Storage: SQLite → PostgreSQL** *(needs `memspine[postgres]`)* — one dialect-neutral
schema serves both (ADR-025). `data_dir` is where the file-backed projections
(LanceDB vectors, Tantivy lexical) live, since a DSN is not a filesystem path.
```yaml
storage:
  backend: postgres
  url: postgresql+psycopg://user:pass@host:5432/memspine   # secrets-resolved
  data_dir: /var/lib/memspine                               # base dir for derived files
read:
  lexical_provider: tantivy   # FTS5 is SQLite-only; use tantivy when hybrid is on with postgres
graph:
  provider: kuzu              # sqlite_adjacency is SQLite-only; use kuzu/ladybug with postgres
```

**Cache: in-process → Redis** *(needs `memspine[redis]`; `valkey` is wire-compatible)* —
also `lmdb` for a persistent single-process cache (`memspine[lmdb]`).
```yaml
cache:
  backend: redis            # memory (default) | lmdb | redis | valkey
  url: redis://localhost:6379/0
  namespace: memspine       # key prefix so instances can share one server
  default_ttl_seconds: 3600
```

**LLM: local → cloud/Bedrock** — LiteLLM routes by the model-id **prefix** (ADR-024).
The special `llamacpp/<path>` prefix runs the in-process llama.cpp adapter
(`memspine[llmlocal]`); every other prefix (`openai/`, `ollama/`, `bedrock/`,
`vertex_ai/`, `azure/`, …) goes through LiteLLM.
```yaml
llm:
  roles:
    extract:                       # local Ollama
      model: ollama/llama3
      api_base: http://localhost:11434
    judge:                         # AWS Bedrock
      model: bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0
      aws_region: us-east-1
```
> **⚠ Breaking (ADR-024):** `llm.roles.<role>` dropped `provider` and `base_url`.
> Migrate `provider: openai` + `base_url: …` → `model: openai/<name>` + `api_base: …`.
> `ollama`/`vllm`/`lmstudio`/OpenAI-compatible all become a `model` prefix + `api_base`.

**Embedding: local → cloud (LiteLLM)** — a cloud embedder's output dimension is not
locally discoverable, so `dim` is **required** when `provider: litellm`.
```yaml
embedding:
  provider: litellm
  model: openai/text-embedding-3-small
  dim: 1536                 # REQUIRED for litellm; the vector store needs it up front
  # api_base / api_key / aws_region as your provider needs
```

**Secrets: env → AWS Secrets Manager** *(needs `memspine[aws]`)* — secrets resolve
*before* the config exists, so this is an **environment variable**, not a config key
(ADR-023). `aws` chains env/`.env` first, then AWS, so local values always win.
```bash
export MEMSPINE_SECRETS_BACKEND=aws     # env (default) | aws
```

**Retrieval: vector-only → hybrid + rerank** — fuse a lexical BM25 leg via RRF, then
optionally cross-encoder rerank. Both default OFF (results stay bit-identical to the
vector-only pipeline until you opt in).
```yaml
read:
  hybrid: true                 # fuse vector + lexical BM25 via RRF (D-25)
  lexical_provider: sqlite_fts5   # sqlite_fts5 (default) | tantivy [tantivy]
  rerank: fastembed            # off (default) | fastembed | flashrank [rerank] | litellm | qwen3 [st]
  rerank_model: cohere/rerank-english-v3.0   # required only when rerank: litellm
```

---

## Config-key reference

Every key of `MemspineConfig` (`src/memspine/config/schema.py`) and its sub-blocks.
`*` marks an open map keyed by name (a role, memory type, or namespace). Dict-valued
keys (`read.scoring`, `*.policies`, `prompts.overrides`) take nested option maps
validated by their own policy/registry. This table is kept honest by
`tests/unit/test_docs_config_surface.py`, which fails if a key here does not exist
in the schema — or if the schema gains a key not documented here.

<!-- CONFIG-KEYS-TABLE:START -->
| Key | Default | Notes |
|-----|---------|-------|
| `profile` | `simple` | Behavior profile; templates set it (base→simple/core/coding/personal/voice/multi_agent/regulated_financial/assistant). |
| `strict_services` | `true` | Missing service hard-fails naming the extra (D-10); `false` starts degraded. |
| `event_log.mode` | `full` | `full` \| `rolling` (bounded window) \| `ephemeral` (nothing persisted — no rebuild/audit; taint rollback falls back to archiving the seed alone, `strict=True` raises) (D-45, #64). |
| `event_log.retention_days` | `30` | Rolling-window retention floor; never prunes past a projector high-water mark. |
| `event_log.compress` | `false` | zstd-compress event payloads at rest. |
| `storage.backend` | `sqlite` | `sqlite` \| `postgres` (ADR-025). |
| `storage.path` | `./memspine.db` | SQLite db file, or `:memory:` for ephemeral. |
| `storage.url` | `null` | Postgres DSN (secrets-resolved); required when `backend: postgres`. |
| `storage.data_dir` | `null` | Base dir for file-backed projections (LanceDB/Tantivy); required for postgres. |
| `storage.encryption.mode` | `none` | `none` \| `sqlcipher` (#52, ADR-035): SQLCipher encryption of the SQLite file, `[encrypt]` extra; sqlite backend only, not `:memory:`. Vectors, the lexical index and disk caches are not covered. |
| `storage.encryption.key_env` | `null` | **Name** of the environment variable holding the SQLCipher key; required with `sqlcipher`. The key is read only from it and never logged. |
| `embedding.provider` | `fastembed` | `fastembed` (ONNX/CPU) \| `hash` (deterministic, tests) \| `static` (model2vec `[static]`) \| `litellm` (cloud). |
| `embedding.model` | `BAAI/bge-small-en-v1.5` | Embedder model id. |
| `embedding.dim` | `null` | **Required** when `provider: litellm` — a cloud embedder's output dim. |
| `embedding.api_base` | `null` | Endpoint override (litellm). |
| `embedding.api_key` | `null` | API key (litellm; secrets-resolved). |
| `embedding.aws_region` | `null` | Bedrock region (litellm). |
| `embedding.request_dimensions` | `false` | litellm only: request exactly `dim` dimensions (Matryoshka models: Cohere embed-v4 256/512/1024/1536, Titan v2, OpenAI v3). |
| `embedding.query_input_type` | `null` | litellm only: input type for retrieval queries (Cohere: `search_query`). |
| `embedding.document_input_type` | `null` | litellm only: input type for stored content (Cohere: `search_document`). |
| `embedding.query_instruction` | `None` | N64: fastembed only. Text prepended to every retrieval query before embedding; documents are unchanged, so no re-index. For the BGE v1.5 models, use `constants.BGE_QUERY_INSTRUCTION` ("Represent this sentence for searching relevant passages: "). fastembed's own `query_embed` does not add it. Query vectors are cached under a key that includes the instruction. |
| `embedding.batch_size` | `32` | G9: most texts per embedding call when `write_messages` embeds its turns up front (Cohere on Bedrock: up to 96). |
| `vector.backend` | `lance` | `lance` is the sole store (ADR-021); `weaviate` reserved (raises). |
| `vector.quantization` | `auto` | `auto` (manifest-driven) \| `none` \| `int8` \| `binary` — E4 native rescore (ADR-020). |
| `vector.namespace_index` | `false` | I1: keep a LanceDB BITMAP scalar index on the vector table's `namespace` column. It is rebuilt every `NAMESPACE_INDEX_EVERY` (500) writes once the table holds `NAMESPACE_INDEX_MIN_ROWS` (1,000) rows, so each user's search prefilter reads that user's rows instead of scanning the column. Results are unchanged; a build failure is logged and stops further attempts. |
| `vector.isolation` | `shared` | I4: `shared` keeps one LanceDB table per embedder with a namespace prefilter. `per_namespace` gives each namespace its own table (`memspine_<embedder>__ns_<hash>`), so a search touches one user's rows only, and `erase_namespace` drops that user's table. Record-id operations (`forget`, erasure checks) search every namespace table. Switching rebuilds the vector projection from the event log. |
| `cache.backend` | `memory` | `memory` \| `lmdb` `[lmdb]` \| `redis` `[redis]` \| `valkey` `[valkey]` (D-09). |
| `cache.path` | `./memspine.cache` | LMDB env directory. |
| `cache.url` | `redis://localhost:6379/0` | Redis/Valkey DSN (secrets-resolved). |
| `cache.namespace` | `memspine` | Key prefix so instances can share one store. |
| `cache.default_ttl_seconds` | `null` | Default TTL when a caller passes none (`null` = no expiry). |
| `cache.max_entries` | *(constant)* | In-memory backend entry cap. |
| `graph.provider` | `auto` | `auto` (GR-1, ADR-064): LadybugDB when the `ladybug` package is installed (`[graph]`), else `sqlite_adjacency`. Explicit: `sqlite_adjacency` \| `ladybug` `[graph]` \| `kuzu` `[kuzu]` (deprecated) \| `neo4j` (reserved) (D-26). |
| `graph.entity_embeddings` | `false` | GR-3: embed entity-node names (GP-2 entity nodes) into the graph store's entity index. LadybugDB keeps them in an `EntityVec` table (`FLOAT[]` column plus its native FTS index), and entity search ranks by exact in-engine cosine; sqlite_adjacency keeps them in node properties. Rows are written once per entity, removed with the node, and cleared on rebuild. |
| `llm.roles.*.model` | `""` | LiteLLM model id; **prefix routes** (`openai/`, `ollama/`, `bedrock/`, `vertex_ai/`, `llamacpp/<path>`) (ADR-024). |
| `llm.roles.*.api_base` | `null` | Local endpoint override (Ollama, vLLM, …). |
| `llm.roles.*.api_key` | `null` | API key (secrets-resolved). |
| `llm.roles.*.aws_region` | `null` | Bedrock region. |
| `llm.roles.*.timeout_seconds` | `60.0` | Per-call timeout. |
| `llm.roles.*.no_think` | `null` | Qwen3 thinking switch: `true` appends ` /no_think` to the last user message; `null` = on for model ids containing `qwen3`, off otherwise. `<think>…</think>` blocks are always stripped from replies. |
| `read.scoring` | `{}` | Options for `ScoringPolicy.bind` (M1 composite). |
| `read.assembly` | `{}` | Options for `AssemblyPolicy.bind` (E2 placement / MMR). |
| `read.rerank` | `off` | `off` \| `fastembed` \| `flashrank` `[rerank]` \| `litellm` \| `qwen3` `[st]` (Qwen3-Reranker, `rerank_model` defaults to `Qwen/Qwen3-Reranker-0.6B`) — E8 cross-encoder (D-51). |
| `read.rerank_model` | `null` | LiteLLM rerank model id; required when `rerank: litellm`. For `fastembed` / `qwen3` it overrides the default local model. |
| `read.static_prefilter` | `false` | E8 cheap lexical-overlap gate (post-vector). |
| `read.static_embedding_prefilter` | `false` | E4 model2vec static-cosine gate `[static]`. |
| `read.hybrid` | `true` | Fuse the lexical BM25 leg via RRF (D-25; on by default since the v0.2 flip, ADR-019); `false` = vector-only. |
| `read.lexical_provider` | `tantivy` | `sqlite_fts5` (FTS5/BM25) \| `tantivy` `[tantivy]`; only when `hybrid` is on. |
| `read.lexical_analyzer` | `default` | N58: Tantivy analyzer for the BM25 leg. `default` (alphanumeric runs, lower-cased) or `english` (adds English stop words and the Snowball English stemmer, so "camping" matches "camp"). The query is tokenized with the same analyzer. `english` keeps its own index (`<db>.tantivy-english`) under the projector name `lexical:english`, so turning it on rebuilds that index from the event log and leaves the default index alone. |
| `read.lexical_dates` | `false` | N30 (write side): the BM25 index also holds each record's date words (the day it was said and the days it names, e.g. "2023-05-07 7 May 2023 Sunday"), so a question naming a date matches lexically. Stored records are unchanged. The index is a variant with its own directory and projector name (`lexical:dates`, or `lexical:english+dates` with the English analyzer), so turning it on rebuilds from the event log. |
| `read.compression` | `{}` | Options for the E5 assembly-stage `CompressionPolicy` (`memspine[compress]`). |
| `read.record_access` | `true` | Append a RETRIEVE event per search (reinforcement stats). `false` makes reads side-effect free (e.g. benchmark isolation). |
| `read.current_state_view` | `false` | C4': render each retrieved keyed fact as `CURRENT (since date)` plus its superseded `HISTORY` (deterministic, from the bi-temporal chain). |
| `read.temporal_leg` | `false` (`true` in `base` and every template that extends it, ADR-062) | C3': fuse a temporal RRF leg: records whose event time lies in an absolute date span named in the query ("7 May 2023", "May 2023", "in 2023"); relative dates are not resolved. |
| `read.rrf_k` | `null` | RRF rank constant k in 1/(k+rank). `null` = 60; Graphiti uses 1 (ablation knob). |
| `read.reply_reserve_tokens` | `0` | ContextPipe: tokens kept free for the reply inside the assembly budget. |
| `read.default_mode` | `auto` | The mode `Engine.read()` uses when the caller passes none (`auto` / `full` / `replay` / `retrieve` / `compose`). The `assistant` template pins `replay`. |
| `read.rerank_date_prefix` | `false` | Hindsight: prefix reranker inputs with `[Date: YYYY-MM-DD]`, so the cross-encoder sees when each candidate happened. |
| `read.rerank_blend` | `None` | N41: None = the reranker's relevance replaces the retrieval score (unchanged). A weight `w` in [0, 1] blends them: `w * rerank + (1 - w) * retrieval`, each min-max normalised over the candidates. |
| `read.rerank_gate` | `None` | N41 confidence gate: when the reranker's best raw score is below this value, it is not confident any candidate answers, and the retrieval order is kept (`rerank_stats()["gated"]` counts these). |
| `read.rerank_context` | `0` | N42: the reranker sees each candidate with this many neighbouring turns of its episodic session on each side (0–3), so a short reply ("yes, the lake") is judged with its question. The stored record and the context shown to the reader are unchanged. |
| `read.skip_rerank_for_ordering` | `false` | Agent Zero: skip the reranker for ordering questions (first / latest / most recent). |
| `decision.provider` | `off` | H24: optional decision provider for calibrated choices among described options without generation. `gliner2` uses the `[ner]` extra (GLiNER2, Apache-2.0). |
| `decision.model` | `fastino/gliner2-base-v1` | H24: the GLiNER2 checkpoint (Hugging Face id; `-large-v1` and `-multi-v1` also exist). |
| `read.planner` | `rules` | H24/G2a: how `read(mode="auto")` picks a mode once full context does not fit. `rules` = deterministic cues; `decision` = the `query_shape` rules settle counts and sets (compose) and ordering questions (replay) first, then the decision provider chooses among `count or list` / `reason or feeling` / `single fact` (compose / replay / retrieve; G24, ADR-052), falling back to the rules on any failure; `llm` = one call to the `plan` LLM role returns a `ReadPlan` (`lookup` / `replay` read by replay, `aggregate` by compose with the plan's up to three subqueries as extra probes, rank-fused with `read.rrf_k`). An unbound role, a failed call or an invalid plan falls back to the rules with a warning. One counted call per auto read. |
| `read.planner_min_confidence` | `0.0` | G2b: with `planner: decision`, a provider choice below this confidence does not route the read (a rule choice is not gated); it keeps the default `replay` (retrieve when no hit is episodic). A choice of unknown confidence (a bare GLiNER2 label) is below any gate above 0. `0.0` = every choice routes. |
| `read.planner_version` | `v1` | #35: with `planner: llm`, `v2` selects the `plan@v2` prompt, which also writes one or two evidence-seeking subqueries for lookup questions ("Is X religious?" → "X church", "X faith"); the lookup read fuses each as an extra vector (+ BM25 under hybrid) leg by RRF. #36: `v3` selects `plan@v3`, which is v2 plus `persons` (the people the question names) and `time_expr` (its date or period words, verbatim); the routed read fuses the persons / time leg (`read.person_time_leg_k`). Still one plan call. `v1`: unchanged. |
| `read.person_time_leg_k` | `10` | #36: with `planner_version: v3`, the most records of the persons / time leg. `time_expr` becomes a date span by the H1 rules (an absolute date, month or year first; else a relative phrase, `read.relative_week` applying, anchored on the namespace's newest record); the leg holds the live records whose `valid_from` lies in the span and/or that are about a planned person (a `person:<name>` tag when the record has any, else its `entity` naming the person as a whole word), both first, then closest to the span's middle, else newest. Fused by RRF into the lookup or compose read; every search gate still applies. |
| `read.completeness_check` | `false` | #38: for reads routed to compose whose plan is `aggregate` or whose question is a list or count question (`query_shape.is_aggregation` / `is_count`), ask the `sufficiency` LLM role (else `plan`) whether the composed context holds every item the question needs (+1 call, prompt `sufficiency`); when it does not, ask for up to three missing-information queries (+1 call, `sufficiency@missing`) and run the compose read once more with them as extra probes. One round at most; an abstained read, a complete verdict, an unbound role or any failure keeps the first read. Other reads make no extra call. Off: byte-identical. |
| `read.compose_replay` | `false` | G2c: compose results get the same ±`replay_window` neighbour expansion as replay mode (nearest first, gated and decorated like replayed turns, within the budget), so routing to compose no longer loses the surrounding turns. |
| `read.aggregate_top_k` | `null` | G11: the `top_k` of a read that `read(mode="auto")` routes to compose (the LLM planner's `aggregate`, the decision planner's or the rules' compose), so list and count questions whose evidence spans sessions pool more candidates; the budget still caps the context. An explicit `mode="compose"` keeps the caller's `top_k`. `null` = unchanged. |
| `read.compose_rewrites` | `false` | P4 (JustMem COMPOSE): `read(mode="compose")` adds up to two answer-free query rewrites from the `query_rewrite` LLM role (`@compose` prompt variant). |
| `read.gap_markers` | `false` | H22 (Mastra): with `render: dated`, prefix records after long silences with `[3 weeks later]`. |
| `read.relevance_filter` | `false` | H17: the `relevance` LLM role labels search candidates relevant / related / irrelevant; only "irrelevant" ones are dropped (Hindsight-style; a yes/no filter loses gold evidence). |
| `read.relevance_safety_net` | `10` | H17: the best-scored candidates always kept, whatever their label. |
| `read.replay_topic_segments` | `false` | H15: replay windows stay inside the hit's topic segment. Boundaries are found by TextTiling-style lexical cohesion within a session (no model), so neighbouring turns from another topic are not replayed. |
| `read.rerank_max_top_k` | `null` | H18: run the configured reranker only when `top_k` is at most this. Reranking pays most when few of many candidates are kept. `null` = always. |
| `read.rerank_keep` | `null` | G5b: with a reranker and `candidate_pool > 1`, keep only the best `rerank_keep` candidates after reranking, before assembly fills the budget, so a wider pool sharpens the ranking instead of doubling the context. `null` = keep the whole pool. A failed rerank keeps the pool. |
| `read.core_terms_leg` | `false` | H13: an extra BM25 leg over the question's core terms (interrogative and function words removed), fused by RRF. Needs the lexical store (`read.hybrid`). |
| `read.metadata_leg` | `false` | C3': fuse a metadata RRF leg: records whose `entity` is named in the query, newest first. |
| `read.order_by_time_for_ordering` | `false` | H16: for ordering questions ("first", "latest", "most recent", …) the assembled records are presented in event-time order rather than score order. |
| `read.render` | `plain` | H5: `dated` prefixes each episodic or semantic record with its event date, `[2023-05-08 Mon] …`, after the stable cache prefix. |
| `read.candidate_pool` | `1` | H11: assembly draws from `candidate_pool × top_k` search candidates, so the token budget rather than a fixed K decides how much evidence enters (LoCoMo: top-10 used ~400 of 4,096 tokens; top-30 lifted multi-hop all-evidence coverage from 5% to 19%). Pair with `read.assembly.relative_floor` (H4: drop candidates below that fraction of the best score; 0 = off). |
| `read.resolve_relative_dates` | `false` | H1: annotate relative-time phrases ("yesterday", "last Friday", "two weeks ago", "last month") in assembled/read records with the absolute date, resolved against each record's event time: `last Friday [= Fri 2023-07-14]`. Rules only, no model. Vague phrases ("recently") are left alone. Stored content is unchanged. On LoCoMo temporal evidence the resolver agrees with the gold date in 126/129 resolvable cases. **Reproducibility (#29, Wave 1):** a mined fact tagged `happened:<date>` is resolved against its `said:` day, and one without a `said:` tag whose `valid_from` is its happened day is not resolved at all, so pre-Wave-1 builds (≤ d6dccc5) rendered e.g. `yesterday [= Thu 2023-06-08]` on it and current builds do not. Runs with this key on (including the `base` template and `read.cards: header`) over happened-tagged facts are therefore not reproducible against pre-Wave-1 builds; with it off, reads are byte-identical (`tests/unit/test_pre_wave1_read_golden.py`). |
| `read.relative_dates_anchored` | `false` | G13: with `resolve_relative_dates`, state week-level phrases relative to the record's own day, LoCoMo's gold convention ("The week before 9 June 2023"): `last week [= the week before 2023-06-09 (2023-06-02..2023-06-08)]` instead of the previous calendar week; `last/this/next weekend` → `the weekend before / of / after <day>`, `last Friday` → `the Friday before <day> (Fri …)`, `two weeks ago` → `two weeks before <day> (≈ …)`, and `a few days ago` → `a few days before <day>` (no span). Months, years and single days are unchanged. Off: byte-identical. |
| `read.relative_week` | `calendar` | #58: with `resolve_relative_dates`, the span of "last/past week" and "next week": `calendar` (the previous / next Monday-to-Sunday week, unchanged) or `preceding_7_days` (the seven days before / after the record's own day, LoCoMo's "the week before <session date>"; the label stays an absolute span). Also used by `consolidation.mine_event_dates` and `read.count_dedupe`. |
| `read.anticipatory_cues` | `false` | C8': a search hit on a cue written with `Engine.add_cues` resolves to its target record; cues are keys, never content (off: invisible). |
| `read.cue_min_trust` | `0.5` | C8': cues below this trust are ignored, so a low-trust source cannot plant cues that redirect retrieval. Cue trust is capped at the target's. |
| `read.query_encoder` | `none` | #61 (ADR-050): read-time query encoder, no LLM. `none` = no encoder (byte-identical reads). `cues` matches the query against stored anticipatory cues (H8 / `add_cues`) by content-word overlap (a cue matches when the query holds at least half its content words) and adds the cued records as one more fused retrieval leg; a cue below `read.cue_min_trust` is ignored and the cued records pass every read gate. Works with `read.anticipatory_cues` off. A trained (Madeleine-style) encoder is future work. |
| `read.topic_timelines` | `false` | H22 (Mnemon): open the volatile context with one dated timeline per topic entity of the retrieved keyed facts: every live fact on that entity (any attribute) plus its superseded history, oldest first, `(until …)` on ended facts. Built at read time from the stored facts (no model, nothing stored). Entries pass the context gates and must be clean (not instruction-flagged, not below `integrity.untrusted_wrap_below`); an entity with a single entry gets none. Applies to `assemble` and `read` (retrieve / replay), not to shared reads. Pair with mined facts (H2): without keyed facts there is nothing to index. |
| `read.timeline_entities` | `3` | H22: how many entities get a timeline, best-scored first. |
| `read.timeline_items` | `8` | H22: the newest entries kept per timeline. |
| `read.standing_instructions` | `false` | H22: right after the pinned persona, a `USER-STATED PREFERENCES` block of the requests and preferences the user stated ("from now on …", "please always …", "I'd prefer …"), newest five, dated. Only `user`-role records at or above `read.standing_min_trust` that pass the context gates; external documents and tool output never qualify. The block is labelled as the user's words, not system instructions. **Risk:** in the trust-horizon threat model a query-only attacker writes as a user (MINJA); leave this off for agents that serve untrusted users. |
| `read.standing_min_trust` | `0.7` | H22: the least (view) trust of a record shown in the standing block. |
| `read.lead_budget_tokens` | `400` | H22: token sub-budget of the lead section (standing block first, then timelines), taken out of the assembly budget and capped at half of it. A block that does not fit is skipped. |
| `read.cards` | `off` | G1b (JustMem cards): `header` opens the volatile context of every read mode and `assemble` with a `FACTS` block of the mined atomic facts relevant to the query, one `[said YYYY-MM-DD] Entity: fact` line each (the date of the fact's earliest source turn; no date without one), fitted as rendered and shown in said order. Facts come from the same hybrid search restricted to `atomic_fact` records and pass every search gate and M12 abstention on their own scores; the rest of the budget goes to the normal read, which then leaves mined facts out (no fact twice) inside its search, before the rerank and `rerank_keep` cut, widening until enough visible records survive. A `full` read leaves out only the facts the header shows. Under `integrity.claims_only_below` a card whose source turn is below the threshold carries the `[CLAIM ...]` marker, and B9 does not put it back as a claim. A header that passed abstention is attached even when the routed read abstains (the read then no longer abstains). Needs `consolidation.mine_facts`. |
| `read.cards_budget_share` | `0.25` | G1b: the share of the budget the cards header may use. With both headers on, `cards_budget_share + profile_budget_share` must be below 1 (config error otherwise). |
| `read.cards_top_k` | `10` | G1b: how many mined facts the header search fetches (best first, kept while they fit). |
| `read.cards_skip_temporal` | `false` | Skip the cards header for date/time questions (`when ...`, `how long ago ...`), so the H1-resolved raw turns answer them; the miner's own dates on cards cost temporal accuracy in the 2026-10-05 smoke test. |
| `read.cards_event_date` | `false` | #29: a card whose mined fact carries a happened date (`consolidation.mine_event_dates`) different from the day it was said renders `[said d1 · happened d2]`. Off: byte-identical. |
| `read.profile_header` | `false` | G3b: after the cards header, a `PROFILE NOTES` block of the H14 profile insights (records `consolidation.reflect_profile` deposits) on the people the query names, or the most relevant insights when none matches, one dated line each. They come from the search restricted to reflective records (every gate applies), and the routed read leaves the shown insights out. Shown only when its own hits pass M12 abstention; names are the capitalised words of the query other than a sentence-initial word (unless repeated), question words and common imperatives (`Tell`, `Give`, `Yes`, `I'm`, ...), matched case-sensitively. |
| `read.profile_budget_share` | `0.15` | G3b: the share of the budget the profile header may use. |
| `read.profile_header_packing` | `false` | #40: the profile header (in place of the G3b block, independent of `profile_header`) packs, following the `[USER PROFILE]` pattern of memory servers, three sections in a fixed order: `Summaries:` (session summaries from consolidation), `Observations:` (the H14 profile insights) and `Related:` (the other best hits for the query; no mined fact while the cards header shows them). Each section comes from its own gated search and is used only when its hits pass M12 abstention; summaries are packed first, then observations, then hits, each best first, while the block fits `profile_header_budget`. Lines are `- [YYYY-MM-DD] text`, escaped and wrapped like any context record, whitespace collapsed, shown oldest first within a section; the header opens with `PROFILE NOTES (`, so stored text cannot forge it. The routed read leaves the packed records out. Off: byte-identical. |
| `read.profile_header_budget` | `300` | #40: the packed profile header's token budget, never more than half the read budget. |
| `read.aggregate_in_replay` | `false` | A1 (ADR-055): a `replay` read (also `auto` when it reads by replay) of a list or count question (`query_shape.is_aggregation` / `is_count`) retrieves `read.aggregate_top_k` candidates (when set; else the caller's `top_k`) through the normal replay path: replay rendering, no compose, no completeness round. The budget still caps the context. Off: byte-identical (golden `tests/unit/golden/routed_read_off.json`). |
| `read.list_cards_only_aggregate` | `false` | A2 (ADR-055): the cards header shows #30 list cards only to list and count questions; other questions get the other cards. With the cards header on, a list card hidden this way also stays out of the read below (the header hides every mined fact). Off: byte-identical. |
| `read.cards_skip_hides_facts` | `false` | ADR-055 addendum: with `read.cards: header` and `read.cards_skip_temporal` (`cards_temporal: skip`), a date question that skips the cards header also gets every mined fact hidden from its read, so it reads raw turns as with mining off. Off: mined facts compete in that question's raw read. |
| `read.cards_only_aggregate` | `false` | ADR-055 addendum: with `read.cards: header`, the cards header (mined facts and #30 list cards) is built only for list and count questions (`query_shape.is_aggregation` / `is_count`). Any other question that is not a date question (`is_temporal`) also gets no mined fact (`atomic_fact`, list cards included) in its routed read, so it reads raw turns only, as with mining off; this holds in every read mode, `full` (and `auto`'s fits-all read) included, and in `assemble`. Precedence: list/count questions first (header as configured, including the date rules when the question is also a date question); date questions keep the `cards_skip_temporal` / `cards_temporal` behaviour unchanged; only the remaining questions are gated. No effect without `cards: header`. Off: byte-identical. |
| `read.temporal_leg_event_dates` | `false` | B2 (ADR-055): with `read.temporal_leg`, a record whose `happened:` date (a mined fact's event date, `consolidation.mine_event_dates`) overlaps the query's date span also enters the temporal leg, at the start of the overlap, so an event said days after it happened is found by its own date. Ordering stays closeness to the span's middle, then `chrono_key`. Off: byte-identical. |
| `read.cards_temporal` | `skip` | B3 (ADR-055): the cards header on date/time questions (`query_shape.is_temporal`). `skip` = unchanged (no cards there when `read.cards_skip_temporal` is on, cards as usual when it is off). `event_dates` = the header is shown with only the cards that carry a `happened:` date, each rendered `[happened d · said d'] Entity: fact` (said omitted when unknown), whatever `cards_skip_temporal` says; list cards and undated facts are left out. Other questions are unaffected. |
| `read.profile_skip_temporal` | `false` | D1 (ADR-055): no profile header, plain (`profile_header`) or packed (`profile_header_packing`), on date/time questions (`query_shape.is_temporal`); mirror of `cards_skip_temporal`. Off: byte-identical. |
| `read.lead_budget_share` | `null` | H12 (ADR-055): cap the total of all lead blocks (cards, graph facts, entity summaries, profile, and the count occurrences reserve) at this share (0-1] of the read budget (after the reply reserve). The occurrences reserve is kept first as far as it fits; then whole blocks leave lowest priority first (`constants.LEAD_BLOCK_DROP_ORDER`: profile, entity summaries, graph facts, cards) until the rest fits, so raw evidence keeps its budget. Applies to `read` and `assemble`. `null`: no cap, byte-identical. |
| `read.raw_turn_floor` | `false` | W19 (ADR-057): derived records (mined facts, cards, summaries) found by the routed search no longer take raw-turn search slots: the search widens by their number (up to 3 times), so every raw turn hit of the read without them stays, with its replay window. On LoCoMo the 4K budget was not binding (about 1.7K used); slots were. |
| `read.verbatim_raw_only` | `false` | F5 (ADR-057): a verbatim question (`query_shape.is_verbatim`: "what did X say about …", "how did X describe …", "exact words") gets no read header and, with `cards: header`, no mined fact: raw turns only. |
| `read.evidence_signal` | `false` | W3 (ADR-057): attach `AssembledContext.evidence` (`core/evidence.py`): top score, spread, distinct days, the asked answer type (date / number / place / name), whether a top-three candidate holds one, and `weak`. Reported only; the context is unchanged. |
| `read.evidence_weak_below` | `null` | W3: with `evidence_signal` (or `cards_when_weak`), a best evidence score below this marks the read weak (search score scale). Null: only a missing answer type makes it weak. |
| `read.cards_when_weak` | `false` | F4 (ADR-057): with `cards: header`, one raw-turns-only search first; strong raw evidence (W3 signal not weak) reads raw turns only (no cards, no mined facts), weak evidence keeps the header. Calibrate `evidence_weak_below` on one dataset, check on another. |
| `read.evidence_first` | `false` | T10 (ADR-057): in a replay read, the best search hit's window opens the turns (time order inside), then the other windows in time order. |
| `read.present_order` | `relevance` | N01 (ADR-057): presentation order of the retrieved records: `relevance` (score), `recorded` (time order), `recorded_if_shared_key` (time order only when two records share entity + attribute, so the newer value reads last). |
| `read.focused_excerpt` | `false` | N13 (ADR-057): a retrieved record of six or more lines is shown as its two best query-matching lines, each with its next line, cuts marked "…"; never for a verbatim question; stored content unchanged. |
| `read.temporal_relative` | `false` | F2 (ADR-057): with `temporal_leg`, a question without an absolute date resolves its first relative phrase ("last week", "yesterday", "two months ago") against the engine clock, using `relative_week`. |
| `read.temporal_infer_year` | `false` | N45: with `read.temporal_leg`, a date with no year ("on 7 May", "May 7th", "in March") gets the latest year that puts it on or before the namespace's newest record (or the as-of time). Month names must be capitalised and next to a day number or after a preposition, so "may I" never matches. Off: such dates name no span. |
| `read.temporal_rank` | `midpoint` | N61: order of in-span records in the temporal leg. `midpoint` (closest to the span's middle) or `overlap` (most content words shared with the question first, then midpoint). |
| `read.temporal_soft` | `false` | N44: when fewer than `fetch_k` records fall in the span, the temporal leg is filled with the nearest records outside it, within one span length (at least `TEMPORAL_SOFT_MARGIN_DAYS` = 3 days). In-span records are never displaced. |
| `read.temporal_leg_mentions` | `false` | N30: with `read.temporal_leg`, a turn whose text names a date ("last weekend", "yesterday", "on 7 May"), resolved against the turn's own time, also enters the leg when that date overlaps the question's span. Read-side only, nothing stored changes; resolved spans are cached per record (`MENTION_CACHE_MAX`). |
| `read.standing_patterns` | `narrow` | N18 (ADR-057): the H22 standing-block cues. `wide` adds first-person evaluative forms ("I avoid / can't stand / am allergic to", "I'm vegetarian", "my favourite"); PrefEval explicit preferences 11.3% → 83.1%. |
| `read.session_cap` | `null` | W10 (ADR-059): at most this many raw-turn hits from any one session (episodic gap-split sessions, else the calendar day), drawn from a 4× wider search, so evidence spread over many sessions reaches the read; in replay each hit brings its window. |
| `read.concentration_filter` | `false` | N21 (ADR-059, RAGDefender): a dense near-duplicate cluster among the search candidates (3+ records sharing `concentration_jaccard` of their content words) collapses to its best member, tagged `concentrated:<n>`. PoisonedRAG planted sets 257 / 300 clustered vs LoCoMo windows 8 / 581. |
| `read.concentration_jaccard` | `0.2` | N21: the content-word Jaccard of a near-duplicate. |
| `read.prf_expansion` | `false` | N03 (ADR-059): pseudo-relevance feedback — up to five content words shared by two or more of the first-round top five hits and absent from the question join the search as one more RRF probe. |
| `read.subject_leg` | `false` | W8 (ADR-059): an RRF leg of the turns of every speaker the question names (`speaker:<name>` tags, `memories.episodic.policies.subject_tagging`), ranked by word overlap with the question. A boost, never a filter. |
| `read.role_aware` | `false` | W11 (ADR-059): assistant turns that recommend something are tagged `recommendation` at `write_messages`; a question about what the assistant said ("what did you recommend") gets an RRF leg of the assistant's turns, recommendations first. |
| `read.profile_scope_gate` | `false` | W9 (ADR-059, OP-Bench): the profile header only for questions about the asker, a choice they face or a named person (`is_personal` or a name); general-knowledge questions get none. Standing instructions are unaffected. |
| `read.multi_intent_split` | `false` | G34 (ADR-059): a multi-part question is split on discourse markers ("…, and where did he move?", "also", "additionally") and each part joins the search as an RRF probe. |
| `read.second_round` | `false` | N04 (ADR-059): when the first search's W3 signal is weak (with `evidence_weak_below`), the names and dates its top three hits mention and the question lacks seed a second search over a doubled pool. |
| `read.cluster_expand` | `false` | N06 (ADR-059): the embedding neighbourhoods of the top two hits (across sessions) join the search as RRF legs — read-time topic clusters (two embeds and two vector queries per read). |
| `read.sentence_leg` | `false` | N05 (ADR-059): an RRF leg ranking records by their single best-matching sentence (shared content words / √length), so one strong sentence in a long turn is not diluted. No model. |
| `read.rerank_instruction` | `null` | N12 (ADR-059): the task instruction an instruction-conditioned reranker (`rerank: qwen3`) judges with; null keeps its memory default. |
| `read.evidence_line` | `false` | W3 step 2 (ADR-059): with `evidence_signal`, a weak read opens its volatile part with a one-line note that memory holds no clear record answering the question. |
| `read.novelty_exclusions` | `false` | G32 (ADR-059): a request for something new ("a book I haven't read") gets a header of what memory says the asker already likes, does or has (likes, activities, favourite_*, pets facts and list cards). |
| `read.profile_slots_header` | `false` | W5 (ADR-059): the Λ-profile block — the current keyed state facts (home, origin, job, favourites, attitudes …) of each person the question names, else of `user`, within `profile_budget_share`. |
| `read.profile_sensitive` | `false` | W5 / W16 (ADR-059): include slots tagged `sensitive:*` in the profile block (default: never). |
| `read.span_line` | `false` | N10 (ADR-059): a duration question ("how long after …", "how many weeks between …") gets one engine-computed line from the two best-matching dated turns: "A (date) -> B (date): N days (about W weeks, M months)". |
| `read.leg_weights` | `{}` | N16 (ADR-059, LaMP RSPG): per-leg RRF weights (`{"vector": w, "lexical": w, "extra": w}`; missing keys weigh 1) for this deployment's task. Empty: unchanged. |
| `read.leg_weights_by_shape` | `{}` | N62: leg weights by question shape (`temporal`, `count`, `ordering`, `inference`, `plain`; `query_shape.question_shape`), applied over `read.leg_weights`. Named legs are `vector`, `lexical`, `temporal` and `entity`; every other extra leg is `extra`. Example: `{temporal: {temporal: 2.0}}`. |
| `read.fusion` | `rrf` | N52: how the retrieval legs are fused. `rrf` (reciprocal rank) or `minmax`: each leg's scores min-max normalised to [0, 1] and summed (weighted by `leg_weights`); a rule leg whose scores all tie is normalised by rank. |
| `read.cohesion_leg` | `false` | N43: an RRF leg of records said within `COHESION_WINDOW_MINUTES` (5) of the first-pass top hits (`ANCHOR_TOP` = 3 from the vector leg, then the lexical leg), nearest first. |
| `read.entity_expand_leg` | `false` | N32 + N53: an RRF leg of records naming the proper nouns and years the first-pass top hits name (speakers excluded), most shared names first. A name found in more than `ENTITY_EXPAND_MAX_SHARE` (5%) of the records is too common and is dropped. |
| `read.maxsim_leg` | `false` | N60: an RRF leg of the first-pass candidates with two or more sentences, ranked by their best sentence's cosine with the query, so one matching sentence in a long turn is not diluted. Candidate sentences are embedded at read and cached per record (`MAXSIM_CACHE_MAX`); no re-index. Erasure purges the cache. |
| `read.graph_node_search` | `false` | GR-6: an RRF leg (`graph_nodes`) of the records that mention the entity nodes best matching the question (cosine on embedded names + text match, fused in the graph store), best entity first. Needs `graph.entity_embeddings` and associative entity nodes. |
| `read.view_tag_leg` | `false` | G-16: an RRF leg (`view_tags`) of records whose location / topic / person view tags (`loc:` / `topic:` / `person:`, from `consolidation.mine_multiview`) share words with the question. Ranked by most shared words, then location over topic over person. |
| `read.session_digest` | `false` | N31: a read header (`SESSION DIGESTS`) with one line per episodic session of the first `SESSION_DIGEST_HITS` (5) hits, up to `SESSION_DIGEST_SESSIONS` (3): the `SESSION_DIGEST_SENTENCES` (2) sentences sharing most content words with the question, within `SESSION_DIGEST_SHARE` (10%) of the budget. Unlike other headers it does not hide the quoted turns from the read. |
| `read.facts_to_sources` | `false` | N54: a mined fact (`atomic_fact`) in the hits gives its rank and score to its live source turns (its parents); a turn already listed is not repeated, and a fact whose sources are gone keeps its slot. |
| `read.type_quotas` | `{}` | N55: the most records of each memory type a read keeps, e.g. `{semantic: 3}`; types not listed are uncapped. |
| `read.recent_exchanges` | `0` | C2: a read header (`RECENT CONVERSATION`) with the namespace's last N live episodic turns, oldest first, within `RECENT_SHARE` (15%) of the budget. A turn identical to the question (the in-flight message) is left out, and the turns stay readable in the retrieved part. |
| `read.leg_min_scores` | `{}` | C6: per-leg score floors applied before fusion, e.g. `{vector: 0.3, lexical: 1.0}` (vector = cosine, lexical = BM25). Weak hits never enter the fusion. |
| `read.section_captions` | `false` | C7: when read headers lead the context, the retrieved part gets its own caption (`RETRIEVED_CAPTION`), so the reader sees which part is which. |
| `read.short_query_lexical_weight` | `None` | N33: the BM25 leg's RRF weight for a question of at most `SHORT_QUERY_WORDS` (5) content words. |
| `read.entity_leg` | `false` | N59: an RRF leg of raw turns that name the question's proper nouns and years, beyond the conversation's speakers. Ranked by names matched, then content words shared, then time order. |
| `read.statement_probe` | `false` | N63: the question rewritten as a statement by rules ("When did Ana go camping?" becomes "Ana go camping") joins the search as one more probe (vector + BM25). |
| `read.speaker_probe` | `false` | N40: when the question names a speaker, "Name: <core terms>" (the stored-turn form) joins the search as a vector probe. |
| `read.recency_leg` | `false` | N16 (ADR-059): a recency-only leg (newest records first) joins the fusion as an extra leg, for tasks where the latest behaviour matters most. |
| `read.count_timeline` | `false` | E3: for count questions (`how many times …`, `how many <things> …`, `how often …`; not durations such as `how many days ago`, see `query_shape.is_count`), every read mode and `assemble` lead the volatile context with an `Occurrences (dated):` block: one `- [said YYYY-MM-DD] <mention>` line per distinct occurrence of the counted event among the episodic records the read retrieved, oldest first. The event is the query's core terms without names and count words; a record mentions it when it shares at least half of them. Same-day mentions in one session, or same-day mentions sharing at least half their words, count once. Mined facts, lead blocks and wrapped (instruction-flagged or untrusted) records are left out; the mentions stay in the context. Off: byte-identical. |
| `read.count_budget_share` | `0.1` | E3: the share of the budget kept for the occurrences block (the read gets the rest; lines are kept oldest first while the block fits). Counts toward the header-share check: active `cards_budget_share + profile_budget_share + count_budget_share` must be below 1. |
| `read.count_dedupe` | `false` | #60: with `count_timeline`, a mention's event day is the single day its relative phrase names ("yesterday", "last Friday"), else the day it was said; two mentions on the same event day with at least half their words shared are one occurrence, even when said on different days. Off: byte-identical. |
| `read.graph_leg` | `false` | GP-3 (#14): fuse a graph leg into the RRF ranking. Seeds are the entity nodes the query names (its 1-4-word n-grams matched against the namespace's entity names; the decision provider's optional `entities` hook, GLiNER2, adds names when configured, never required), else the entities of the best 3 hits of the other legs. A walk of `graph_depth` entity hops yields fact records, each followed by its source turns; the first `graph_leg_k` form the leg, and its hits pass every search gate. Needs `memories.associative` with `policies.entity_nodes`. Off: byte-identical (golden `tests/unit/golden/graph_leg_off_read.json`). |
| `read.graph_depth` | `2` | GP-3: walk depth in entity hops (entity -> record -> entity is one), 1-3. |
| `read.graph_leg_k` | `10` | GP-3: the most records (facts and their source turns) the graph leg contributes. |
| `read.graph_min_trust` | `0.25` | GP-10 (#16): a graph walk (the leg and the facts block) never enters a record below this trust, nor a quarantined, erased or taint-rolled-back one; a refused node is a dead end. Default: the firewall's quarantine threshold. |
| `read.cards_include_edges` | `false` | GP-5 (#15): after the cards header, a `GRAPH FACTS` block of the edge facts (`rel:`-tagged records) the graph walk reaches from the entities the query names, live and superseded, one `[2023-05-01 → present] Melanie read "X" (sources: 2)` line each (a superseded fact shows its end date; `sources` counts the live episodes stating it). Shares `cards_budget_share` with the cards header and counts toward the header-share check; the shown facts are left out of the read below. Off: byte-identical. |
| `read.graph_rerank` | `off` | #22 (GP-4/KB-4/GR-19): rerank the gated candidates, before the `top_k` cut, by graph proximity to the graph leg's seeds (the entities the query names, else those of the best hits): `distance` = 1 / entity hops of the seed walk, `ppr` = local push-PPR restarted at the seeds over their `subgraph()` (normalised to the best record). Facts restated by more episodes (`edge_source:` provenance, GR-9) get an episode-mentions boost `log(1+n)/log(1+n_max)`. Same GP-10 trust caps as the graph leg; needs associative memory with `entity_nodes` for the graph part. `off` = byte-identical. |
| `read.graph_rerank_weight` | `0.2` | #22: the weight `w` of each `graph_rerank` boost: relevance `r` becomes `r + w * b * (1 - r)` for a boost `b` in [0, 1], so an unboosted candidate keeps its score. `0.0` = no effect. |
| `read.causal_walk` | `off` | W12 (ADR-061): before the `top_k` cut, walk the `because` (effect -> cause) and `reply_to` (reply -> answered message) LINKs from the best 5 candidates up to `causal_walk_hops` hops; a reached record joins, or is lifted to, `seed relevance x 0.9^hops`, then passes every search gate (status, quarantine, admission, `graph_min_trust`, date filter). `why` = only questions asking for a cause ("Why ...", "How come ...", "What made ..."); `always` = every read. Needs associative memory; edges come from `memories.associative.policies.rule_edges` and `write(reply_to=)`. `off` = byte-identical. |
| `read.causal_walk_hops` | `2` | W12: the most hops `causal_walk` follows (1-3). |
| `read.reply_links` | `false` | G27 (ADR-061): in a replay read, a turn written with `reply_to` also shows the message it answers (gated like a replayed neighbour, same namespace only, within the budget). Off: byte-identical. |
| `read.entity_summaries` | `false` | GP-6 (#17): after the cards header and the graph facts, an `ABOUT` block with one `About <Name>: …` line per entity the query names (the graph leg's seeds) that has a live entity summary (`memories.associative.policies.entity_summaries`). Summaries pass the graph admission gate (status, quarantine, `graph_min_trust`, integrity) and the context wrappers. Shares `cards_budget_share` and counts toward the header-share check; the shown summaries are left out of the read below. Off: byte-identical. |
| `read.graph_communities` | `false` | GP-9 (#23): community summaries (`reorganize` parents) are read only when a seed entity of the graph leg is a member of the community, that is, when one of the records the entity mentions is a member (communities are built over association edges, `mentions` excluded). Every other community summary is dropped from search results; with `graph_leg` on, the admitted ones join the graph leg. Seeds as for `graph_leg` (query names, else the entities of the best 3 hits). Off: byte-identical. |
| `firewall.enabled` | `true` | `false` keeps trust scoring but disables flagging, anomaly checks and quarantine: the N1 ablation arm only. |
| `firewall.skip_message_roles` | `[]` | H21: `write_messages` never deposits turns with these roles (e.g. `["system", "tool"]`). |
| `firewall.skip_injected_recall` | `false` | H21: never re-deposit a turn carrying memspine's own assembly markers (recalled memory echoed back into the conversation). |
| `firewall.tag_assistant_claims` | `false` | H21: tag assistant turns `assistant_claim`, a proposal rather than an observed fact. |
| `firewall.redact_secrets` | `false` | Replace cloud keys, VCS/chat tokens, JWTs, private keys, `key=value` credentials and emails with `[REDACTED:<kind>]` at write, in content, entity, attribute and tags. |
| `firewall.pii` | `off` | PII pack (phone, Luhn-checked card, US SSN, mod-97 IBAN, IPv4/IPv6) over content, entity, attribute and tags: `redact` masks each match as `[REDACTED:<kind>]`; `tag` keeps the text, adds `pii:<kind>` tags and raises `pii_tier` to at least `high`; `off` does neither. |
| `firewall.pii_extended` | `false` | N23 (ADR-058): with `pii` on, the cue-anchored kinds without a checksum: card numbers after a card cue, bank accounts, passports, driving licences, licence plates, street addresses (only the value is masked). |
| `firewall.signals.instruction` | `true` | W2 (ADR-058): the base instruction-framing patterns. `false` is an ablation arm. |
| `firewall.signals.anomaly` | `true` | W2: the embedding-outlier signal. `false` is an ablation arm. |
| `firewall.signals.minja_bridge` | `true` | W2: the MINJA bridge-prefix signal. `false` is an ablation arm. |
| `firewall.signals.instruction_extended` | `false` | W2: the base patterns plus ASB / MEM-INV framings ("must strictly adhere", "do not use other tools", "Answer: task complete", "ignore the previous sentence", overrides, prompt reveals, chat-template tokens, "SYSTEM UPDATE"). ASB framed attacks 0 → 100%; PrefEval preferences 0 / 1,018 false flags. |
| `firewall.signals.semantic_risk` | `false` | N20 (ADR-058): flags self-claimed authority ("this information has been verified") and answer binding ("the correct answer is", "whenever anyone asks"); quarantines untrusted origins only. |
| `firewall.signals.query_anomaly` | `false` | N22 (ADR-058): a write whose best cosine to the namespace's last 64 query vectors exceeds mean + `query_anomaly_kappa` σ of recent write scores (after 20) is anomalous. In memory. |
| `firewall.signals.query_anomaly_kappa` | `3.0` | N22: the z-score threshold. |
| `firewall.sensitive_topics` | `false` | W16 (ADR-059): tag GDPR art. 9-style topics (health, religion, sexual orientation, politics, ethnicity, legal, financial hardship; `core/sensitive.py`) as `sensitive:<topic>` and raise `pii_tier` to at least `high`. The text is kept. |
| `integrity.corroboration_roots` | `false` | W13 (ADR-059): corroboration counts independent lineage roots: a corroborating write whose `source.parents` lineage shares a root with the quarantined record or an earlier corroborator does not count. |
| `write(..., reply_to=)`, `write_messages` turn `reply_to` | — | G27 (ADR-061): the record a turn answers (record id, or in `write_messages` the index of an earlier message of the call). Stored as a `reply_to:<id>` tag; with associative memory a `reply_to` LINK too. A missing or foreign target is refused like a missing record. |
| `Engine.verify_forget(probe=...)` | — | W14 (ADR-059): erasure proven on recall — the erased text, searched for, must bring back no record holding it or a near-duplicate (`residual_recall`). |
| `read(as_of=...)`, `assemble(as_of=...)`, REST `/assemble` `as_of` | — | W7 (ADR-059): valid-time view — records begun by then; a fact superseded after `as_of` counts as current; relative phrases resolve against it. Add `recorded_before` for a known-at read. |
| `firewall.max_content_chars` | `null` | Quarantine non-privileged writes longer than this (size anomaly). |
| `firewall.protected_keys` | `[]` | Fact keys (`entity` or `entity.attribute`) only operator/system sources may write; others are quarantined. |
| `integrity.principal_reputation` | `false` | B7: multiply a principal's later write trust by min(1, 2 × Beta mean of its good vs bad records), where bad = quarantined, or the seed of a taint rollback. It only lowers trust, so the trust horizon still holds. `Engine.principal_reputation(p)` returns the factor. |
| `integrity.enabled` | `false` | Trust-horizon invariant (THI) for shared memory (opt-in); off = pre-MTI behaviour, byte-identical. |
| `integrity.attenuation` | `product` | `product` (trust × κ per grant hop) \| `min` (min(trust, κ)). |
| `integrity.kappa` | `0.5` | Default per-grant attenuation κ ∈ (0, 1]. |
| `integrity.edge_kappa` | `{}` | Per-edge κ overrides keyed `"grantor->grantee"`. |
| `integrity.derivation_decay` | `1.0` | Extra factor applied to every `derived_from` deposit (1.0 = none). |
| `integrity.verification_bonus` | `1.0` | Baseline emulation only (MAP-Graph-style bonus, 1.0–2.0): >1 lets derived trust exceed its parents and breaks the invariant on purpose. |
| `integrity.admission_threshold` | `0.0` | Records whose view trust is below θ are never returned by `search`/`shared_search`. |
| `integrity.trust_weighted_ranking` | `true` | Rank by composite score × view trust. |
| `integrity.principal_bound_corroboration` | `true` | Promotion needs corroborators with distinct `source.principal`s (never the held record's). |
| `integrity.merge_reinforcement_gate` | `true` | A less-trusted duplicate never reinforces the record it merges into. |
| `integrity.implicit_parents` | `off` | B0: `turn` / `session`: records each session's reads and makes them implicit parents of its next write (write trust ≤ lowest view trust read), so omitting `derived_from` cannot launder. `turn` keeps the parents for every write until the session's next read; `session` keeps them until `end_session()`. Reads with no `session_id` share one fail-closed ledger per namespace that only `end_session(ns)` clears. |
| `integrity.untrusted_wrap_below` | `0.0` | B6: `assemble` renders records whose view trust is below this inside an untrusted-data wrapper (data, not instructions). 0.0 = off. |
| `integrity.claims_only_below` | `0.0` | B9 facts-only: a context record whose view trust is below this never enters a context window as raw text. Each live, unflagged atomic fact mined from it (H2) stands in once, prefixed `[CLAIM from a low-trust source, unverified]`; with none it is left out. Mined facts, the persona and lead blocks are exempt. Applies to assemble, retrieve, replay, full and compose reads. Needs `integrity.enabled`. Trades recall on low-trust sources for containment of their framing. `0.0` = off. |
| `integrity.live_reevaluation` | `false` | B4': search/shared_search re-check each candidate's trust against its current parents (ancestor quarantine/rollback propagates, and so does grant revocation: a parent behind a revoked grant counts 0, see ADR-029 edge cases; radii only shrink). |
| `workers.runner` | `inline` | `inline` \| `dbos` `[dbos]` \| `taskiq` `[taskiq]` (D-16). |
| `workers.broker_url` | `redis://localhost:6379/0` | taskiq broker endpoint (ignored by other runners). |
| `workers.dbos_system_database_url` | `null` | DBOS system db; `null` derives a SQLite file beside `storage.path`. |
| `retention.classes` | `[]` | #48: retention classes, checked in order, first match wins: `{namespace: <glob>, memory_type: <type or null>, ttl_days: <days>}`. A sleep-cycle stage (`retention_expire`, run first, only when this list is non-empty) hard-forgets records whose `recorded_at` is older than the TTL through the ordinary forget path (cascading to derived records). Soft-forgotten records past the TTL are hard-erased too (a soft forget keeps the content). Records whose type's `retention` policy refuses deletion (legal hold, `regulated` PII) are kept, and so are records with such a descendant. Empty: nothing expires and the sleep cycle is unchanged. `Engine.expire_retention(now=None)` runs it on demand. |
| `audit.reads` | `false` | #49: every `search` / `assemble` / `read` / `retrieve` / `shared_search` / `export` appends one `memory.read_audit` event: principal (`event.actor`, from the REST auth binding or `principal_scope`, else `anonymous`), namespace, returned record ids, purpose and time. A verb that calls another public read verb audits once. Hash-chained (see `audit.actions`). No projector reads it. |
| `audit.actions` | `false` | #49: `forget` (with `actor=` / `reason=`), `correct`, retention expiry, `export`, `erase_subject`, `erase_namespace`, `approve_quarantined`, `reject_quarantined`, `feedback` (the signal, never the note), `grant` and `revoke` append a `memory.audit` event with the actor, principal, reason and record ids. Both audit kinds share one SHA-256 hash chain (`payload.chain.prev` / `.hash`); `Engine.verify_audit_chain()` / `audit_chain_ok()` validate it (a rolling log anchors at its oldest surviving audit event). |
| `consent.enforce` | `false` | #50 purpose limitation: records carry purposes (`write(..., purposes=[...])`, stored as `consent_tags`; `*` = any purpose) and reads pass `purpose=`. On, a read returns only records whose purposes include the read's purpose; a read without a purpose sees only untagged and `*` records. Applied in the search gates and to every returned list or assembled context. |
| `consent.untagged` | `allow` | #50: with `enforce`, whether records with no purpose are visible to every read (`allow`) or to none (`deny`). |
| `consent.remote_llm_max_tier` | `null` | #50 remote-LLM gate: `none` \| `low` \| `high` \| `regulated`. Every LLM role bound to a remote provider is wrapped so that, before each call, the text of any record whose `pii_tier` is above this tier (content, archived versions, the 400-char relevance-note prefix, the text without its leading entity name or fact key, in any whitespace, case or JSON escaping) is replaced by `[WITHHELD: above the remote-LLM PII tier]`; the `sufficiency`, `verify_answer` and relevance prompts also replace a context record above the tier (by its own tier, which a derived record or lead block inherits) before rendering. Local = `llamacpp/…`, an `ollama/…` model without `api_base`, or an `api_base` on `localhost`/`127.0.0.1`/`::1`/`*.local`/`consent.local_hosts`. Textual gate: a paraphrase of the content is not caught. `null`: off. |
| `consent.local_hosts` | `[]` | #50: extra `api_base` host names that count as local for the remote-LLM gate. |
| `rest.auth.mode` | `none` | #51 reference auth middleware (ADR-041; not a production auth plane): `none` (unauthenticated, v0.1) \| `api_key` \| `oidc_jwt` (needs `pyjwt`). Binds a principal and its namespaces to each request: another namespace gets 403; `/sleep`, `/rebuild`, `/export`, `/quarantine…` need the admin role. |
| `rest.auth.api_keys` | `[]` | #51 `api_key` mode: list of `{key_env: <ENV VAR>, principal, namespaces: [<glob>], admin: false}` (or `key:` instead of `key_env`). Sent as `Authorization: Bearer <key>` or `X-API-Key`. Only SHA-256 digests are kept; keys are never logged or echoed. |
| `rest.auth.jwt.issuer` | `null` | #51 `oidc_jwt`: required `iss`; must be set in `oidc_jwt` mode (config validation error otherwise). |
| `rest.auth.jwt.audience` | `null` | #51 `oidc_jwt`: required `aud`; must be set in `oidc_jwt` mode (config validation error otherwise). |
| `rest.auth.jwt.algorithms` | `["RS256"]` | #51 `oidc_jwt`: accepted signing algorithms; never `none`, never `HS*` mixed with another family or with `jwks_url`. Tokens must carry `exp`, `iss` and `aud`. |
| `rest.auth.jwt.jwks_url` | `null` | #51 `oidc_jwt`: JWKS endpoint for the signing keys (PyJWT `PyJWKClient`). |
| `rest.auth.jwt.key_env` | `null` | #51 `oidc_jwt`: env var holding a PEM public key or HMAC secret (when no `jwks_url`). |
| `rest.auth.jwt.principal_claim` | `sub` | #51 `oidc_jwt`: claim naming the principal. |
| `rest.auth.jwt.namespaces_claim` | `memspine_namespaces` | #51 `oidc_jwt`: claim listing the allowed namespace globs (list or space-separated). |
| `rest.auth.jwt.roles_claim` | `roles` | #51 `oidc_jwt`: claim listing roles (list or space-separated). |
| `rest.auth.jwt.admin_role` | `memspine-admin` | #51 `oidc_jwt`: the role that unlocks the admin routes. |
| `rest.rate_limit` | `null` | #51: `{requests_per_second, burst: 10}` in-memory token bucket per principal (per client address without auth); over the limit → 429. One process only. With auth on, it also sizes the per-address bucket of failed authentications (default burst 10, 0.1/s). |
| `workers.sleep_interval_seconds` | `null` | D1: when set (seconds), the engine runs the full sleep cycle on that interval autonomously; `null` keeps v0.1 behavior (cycle runs only on `Engine.sleep()`). |
| `prompts.overrides` | `{}` | Per-prompt overrides (body/system/format/version/output_model/token_budget) (D-43). |
| `prompts.partials` | `{}` | Override fragments for shared Jinja `{% include %}` partials (anti-injection block, output footer); `<name>` → replacement text, consulted before the shipped `_partials/` dir (B1). |
| `prompts.selection` | `{}` | Per-role default scenario selectors: `<role>` → map of optional `memory_type`/`condition`, merged into every `select(role)` query so a deployment can pin a prompt variant without code (B2). Shipped `chat` conditions: `dated` (H12, `chat@dated`), `dated2` (G12, `chat@dated2`: `chat@dated` plus "a line's leading date is when it was said, `[= …]` is when the event happened; answer *when* questions with the happened date"), `infer` (G10, `chat@infer`), `dated3` (#34, `chat@dated3`: brief reasoning then a final `Answer:` line, which `Engine.final_answer()` extracts; quote the specific detail; dates in the granularity asked; "a date in brackets is when it was said; the event may be earlier"; merge repeated mentions before counting; "Not mentioned" only when nothing bears on the question). `temporal` (C1/H11, `chat@temporal`: `chat@dated` plus "infer a date not stated outright from the line's date and any relative phrase; answer dates as DD Month YYYY"), `inference` (C1/H11, `chat@inference`: `chat@dated` with the refusal replaced by "give the most plausible answer and say 'likely'"). C2: the entry `chat_by_shape` (default unset = unchanged) maps a question shape (`temporal`: `query_shape.is_temporal`; `inference`: `is_inference`, a "would / likely / might / could ...?" question; temporal wins) to the `chat` condition `Engine.chat_messages` uses for that question, e.g. `chat_by_shape: {temporal: temporal, inference: inference}` (the harness `--qa-prompt routed` mapping) or `{temporal: infer, inference: infer}`; other questions keep the `chat` selection, a per-call `condition` wins, and an unknown shape or condition is a config error. `plan` condition `v2` is selected by `read.planner_version`; `extract` conditions `session3` / `dates` by the consolidation options. |
| `memories.*.enabled` | `false` | Enable a memory type (`working`/`episodic`/`semantic`/…); C1b auto-enables prerequisites. |
| `memories.*.policies` | `{}` | Per-type policy overrides (conflict/dedup/trust/entity_extraction/page_size/…). `semantic.policies.extract_graph` (`{max_rounds, min_confidence}`) opts into C2 graphiti-style writes: with an `extract_edges` LLM role, the background `extract_graph` sleep stage writes edge facts + `asserted` links. `semantic.policies.write_pipeline: graph` opts into the C3 synchronous variant — edges extracted at write time and written through the M4/M5 ladder (ADR-026). `episodic.policies.sessions.passive_after` (seconds or `"30d"`; default off) opts into the #53 session lifecycle: idle sessions go PASSIVE, out of default reads (ADR-037). |
| `namespaces.*.policies` | `{}` | Per-namespace policy overrides (D-14). |
<!-- CONFIG-KEYS-TABLE:END -->

Secrets backend selection is **not** a config key: `MEMSPINE_SECRETS_BACKEND`
(`env` default \| `aws`) is an environment variable, because secrets resolve before
`MemspineConfig` is built (ADR-023).

### Policy options (nested under the dict-valued keys)

The dict-valued keys above take the options of a bindable policy
(`src/memspine/core/policies/`). These are the opt-in read and write options that the
table above does not list one by one. All are off by default, so `profile="simple"` is
unchanged; `tests/unit/test_simple_profile_golden.py` pins their defaults.

| Option path | Default | Notes |
|-----|---------|-------|
| `memories.semantic.policies.conflict.contest_ties` | `false` | H9: when neither event time nor trust decides between two values of one fact key, keep both (verdict CONTEST). The contender is tagged `disputed`; the current fact stays the single active one. |
| `memories.semantic.policies.conflict.contest_window_seconds` | `0.0` | H9: two event times at most this many seconds apart count as a tie. `0.0` = only identical event times. |
| `memories.semantic.policies.conflict.contest_trust_margin` | `0.05` | H9: two trusts at most this far apart count as a tie. |
| `memories.semantic.policies.write.reflexion` | `true` | #32 (GR-16): run the extra edge-extraction rounds (`extract_graph.max_rounds` > 1) of the C2/C3 graph write paths. `false` makes every extraction a single call (one call per source fewer for each extra round), the ablation Graphiti ran before removing reflexion. With the default `max_rounds: 1` there is no extra round, so nothing changes. |
| `memories.semantic.policies.conflict.contest_lower_trust` | `false` | A same-key write less trusted than the current fact (but within `trust_margin`) CONTESTs it instead of superseding or retracting it, so a lower-trust source cannot archive a higher-trust fact (ADR-029, supersession). |
| `memories.semantic.policies.conflict.interval_order` | `false` | #19 interval arithmetic: a superseded or retracted fact gets `invalid_at` (when it stopped being true in the world) = the next statement's `valid_from`. An older-arriving statement that contradicts the current fact is stored as history ending where the next statement on its key begins, and the history entry it lands inside is closed at its start, so facts arriving out of order end with the correct current fact and non-overlapping intervals. A statement with the same key and endpoints (`dst:` tag) as the current fact is merged as a duplicate, not a contradiction. Off = the plain R4 backfill, and no `invalid_at` is ever written. |
| `memories.semantic.policies.extract_graph.granularity` | `record` | #20: `session` sends each consolidated session to the `extract_edges` role in ONE call (`extract_edges@session`: turns numbered `[n] [YYYY-MM-DD]`); each edge cites its turns in `episode_indices`, and those turns become the fact's parents and get its `asserted` links (no citation = the whole session). With a decision provider (`decision.provider: gliner2`) the entities it finds in the session are the prompt's allowed-entity list. A per-session `stage_done` marker (stage `extract_graph`) plus the per-turn watermarks keep a second sweep from calling again. Records outside any session stay per record. `record` = one call per source record. |
| `memories.semantic.policies.extract_graph.event_identity` | `plain` | G-3: `plain` keys an extracted edge on (src, rel, dst). `dated` also keys an *event* edge on its event day and the numbers its fact states, so "visited Paris in 2022" and "in 2023" (or "ran 5 km" / "10 km") stay two events. State edges are unaffected. Switching on an existing graph re-extracts events under new keys. |
| `read.assembly.latest_slots` | `0` | H19: reserve up to this many of the most recent candidates (by event time) in the selection, so the latest evidence is not crowded out. `0` = off. |
| `read.assembly.dedupe_jaccard` | `1.0` | H23: drop a candidate whose word set overlaps an already selected one at or above this Jaccard ratio. `1.0` = off. |
| `memories.episodic.policies.consolidation.mine_facts` | `false` | C6′: a sleep stage mines dated atomic facts once per consolidated session through the write door. Needs an `extract` LLM role. |
| `memories.episodic.policies.consolidation.mine_by_topic` | `false` | H15: with `mine_facts`, mine each topic segment of a session in its own call (lexical-cohesion boundaries, no model), so the miner reads one topic at a time. A fact's parents are its segment's turns (the call saw nothing else). One call per segment. |
| `memories.episodic.policies.consolidation.mine_prompt` | `session` | #27: the `extract` prompt condition the miner selects. `session3` = `extract@session3` (a worked relative-date example, a complete-coverage rule for list items and countable events, evidence-line citations, and a 4096-token output cap sent as `max_tokens`). |
| `memories.episodic.policies.consolidation.mine_evidence_turns` | `false` | #29: number the miner's transcript lines (`[n] [YYYY-MM-DD] …`); a fact that cites valid lines in `turns` gets those turns as parents instead of the whole session (segment). |
| `memories.episodic.policies.consolidation.mine_event_dates` | `false` | #29: tag each mined fact `happened:<date>`: the H1 resolution of a relative phrase in the fact or its cited turns (each against its own date; `read.relative_week` applies), else the miner's `date`. A rule-resolved date also becomes the fact's `valid_from`. |
| `memories.episodic.policies.consolidation.mine_event_dates_llm` | `false` | #29: with `mine_event_dates`, one batched `extract@dates` call per mined batch dates the facts still undated. Needs the `extract` role. |
| `memories.episodic.policies.consolidation.mine_multiview` | `false` | #28: store each mined fact's multi-view fields as tags, `person:<name>`, `loc:<place>` and `topic:<class>` (NFKC, case-folded, whitespace collapsed), so read legs can prefilter; the statement is stored as mined. The fields pass the #31 guards (placeholders and reasoning dropped, at most 8 persons, 80 chars each). With the default `mine_prompt` the miner uses `extract@session4`, which asks for them; `mine_prompt: session4` selects that prompt without the tags. |
| `memories.episodic.policies.consolidation.list_cards` | `false` | #30: after `mine_facts`, keep one person-level list card per (person, class) of live event facts, e.g. `Melanie — activities: pottery class (2023-05), camping (2023-07)`. Person = `person:` tags, else the entity; class = `topic:` tag, else a non-generic miner attribute, else one `extract@classes` call per person batch (cached in the log). Groups of at least 2 facts; at most 25 items, newest kept. Parents = the facts (a hard forget cascades), trust capped at the least trusted fact; re-derived (old card archived) only when its members or text change. Cards are `atomic_fact` records, so `read.cards: header` shows them whole within `cards_budget_share`. |
| `memories.episodic.policies.consolidation.anticipate` | `false` | H8: a sleep stage asks the `anticipate` role (falls back to `extract`) once per session for likely future questions and stores them as cues via `add_cues`. |
| `memories.episodic.policies.consolidation.session_summary.incremental` | `false` | #56 (ADR-048): summarise open sessions too, and update a summary whose session gained turns with ONE `summarize@incremental` call over the previous summary plus only the new turns. Parents = every member turn (erasure cascades), trust = their minimum, each version supersedes the previous one. A summary written while the session was open is re-stamped without a call when it closes; only then do the derived stages (mine_facts, anticipate, ...) see the session. Needs a `summarize` role (without one, the extractive summary is rebuilt). |
| `memories.episodic.policies.consolidation.session_summary.rebuild_every` | `8` | #56: with `incremental`, rebuild the summary from all turns (one `summarize` call) once this many turns were folded in incrementally since the last rebuild, so the summary cannot drift. |
| `memories.episodic.policies.consolidation.predict_calibrate` | `false` | #62 (ADR-049, Nemori predict-calibrate, research-grade): a sleep stage, once per consolidated session, predicts the session's facts from stored memory and its opening (`predict_episode` role, falls back to `extract`), compares the transcript with the known statements and the prediction (`calibrate` role, falls back to `extract`) and stores the facts memory does not hold yet as semantic facts tagged `surprise_fact` (parents = the session's turns, trust capped at them). A returned fact is dropped only when a known statement (trust at least 0.5) covers it, on an overlapping date for a dated event, so a repeat event on a new date is stored; a predicted line never suppresses storage. Prompt inputs are collapsed to single lines with markers escaped. Two calls per session. |
| `memories.associative.policies.community.algorithm` | `auto` | KB-12 (ADR-043): `auto` and `leiden` run graspologic-native Leiden (canonical edge order, seeded, warm-started from the previous partition) then LPA refinement, and stay a no-op without the `[community]` extra; `lpa` runs the built-in label propagation without the extra, with a collapse guard (largest community > 50% of >= 100 nodes keeps the previous partition and logs a warning). |
| `memories.associative.policies.community.refine_passes` | `10` | LPA passes that refine a Leiden result (stops early when nothing moves). `0` = pure Leiden. |
| `memories.associative.policies.community.incremental` | `false` | KB-12/#84: per sleep, new nodes take their neighbours' majority community and at most `incremental_passes` LPA passes run over the touched nodes; a warm full rebuild runs on the refresh triggers. State is kept as `community_partition` MARKER events. |
| `memories.associative.policies.community.incremental_passes` | `3` | LPA passes per incremental sleep. |
| `memories.associative.policies.community.refresh_fraction` | `0.1` | With `incremental`: a full rebuild runs once incrementally placed nodes exceed this share of the graph. |
| `memories.associative.policies.community.refresh_every` | `5` | With `incremental`: a full rebuild runs at least every this many sleeps. |
| `memories.associative.policies.community.summary_keep_jaccard` | `1.0` | #84: a community whose membership Jaccard against the member set its summary was written from is at least this keeps that summary (its membership links follow the community) instead of a rewrite, unless a newcomer is less trusted than the summary. `1.0` = off; `0.8` is the KB-12 recommendation. |
| `memories.episodic.policies.consolidation.reflect_profile` | `false` | H14: a sleep stage asks the `reflect` role (generic `reflect.yaml` prompt) once per session for profile insights, stored through `Engine.reflect`. Needs reflective memory enabled. |
| `memories.semantic.policies.conflict.merge_containment` | `false` | W6 (ADR-059): a same-key write whose content words are all in the current fact's is a restatement (NOOP), not a supersession of the richer fact. |
| `memories.episodic.policies.consolidation.miner` | `llm` | W5 (ADR-059): `rules` mines first-person personal facts without a model (`core/rule_miner.py`): home, origin, job, employer, relationship, age, education, diet, favourites, partner / parents supersede; likes, dislikes, pets, family, activities, plans coexist. With `mine_facts: true`. |
| `memories.episodic.policies.subject_tagging` | `false` | W8 (ADR-059): an episodic turn written as "Name: text" is tagged `speaker:<name>`. |
| `memories.episodic.policies.forget_detector` | `false` | G25 (ADR-059): a user turn asking to forget something ("please forget my address") is tagged `forget_request`; `Engine.forget_requests()` lists the candidate records. Nothing is deleted automatically. |
| `memories.episodic.policies.consolidation.auto_watch` | `false` | G33 (ADR-059): a mined `plans` fact whose source turn names a future time becomes a prospective watch due then (needs prospective memory). |
| `memories.episodic.policies.consolidation.mining_cache` | `false` | E1 (ADR-059): with the LLM miner, mine at temperature 0 and cache each reply by (model, prompt version, variant, transcript), so the same history always yields the same facts. |
| `memories.associative.policies.rule_edges` | `false` | W12 (ADR-061): sleep stage `rule_edges` (right after `extract_graph`), no model. Causal connectives (because / 'cause / since / after / due to / so / as a result / that's why) and answers to "why / what made you" turns become `because` LINKs from the effect turn to the cause turn (the earlier turn a clause names by content-word overlap, or the adjacent turn); kinship and role patterns ("my sister Ana", "Ana, my boss") become facts "Ana is Caroline's sister" through the semantic door. `true`, or a mapping: `causal`, `kinship` (both `true`), `lookback` (40 turns), `min_overlap` (2 words). Needs associative memory; read with `read.causal_walk`. |
| `memories.procedural.policies.lessons` | off | W17a (ADR-060): `true` lets corrections leave lessons; `{inject: true, max: 3, min_similarity: 0.5}` also shows the lessons for the query after all evidence in `assemble`, marked advisory. `Engine.record_outcome` writes lessons whatever the key. |
| `memories.procedural.policies.recall_k` | `3` | W17b (ADR-060): default k of `Engine.recall_plans` (ranked `cos × (1 + helpful) / (1 + harmful)`, failure-dominated plans pruned). `recall_plan` stays top-1. |
| `memories.procedural.policies.auto_verify_on_reward` | `false` | W17b (ADR-060): an outcome receipt with reward ≥ 1 advances a used staged plan to verified (active still needs the dry run). |
| `memories.procedural.policies.trajectory` | off | N27 (ADR-060): `{expand: true, radius: 1, cap: 20}` brings the neighbouring steps of a trajectory hit (`Engine.record_trajectory`) into `assemble`. |
| `memories.procedural.policies.task_state` | `false` | W17e (ADR-060): `assemble(..., session_id=<task id>)` pins the open task state (`Engine.set_task_state` / `update_subgoal`, done needs a receipt) right after the persona. |
| `memories.procedural.policies.quarantine_lesson` | `false` | N24 (ADR-060): every quarantine verdict writes an advisory lesson with the source signature, reason kinds and content hash, never the held text; repeats add notes. |
| `memories.episodic.policies.correction_detector` | `false` | W17d (ADR-060): a user turn correcting a fact ("no, I said Tuesday", "actually it's", "not X, Y", "that's wrong") is tagged `correction`; the best-matching live keyed fact is superseded (or retracted without a replacement). |
| `memories.semantic.policies.trust.source_types` | `{}` | N26 (ADR-060): document type (`doctype:<t>` tag, else channel) → authority tier 0–3; trust capped at 0.3 / 0.45 / 0.6 / 1.0. |
| `memories.semantic.policies.trust.hold_needs_evidence` | `false` | N26 (ADR-060): hold a non-privileged write below `authority_min_tier` (2) in quarantine (`pending_evidence`); `Engine.review_evidence` promotes it on authoritative support. |
| `memories.associative.policies.entity_nodes` | `false` | GP-2 (#13): the graph projector adds an `ent:<namespace>:<canonical>` node per entity a record names (its `entity` field and `dst:` tags; canonical = NFKC, casefolded, whitespace collapsed) and a `mentions` edge record -> entity weighted by the record's trust. `true` uses the default blocklist (pronouns, day words, "luck"); a map takes `blocklist` (replaces it) and `allowed` (only these names). Rebuild == incremental; forgetting a record removes its mentions and any entity left without one. `mentions` is a reserved rel. Change it, then `engine.rebuild()`. |
| `memories.associative.policies.entity_summaries` | `false` | GP-6 (#17): the `summarize_entities` sleep stage (after `extract_graph`, before `reorganize`) writes one summary record per entity node whose membership (the live records with a `mentions` edge to it) changed since the last sweep; unchanged entities cost nothing (watermark: `entity_summarized` MARKER events, erased with the summary). The dated fact lines are the summary for free while they fit `free_chars` (2000); longer ones go to the `summarize_entity` role (else `summarize`; prompt `summarize_entity.yaml`: facts only, dates kept, no meta-language), `batch_size` (30) entities per call; without a role, the newest lines that fit. The record is derived (channel `entity_summary`, tags `entity_summary` + `about:<Name>`): parents = the members (a member's hard forget cascades to it), trust = the least member trust, firewall-screened. Drift supersedes it (archived); a soft-forgotten member is dropped at the next sweep. `true` or a map with `free_chars`, `batch_size`. Needs `entity_nodes`. Read with `read.entity_summaries`. |
| `memories.semantic.policies.extract_graph.resolve` | `off` | GP-7 (#18): entity resolution in `extract_graph` before facts are written: `rules` = exact canonical name, then the alias table (earlier decisions), then candidates (embedding cosine top 15 among the namespace's entities), an entropy gate (short low-entropy names such as "Mel" skip string matching) and MinHash shingle Jaccard ≥ 0.9; `llm` adds one batched `resolve_entity@batch` call per sweep for the names still ambiguous (needs a `resolve_entity` role; without one it acts as `rules`). A merged name is written as the known entity's spelling ("Mel" → "Melanie"). A match whose source trust differs from the entity's (the most trusted record naming it) by more than 0.2 is `contested` and not merged. Decisions are `entity_resolved` MARKER events (keyed by the source record, erased with it), so rebuilds and later sweeps reuse them without a call. Names new in the same sweep are not resolved against each other; write-time records are not resolved. |

---

## Where to go next

- [`FEATURES.md`](./FEATURES.md) — the feature catalog (types, firewall, E2–E9).
- [`examples/01_quickstart.py`](../examples/01_quickstart.py) → `04_prospective_shared_rest.py`.
- [`memspine-structure-plan.md`](./memspine-structure-plan.md) — the authoritative blueprint.
- [`adr/`](./adr/) — architecture decision records (ADR-001 … ADR-031); the newest cover
  the multi-call write pipeline (ADR-026), record group tags (ADR-027), Leiden community
  detection (ADR-028, amended by ADR-043), the trust-horizon invariant (ADR-029, proposed), relevance-first
  scoring (ADR-030, proposed), and the decision port with call accounting (ADR-031,
  proposed).
