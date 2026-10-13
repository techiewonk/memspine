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
pip install "memspine[graph,ingest,rest]"   # pick the extras you need

# Developers (all extras + test/lint/docs tooling):
uv sync --all-extras
just check        # ruff + mypy --strict + pytest
```

Common extras: `graph` (LadybugDB), `ingest` (markitdown+chonkie), `ner` (gliner2),
`compress` (llmlingua, E5), `rerank` (flashrank, E8),
`community` (graspologic-native), `rest` (FastAPI), no extra for the MCP server or the agent tools (stdlib only), `dbos`/`taskiq` (durable/brokered workers). See the
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

A record outside the scope never reaches the context. That covers search hits, replay neighbours, `full` mode, and the recent-conversation and digest headers. Both filters combine with each other and with the date filters. Two more filters, `memory_types=[...]` (e.g. `episodic`, `semantic`) and `tags_any=[...]` (at least one tag), work the same way (G-12). `focal_entity="Name"` adds that entity as the first seed of the graph walk and the graph rerank for this one read (G-11, Graphiti's center node). Example: `read(q, namespace="u", sessions=["trip-1"], roles=["user"])`.

**Per-write extraction hint (G-8).** `write(..., extraction_hint="only employment facts")` stores the hint with the record. `extract_graph` passes it to the edge prompt as instructions for that record only.

**Structured session summaries (G-19).** `prompts.selection.summarize: {condition: structured}` makes session summaries five labelled lines: Request, Findings, Outcome, Next steps, Open questions (`summarize@structured`). That shape suits "what did we cover" and aggregation questions, and `brief()` shows these summaries.

**Session-start brief (G-20).** `brief(namespace, budget_tokens, summaries=3, recent_turns=6)` returns context for the start of a new session, with no question. In order, while they fit the budget:
- the profile block (with `read.profile_slots_header`);
- the newest session summaries;
- the last turns, oldest first.

Every record passes the same gates as a read.

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

**`graph` template (GR-14).** `Engine(template="graph")` turns on the whole Graphiti-style setup on LadybugDB: entity nodes from facts and from the names raw turns mention, embedded entity names, the graph walk from the entities a question names, and the hybrid entity-node search. Measure it against `base` before relying on it.

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

### Structured-output health (I69)
```python
engine.structured_stats()            # {"extract@2": {"calls": 40, "clean": 37, "repaired": 2,
                                     #   "validation_failed": 1, "llm_errors": 0, "retried": 0,
                                     #   "repair_rate": 0.05, "failure_rate": 0.025, ...}}
engine.structured_stats(reset=True)  # snapshot, then clear
```
Every extraction, planner and judge call parses the reply (YAML or JSON), repairs it
when the strict parse fails, and validates it. The counters say how often each prompt
needed a repair or failed outright (process-wide, in memory). If `failure_rate` is
under about 1% per prompt, leave the recovery switches off. Otherwise enable
`llm.structured.retry_on_error` (one re-prompt with the validation error) and, for
backends that support it, `llm.structured.constrained_retry`. The `[structured]`
extra (instructor) was removed: nothing imported it.
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
# Agent tools over MCP (I66; see docs/AGENT_TOOLS.md)
memspine mcp -n user/ana -c ./memspine.yaml        # stdio MCP server, namespace fixed here
memspine mcp -n user/ana --profile read_only       # search only
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
  provider: ladybug           # sqlite_adjacency is SQLite-only; use ladybug with postgres
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
| `data_profile` | `off` | I25: data-shape profile. `off` = nothing changes. `auto` = presets chosen from `data_shape`; or a comma-separated list of preset names from `config/presets/` (`ts_dated`, `ts_none`, `long_turns`, `large_history`, `named_speakers`, `chat_roles`, `non_english`). Presets are a layer between the template and your own settings, so anything you set wins. Not called `profile`: that key is the usage profile. See *Data-shape profiles* below. |
| `data_shape.has_timestamps` | `null` | I25: the data carries real per-turn timestamps (`true` -> `ts_dated`, `false` -> `ts_none`; `null` = unknown, selects nothing). |
| `data_shape.has_question_date` | `null` | I25: queries carry the date they are asked at (declared; no preset keys on it yet). |
| `data_shape.speaker_kind` | `unknown` | I25: `named` (-> `named_speakers`) \| `user_assistant` (-> `chat_roles`) \| `single_author` \| `unknown`. |
| `data_shape.turn_length` | `unknown` | I25: `short` \| `medium` \| `long` (-> `long_turns`) \| `unknown`. Typical (median) characters per turn: <= 300 short, >= 800 long. |
| `data_shape.history_size` | `unknown` | I25: `small` \| `medium` \| `large` (-> `large_history`) \| `unknown`. Large = about 2,000 turns or more per item. |
| `data_shape.language` | `null` | I25: a language code; anything not `en*` selects `non_english` (I23 guard). |
| `data_shape.abstention_possible` | `null` | I25: some questions have no answer (declared; no preset keys on it yet). |
| `event_log.mode` | `full` | `full` \| `rolling` (bounded window) \| `ephemeral` (nothing persisted — no rebuild/audit; taint rollback falls back to archiving the seed alone, `strict=True` raises) (D-45, #64). |
| `event_log.retention_days` | `30` | Rolling-window retention floor; never prunes past a projector high-water mark. |
| `event_log.compress` | `false` | zstd-compress event payloads at rest. |
| `storage.profile` | `embedded` | `embedded` \| `server` (F-16d, M-04 C). `embedded` = SQLite + LanceDB + Tantivy (today). `server` = Postgres (needs `storage.url`; sets `backend: postgres`) built by its own Alembic tree, pgvector when installed. Both pass the same store contract tests. |
| `storage.backend` | `sqlite` | `sqlite` \| `postgres` (ADR-025). |
| `storage.path` | `./memspine.db` | SQLite db file, or `:memory:` for ephemeral. |
| `storage.url` | `null` | Postgres DSN (secrets-resolved); required when `backend: postgres`. |
| `storage.data_dir` | `null` | Base dir for file-backed projections (LanceDB/Tantivy); required for postgres. |
| `storage.encryption.mode` | `none` | `none` \| `sqlcipher` (#52, ADR-035): SQLCipher encryption of the SQLite file, `[encrypt]` extra; sqlite backend only, not `:memory:`. Vectors, the lexical index and disk caches are not covered. |
| `storage.encryption.key_env` | `null` | **Name** of the environment variable holding the SQLCipher key; required with `sqlcipher`. The key is read only from it and never logged. |
| `embedding.provider` || `read.rerank` | `off` | `off` | `fastembed` | `flashrank` `[rerank]` | `litellm` | `jina` `[st]` (Jina listwise reranker; `rerank_model` defaults to `jinaai/jina-reranker-v3.5`, runs the repo's custom code) | `qwen3` `[st]`| `fastembed` (ONNX/CPU) \| `hash` (deterministic, tests) \| `static` (model2vec `[static]`) \| `litellm` (cloud). |
| `embedding.model` | `BAAI/bge-small-en-v1.5` | Embedder model id. |
| `embedding.dim` | `null` | **Required** when `provider: litellm` — a cloud embedder's output dim. |
| `embedding.api_base` | `null` | Endpoint override (litellm). |
| `embedding.api_key` | `null` | API key (litellm; secrets-resolved). |
| `embedding.aws_region` | `null` | Bedrock region (litellm). |
| `embedding.device` | `null` | `provider: st` only: torch device (`cuda`, `cpu`). |
| `embedding.dtype` | `null` | `provider: st` only: weight dtype (`bfloat16`, `float16`). `provider: st` needs `embedding.dim` and the `[st]` extra. |
| `embedding.query_prompt_name` | `null` | `provider: st` only: sentence-transformers prompt name used for queries (Jina v5: `query`). |
| `embedding.document_prompt_name` | `null` | `provider: st` only: prompt name used for documents (Jina v5: `document`). |
| `embedding.trust_remote_code` | `false` | `provider: st` only: allow the model repository's custom code to run (Jina v5 needs it). |
| `embedding.request_dimensions` | `false` | litellm only: request exactly `dim` dimensions (Matryoshka models: Cohere embed-v4 256/512/1024/1536, Titan v2, OpenAI v3). |
| `embedding.query_input_type` | `null` | litellm only: input type for retrieval queries (Cohere: `search_query`). |
| `embedding.document_input_type` | `null` | litellm only: input type for stored content (Cohere: `search_document`). |
| `embedding.query_instruction` | `None` | N64: fastembed only. Text prepended to every retrieval query before embedding; documents are unchanged, so no re-index. For the BGE v1.5 models, use `constants.BGE_QUERY_INSTRUCTION` ("Represent this sentence for searching relevant passages: "). fastembed's own `query_embed` does not add it. Query vectors are cached under a key that includes the instruction. |
| `embedding.versioned_spaces` | `False` | F-03 (P-25 C): version the vector space. Each vector belongs to a space (embedder model, dim, dtype, query instruction, normalisation); after an embedder change new writes go to the new space, reads query only a namespace's active space, and `reembed` builds the new space in batches and switches atomically. Off = one table per embedder, unchanged. |
| `embedding.profile` | `null` | MD-02 (P-17 C, M-23 B): a named embedding profile that fills provider, model, dim and query instruction. `gpu` = Qwen3-Embedding-0.6B, 1024 dims, bf16 on CUDA, with the evals retrieval instruction (`[st]`); `cpu_multilingual` = paraphrase-multilingual-MiniLM-L12-v2 (ONNX, 384 dims, fastembed). Null = the embedder fields are used as written. |
| `embedding.pin_profile` | `False` | MD-02: write the resolved profile (model, dim, dtype, instruction, normalisation, version hash) beside the store at creation (`<db>.embedding-profile.json`, and into the Lance table metadata) and refuse to open the store with a different one. Off = nothing written or checked. In-memory stores pin nothing. |
| `embedding.migrate_profile` | `False` | MD-02: accept a profile change on open (the pin is replaced, the old hash kept as `previous`). Needs `embedding.versioned_spaces`; run `VersionedSpaceStore.reembed` (F-03) to move the old records. |
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
| `graph.provider` | `auto` | `auto` (GR-1, ADR-064): LadybugDB when the `ladybug` package is installed (`[graph]`), else `sqlite_adjacency`. Explicit: `sqlite_adjacency` \| `ladybug` `[graph]` \| `neo4j` (reserved) (D-26). |
| `graph.entity_embeddings` | `false` | GR-3: embed entity-node names (GP-2 entity nodes) into the graph store's entity index. LadybugDB keeps them in an `EntityVec` table (`FLOAT[]` column plus its native FTS index), and entity search ranks by exact in-engine cosine; sqlite_adjacency keeps them in node properties. Rows are written once per entity, removed with the node, and cleared on rebuild. |
| `graph.key_v2` | `false` | F-01 (M-17, W-20): graph key format v2 for `extract_graph` edges. The key hashes the edge kind, normalised endpoints and relation, and an occurrence (the sorted source records the edge cites), so replaying the same turns is idempotent while A -> B -> A writes a new occurrence of A instead of being swallowed as a duplicate of the closed A. A live earlier occurrence still takes a verbatim restatement as provenance (GR-9). Old (v1) keys are aliases: records written before the flag are found by their old key. Emits `derive.graph.key` on the wiretap. Off: the `(namespace, src, rel, dst)` key, unchanged. |
| `graph.edge_compiler_v2` | `false` | F-02c (M-16, S5-3, LIB-35): the `compile_edges` sleep stage (before `summarize_entities`) compiles assertion records (semantic facts with a subject; `rel:`/`dst:`/`kind:` tags of `extract_graph` facts give the object and relation) into typed graph edges: one assertion node, role edges (`role:subject`, `role:object`, `role:value`, `role:time`, further n-ary roles) and one subject-object projection edge, with the tri-temporal axes (`valid_from`/`valid_to`, `source_at`, `event_precision`, `event_basis`), evidence ids, trust and a `tentative` tag (hypothetical, planned, reported or question content, assistant-proposed content, partly bound edges). Edges are projected as LINK events and only when their stored form differs (idempotent). A forgotten, hidden or retired record takes its edges with it (lineage by `record_id`); a superseded one keeps closed edges. With `isolation.scope_keys_v2` the keys use tenant + owner. Needs a graph store (associative memory). Emits `graph.compile.*` on the wiretap. Off: no compiled edges, unchanged. |
| `graph.backend` | `auto` | F-16e (M-04 / M-18, owner Q4): the server profile's graph backend. `auto` = the embedded store chosen by `graph.provider`; `neo4j` selects Neo4j as the server graph, which still needs `graph.neo4j_outbox` (the embedded store stays the read path; Neo4j is fed through the outbox). |
| `graph.neo4j_outbox` | `false` | F-16e: mirror the graph to Neo4j through the durable outbox (`workers`-independent runner on the storage database). Every WRITE node, LINK edge (plain and compiled), tombstone and FORGET is queued as a `graph_projection` outbox item; a registered handler applies it with idempotent MERGE (scope-keyed with `isolation.scope_keys_v2`), resuming from the F-04 watermark after a crash and retrying holes. Erasure / retire removes the edges and orphan nodes; `rebuild()` clears and re-feeds Neo4j from the log. Needs `storage.profile: server` or `graph.backend: neo4j`, `memories.associative`, the `[neo4j]` extra and the three environment variables below. Off: no Neo4j, embedded graph unchanged. Emits `graph.neo4j.*` on the wiretap. |
| `graph.neo4j_url_env` | `MEMSPINE_NEO4J_URL` | F-16e: NAME of the environment variable holding the Neo4j URL (the value is never stored in config). |
| `graph.neo4j_user_env` | `MEMSPINE_NEO4J_USER` | F-16e: NAME of the environment variable holding the Neo4j user. |
| `graph.neo4j_password_env` | `MEMSPINE_NEO4J_PASSWORD` | F-16e: NAME of the environment variable holding the Neo4j password. |
| `graph.neo4j_database` | `neo4j` | F-16e: Neo4j database name. |
| `isolation.scope_keys_v2` | `false` | F-16c (M-05): the read-cache key and the `extract_graph` edge key are built from the typed `Scope` (`memspine.scope`: tenant + owner; the session is provenance and never part of a key). Off: the namespace stands for both scope fields in the cache key and the edge key hashes the raw namespace. Edges keyed with it on do not merge with edges keyed with it off. |
| `isolation.viewer_required` | `false` | F-16c (G-12 B): every engine read verb (`search`, `read`, `assemble`, `retrieve`, `shared_search`, `get_record`, `brief`, `conversation`, `session_memories`, `timeline`, `sessions`, `namespace_stats`) refuses a read of a multi-party namespace that names no viewer (`ScopeRefused`, reason `viewer_required`; tap `scope.refused`). Verbs with no native viewer filter drop records the viewer may not see (tap `scope.filtered`). Off: no refusal. FX-EX1A (EX-1a finding F1): while on, a multi-party namespace behaves as `write.participants: session` (raw turns tagged with their speakers) and every derived record inherits its parents' participant and visibility tags, never more visible than the strictest parent, whatever `write.participants` says. |
| `isolation.multi_party_namespaces` | `[]` | F-16c (G-12 B): namespaces (and everything under them) that count as multi-party for `isolation.viewer_required`, besides namespaces a `Memory(..., multi_party=True)` handle declares and namespaces holding records with several participants or a `visibility:` tag. |
| `llm.roles.*.model` | `""` | LiteLLM model id; **prefix routes** (`openai/`, `ollama/`, `bedrock/`, `vertex_ai/`, `llamacpp/<path>`) (ADR-024). |
| `llm.roles.*.api_base` | `null` | Local endpoint override (Ollama, vLLM, …). |
| `llm.roles.*.api_key` | `null` | API key (secrets-resolved). |
| `llm.roles.*.aws_region` | `null` | Bedrock region. |
| `llm.roles.*.timeout_seconds` | `60.0` | Per-call timeout. |
| `llm.roles.*.no_think` | `null` | Qwen3 thinking switch: `true` appends ` /no_think` to the last user message; `null` = on for model ids containing `qwen3`, off otherwise. `<think>…</think>` blocks are always stripped from replies. |
| `llm.structured.retry_on_error` | `false` | I69: when a structured reply fails validation, re-prompt ONCE with the validation error appended. Measure first with `engine.structured_stats()`. |
| `llm.structured.constrained_retry` | `false` | I69: on that retry only, request JSON-schema constrained decoding (`response_format`; OpenAI-compatible servers, Ollama via LiteLLM, llama.cpp); a backend that refuses it falls back to a plain retry. |
| `models.tiers_v2` | `false` | MD-01 (P-01, P-02, P-03, P-15, P-19). Roles with no `llm.roles` entry resolve through tier, placement and fallback (`memspine.models.resolve`); a role no model serves gets no provider and makes zero model calls. Off = roles resolve from `llm.roles` exactly as before. Taps: `models.resolve`, `models.placement`, `models.rerank_chain`. `describe()["models"]` holds the plan. |
| `models.catalogue` | `[]` | MD-01. Declared models with memory estimates: `id` (LiteLLM id for an llm), `kind` (`llm`/`reranker`/`embedder`), `tier` (`none`/`small-local`/`worker`/`frontier`), `family` (reranker: `jina-v3.5`/`qwen3-reranker-0.6b`/`minilm`), `hosted`, `vram_mb`, `ram_mb`, `context_vram_mb`, `placement` (`any`/`gpu`/`cpu`), `supports_thinking`, `profile`, `api_base`, `api_key`, `aws_region`, `timeout_seconds`. The plan is computed from these sizes; no GPU is needed. |
| `models.gpus` | `[]` | MD-01. Declared CUDA devices (`name`, `vram_mb`); empty = CPU only. A non-NVIDIA name (AMD, Intel, DirectML) is ignored: MD-03 measured the iGPU 5-8x slower than the CPU. |
| `models.detect_gpus` | `false` | MD-01. Also probe `nvidia-smi` when no GPU is declared. |
| `models.ram_mb` | `0` | MD-01. Host RAM budget for CPU placement; 0 = not checked. |
| `models.gpu_headroom_mb` | `512` | MD-01. VRAM kept free on every GPU. |
| `models.default_rerankers` | `shipped` | MD-01. `shipped`: when no reranker is declared, plan the owner-chosen chain (Jina v3.5, then Qwen3-Reranker-0.6B, then MiniLM on CPU) with estimated sizes. With `read.rerank_v2` on, the chain and each link's device come from the plan. |
| `models.rerank` | `chain` | MD-01. `off` leaves reranking out of the plan (results unscored). |
| `models.embedder_profile` | `auto` | MD-01. `auto`/`cpu`/`gpu`: the GPU instruction embedder is chosen only when it lands on a GPU, else the CPU default. |
| `models.role_tier` | `{}` | MD-01. Catalogue role -> tier name. `none` turns the role off (the no-model path); another name picks the smallest declared model at that tier or above. Roles: `extract`, `extract_edges`, `summarize`, `call2`, `resolve`, `dedupe`, `sufficiency`, `query_rewrite`, `relevance`, `chat`, `verify`, `judge`, `reflect`, `plan`. |
| `models.pins` | `{}` | MD-01. Catalogue role -> declared model id (wins over tier choice; a hosted pin on a local-only role is an error). |
| `models.allow_hosted` | `false` | MD-01 (P-02). Hosted models may serve only reader-side roles marked hosted-ok (`chat`, `verify`, `plan`, `sufficiency`, `query_rewrite`); raw-turn roles stay local. |
| `models.required_roles` | `[]` | MD-01 (P-03). Catalogue roles that must be served; with `on_missing_model: error` a missing one fails start. `write.window_extract` != `off` adds `extract`. |
| `models.on_missing_model` | `error` | MD-01 (P-03). `error` fails start for a required role with no model; `rules`/`skip` record the role's fallback (rules or skip, as the role defines) and carry on. |
| `models.thinking` | `{}` | MD-01 (P-15). Catalogue role -> capped thinking budget in tokens. Honoured only for `judge` and `reflect` on a model declared `supports_thinking`; every other role stays thinking off. Screen before enabling (paired conflict cases, truncations counted). |
| `read.scoring` | `{}` | Options for `ScoringPolicy.bind` (M1 composite). |
| `read.assembly` | `{}` | Options for `AssemblyPolicy.bind` (E2 placement / MMR). |
| `read.rerank` | `off` | `off` | `fastembed` | `flashrank` `[rerank]` | `litellm` | `jina` `[st]` (Jina listwise reranker; `rerank_model` defaults to `jinaai/jina-reranker-v3.5`, runs the repo's custom code) | `qwen3` `[st]`\| `fastembed` \| `flashrank` `[rerank]` \| `litellm` \| `qwen3` `[st]` (Qwen3-Reranker, `rerank_model` defaults to `Qwen/Qwen3-Reranker-0.6B`) — E8 cross-encoder (D-51). |
| `read.rerank_model` | `null` | LiteLLM rerank model id; required when `rerank: litellm`. For| `read.rerank` | `off` | `off` | `fastembed` | `flashrank` `[rerank]` | `litellm` | `jina` `[st]` (Jina listwise reranker; `rerank_model` defaults to `jinaai/jina-reranker-v3.5`, runs the repo's custom code) | `qwen3` `[st]`/ `qwen3` it overrides the default local model. |
| `read.rerank_device` | `null` | `rerank: qwen3` only: torch device (`cuda`); unset = CPU float32. |
| `read.rerank_quant` | `null` | `rerank: qwen3` only: `4bit` | `8bit` (bitsandbytes, GPU). |
| `read.rerank_chunk_chars` | `null` | I9: `null` = a record longer than the reranker's context is cut and its tail dropped (qwen3: beyond about 8k tokens; ONNX cross-encoders: about 512). N = records longer than N characters are scored in overlapping windows of N characters and take the MAX window score. Opt-in. |
| `read.rerank_chunk_overlap` | `0` | I9: characters shared by neighbouring windows. |
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
| `read.exact_budget` | `false` | R3-09a: count every budgeted piece with the reader tokenizer (not `len // 4 + 1`) and skip an oversize record to try smaller ones instead of stopping at the first overflow. |
| `read.reader_tokenizer` | `null` | R3-09a: reader tokenizer, resolved offline only (`hf:<name>` cached tokenizer, `tiktoken:<name>` cached encoding, or a bare name); unresolvable: chars/4 estimate, reported as `accounting=estimated`. |
| `read.exact_budget_max_skips` | `8` | R3-09a: over-budget records skipped before the rest are dropped (early break). |
| `read.evidence_bundles` | `false` | R3-09b: mined facts enter as complete evidence bundles (fact plus cited source turns), whole or not at all, packed under the true budget. |
| `read.bundle_pool_factor` | `2.0` | R3-09b: selection pool as a multiple of the budget when bundles are on. |
| `read.bundle_fact_share` | `0.6` | R3-09b: share of the evidence budget fact bundles may take while other records wait. |
| `read.trust_rendering` | `false` | T5-02: low-trust, tentative and contested records rendered by band as quoted, neutralised data; quarantined records withheld; replaces the B6 untrusted note. |
| `read.default_mode` | `auto` | The mode `Engine.read()` uses when the caller passes none (`auto` / `full` / `replay` / `retrieve` / `compose`). The `assistant` template pins `replay`. |
| `read.rerank_date_prefix` | `false` | Hindsight: prefix reranker inputs with `[Date: YYYY-MM-DD]`, so the cross-encoder sees when each candidate happened. |
| `read.rerank_blend` | `None` | N41: None = the reranker's relevance replaces the retrieval score (unchanged). A weight `w` in [0, 1] blends them: `w * rerank + (1 - w) * retrieval`, each min-max normalised over the candidates. |
| `read.rerank_gate` | `None` | N41 confidence gate: when the reranker's best raw score is below this value, it is not confident any candidate answers, and the retrieval order is kept (`rerank_stats()["gated"]` counts these). |
| `read.rerank_context` | `0` | N42: the reranker sees each candidate with this many neighbouring turns of its episodic session on each side (0–3), so a short reply ("yes, the lake") is judged with its question. The stored record and the context shown to the reader are unchanged. |
| `read.rerank_balanced` | `false` | GR-15 (Graphiti's balanced shortlist): with a reranker on, the pool cut before reranking takes each leg's best remaining hit in turn (vector, lexical, then the extra legs), so one leg cannot fill the pool. |
| `read.pool_protect_per_leg` | `0` | I75a: each leg's top-N hits (vector, lexical, every extra leg) are guaranteed a slot in the pool sent to the reranker even when their fused rank falls outside the pool size; the rest is filled by RRF. The pool grows by at most legs x N. 0 = off. |
| `read.pool_protect_mode` | `per_leg` | R02 / I75 v2, with `pool_protect_per_leg: N`. `per_leg` = I75a above. `source_family` protects the top-N per independent source family (lexical; semantic = vector + perspective + other vector-derived legs; temporal; other), counts a hit several correlated legs vote for once, and keeps the pool at its normal size by evicting the lowest-RRF unprotected candidates. Forensics: `protected`, `pool_family`. |
| `read.family_fusion` | `false` | R3-06 (LIB-10). Fuse by source family (a family's legs merge by best rank), protect each family's top `pool_protect_per_leg` hits (3 when 0), apply `leg_min_scores` as floors that never drop a protected hit, cap the pool. Equal leg weights. Tap: `read.fusion.pool`. |
| `read.lift_before_rerank` | `false` | R3-08 (LIB-11, ER-21 B, POL-2). Lift derived hits to their source turns before the rerank (lift, re-admit, dedupe, cap, rerank); a derived hit with a hidden or missing source turn is dropped. Tap: `read.lift.stage`. |
| `read.lift_max_turns` | `2` | R3-08. At most this many source turns replace one derived hit. |
| `read.rerank_v2` | `false` | R3-07 (LIB-42). Rerank every read through `rerank_v2_chain` over a pool capped at `rerank_v2_pool`; unscored candidates stay unscored (never 0.0) and follow the scored ones; the Jina tail is None instead of 0.0. Ignores `read.rerank` and the `rerank_max_top_k` gate. Tap: `read.rerank.scored`. |
| `read.rerank_v2_chain` | `[jina, qwen3, fastembed]` | R3-07. Reranker modes in preference order (Jina v3.5, Qwen3-Reranker-0.6B, MiniLM CPU); all failing means unscored. Drop `jina` to avoid CC-BY-NC weights. |
| `read.rerank_v2_pool` | `64` | R3-07. Candidates sent to the reranker; the rest stay unscored. |
| `read.perspective_leg` | `true` | I75b: `false` makes `perspective_mode` a pure multiplier (`1 - perspective_weight * (1 - match)`) on candidates the other legs found; no perspective RRF leg is added. |
| `read.mmr_lambda` | `None` | G-10: a lambda in [0, 1] reorders the final hits by maximal marginal relevance on embeddings: `lam * relevance - (1 - lam) * max cosine to the hits already taken`. 1 = relevance only, 0 = diversity only. Scores are kept; only the order changes. |
| `read.skip_rerank_for_ordering` | `false` | Agent Zero: skip the reranker for ordering questions (first / latest / most recent). |
| `decision.provider` | `off` | H24: optional decision provider for calibrated choices among described options without generation. `gliner2` uses the `[ner]` extra (GLiNER2, Apache-2.0). |
| `decision.model` | `fastino/gliner2-base-v1` | H24: the GLiNER2 checkpoint (Hugging Face id; `-large-v1` and `-multi-v1` also exist). |
| `read.planner` | `rules` | H24/G2a: how `read(mode="auto")` picks a mode once full context does not fit. `rules` = deterministic cues; `decision` = the `query_shape` rules settle counts and sets (compose) and ordering questions (replay) first, then the decision provider chooses among `count or list` / `reason or feeling` / `single fact` (compose / replay / retrieve; G24, ADR-052), falling back to the rules on any failure; `llm` = one call to the `plan` LLM role returns a `ReadPlan` (`lookup` / `replay` read by replay, `aggregate` by compose with the plan's up to three subqueries as extra probes, rank-fused with `read.rrf_k`). An unbound role, a failed call or an invalid plan falls back to the rules with a warning. One counted call per auto read. |
| `read.planner_min_confidence` | `0.0` | G2b: with `planner: decision`, a provider choice below this confidence does not route the read (a rule choice is not gated); it keeps the default `replay` (retrieve when no hit is episodic). A choice of unknown confidence (a bare GLiNER2 label) is below any gate above 0. `0.0` = every choice routes. |
| `read.planner_version` | `v1` | #35: with `planner: llm`, `v2` selects the `plan@v2` prompt, which also writes one or two evidence-seeking subqueries for lookup questions ("Is X religious?" → "X church", "X faith"); the lookup read fuses each as an extra vector (+ BM25 under hybrid) leg by RRF. #36: `v3` selects `plan@v3`, which is v2 plus `persons` (the people the question names) and `time_expr` (its date or period words, verbatim); the routed read fuses the persons / time leg (`read.person_time_leg_k`). Still one plan call. `v1`: unchanged. |
| `read.person_time_leg_k` | `10` | #36: with `planner_version: v3`, the most records of the persons / time leg. `time_expr` becomes a date span by the H1 rules (an absolute date, month or year first; else a relative phrase, `read.relative_week` applying, anchored on the namespace's newest record); the leg holds the live records whose `valid_from` lies in the span and/or that are about a planned person (a `person:<name>` tag when the record has any, else its `entity` naming the person as a whole word), both first, then closest to the span's middle, else newest. Fused by RRF into the lookup or compose read; every search gate still applies. |
| `read.completeness_check` | `false` | #38: for reads routed to compose whose plan is `aggregate` or whose question is a list or count question (`query_shape.is_aggregation` / `is_count`), ask the `sufficiency` LLM role (else `plan`) whether the composed context holds every item the question needs (+1 call, prompt `sufficiency`); when it does not, ask for up to three missing-information queries (+1 call, `sufficiency@missing`) and run the compose read once more with them as extra probes. One round at most; an abstained read, a complete verdict, an unbound role or any failure keeps the first read. Other reads make no extra call. Off: byte-identical. |
| `read.completeness_rounds` | `1` | G-17 (SimpleMem multi-round reflection): how many completeness rounds a checked compose read may run (1–3). Each round adds the new missing-information queries as probes; the loop stops at the first complete verdict or when no new query appears. Each round costs one `sufficiency` call plus one missing-queries call. |
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
| `read.replay_window_before` | `null` | RETRIEVAL_GAPS finding 4: turns of the hit's session replayed BEFORE each hit (replay mode and the `compose_replay` expansion). `null` = the `read(replay_window=...)` argument (default 2, symmetric, unchanged). 63% of the gold a window supplies lies after the hit, so `before=2, after=4` beats a symmetric 3 at equal cost. `0` = no earlier neighbours. `read(replay_window=0)` still disables the compose expansion. |
| `read.replay_window_after` | `null` | As `replay_window_before`, for turns AFTER each hit. `null` = the `replay_window` argument. |
| `read.replay_window_unit` | `turns` | I6/I20: `turns` = the window counted in turns (unchanged). `tokens` = each hit takes neighbours nearest first, per side, until that side's token allowance is used; a neighbour that would pass it closes the side (long assistant turns, very long histories). |
| `read.replay_window_tokens_before` | `256` | I6: token allowance per hit for the turns BEFORE it (unit `tokens`). |
| `read.replay_window_tokens_after` | `512` | I6: token allowance per hit for the turns AFTER it (unit `tokens`). |
| `read.replay_budget_scaling` | `false` | I20: scale the turn window, the token allowances and `top_k` by `f = min(1, budget / replay_budget_reference)` (floor 0.25), so pool and window shrink together when the budget is tight (a side that had a neighbour keeps one turn; `top_k` keeps 1). |
| `read.replay_budget_reference` | `4096` | I20: the budget at which the window is at full size. |
| `read.replay_hits_first` | `false` | I20: replay admits every hit (best first) before any neighbour, so a neighbour of one hit never costs a lower-ranked hit its place. |
| `read.dedupe` | `off` | I31: near-duplicate removal among the candidates before assembly: `exact` (equal text), `jaccard` (content-word Jaccard >= `dedupe_threshold`), `embedding` (vector cosine >= `dedupe_threshold`; falls back to jaccard for a record with no vector). The drops are in `search_forensics()["dedupe_dropped"]` as `(dropped id, kept id, similarity)`. The assembly policy's `dedupe_jaccard` (H23, MMR stage) is separate and unchanged. |
| `read.dedupe_threshold` | `0.9` | I31: the similarity at or above which two candidates are duplicates. |
| `read.dedupe_keep` | `best` | I31: `best` keeps the higher-scored copy; `earliest` keeps the earlier one (by event time) with the better of the two scores. |
| `read.profile_relevance_gate` | `off` | I33: `overlap` = a profile line (the slots header, the profile header's insights) is shown only when it shares a content word with the question (a name alone is not relevance); session-start digests are not gated. The seam `Engine._profile_line_relevant` is where the decider's relevance check plugs in. Profile blocks stay off in the BEST config. |
| `read.rerank_floor` | `minmax` | RETRIEVAL_GAPS finding 3: a reranked list is min-max normalised (best 1.0, worst 0.0), so `read.assembly.relative_floor` then deletes about half of the hits and their replay windows whatever the reranker thought of them. `minmax` = unchanged. `skip` = a read the reranker scored does not apply the relative floor (the reranker, `rerank_keep` and the budget bound the context); a read it did not score (off, gated, failed) still does. A `raw` mode (floor on raw reranker probabilities) was left out: only some rerankers are calibrated 0-1, and the floor would still act on the composite score. |
| `read.resolve_relative_dates` | `false` | H1: annotate relative-time phrases ("yesterday", "last Friday", "two weeks ago", "last month") in assembled/read records with the absolute date, resolved against each record's event time: `last Friday [= Fri 2023-07-14]`. Rules only, no model. Vague phrases ("recently") are left alone. Stored content is unchanged. On LoCoMo temporal evidence the resolver agrees with the gold date in 126/129 resolvable cases. **Reproducibility (#29, Wave 1):** a mined fact tagged `happened:<date>` is resolved against its `said:` day, and one without a `said:` tag whose `valid_from` is its happened day is not resolved at all, so pre-Wave-1 builds (≤ d6dccc5) rendered e.g. `yesterday [= Thu 2023-06-08]` on it and current builds do not. Runs with this key on (including the `base` template and `read.cards: header`) over happened-tagged facts are therefore not reproducible against pre-Wave-1 builds; with it off, reads are byte-identical (`tests/unit/test_pre_wave1_read_golden.py`). |
| `read.relative_dates_anchored` | `false` | G13: with `resolve_relative_dates`, state week-level phrases relative to the record's own day, LoCoMo's gold convention ("The week before 9 June 2023"): `last week [= the week before 2023-06-09 (2023-06-02..2023-06-08)]` instead of the previous calendar week; `last/this/next weekend` → `the weekend before / of / after <day>`, `last Friday` → `the Friday before <day> (Fri …)`, `two weeks ago` → `two weeks before <day> (≈ …)`, and `a few days ago` → `a few days before <day>` (no span). Months, years and single days are unchanged. Off: byte-identical. |
| `read.skip_defaulted_dates` | `false` | I7: a source with no timestamps (ConvoMem, PrefEval, LaMP shapes) gets `valid_from` = the write clock, so every date prefix would show today. On: a write with no supplied event time is tagged `ts_defaulted`; `render: dated` / the `[YYYY-MM-DD Day]` prefix, the `resolve_relative_dates` annotation, `rerank_date_prefix` and the timeline date skip such records. Timestamped records are never affected; records written with the key off carry no tag (so they render as before). Facts mined later from an undated turn are not tagged. Off: byte-identical. |
| `read.language_guard` | `off` | I23: `on` = the English-only regex features fail closed (no trigger, no span) on a question or turn that a cheap script/stopword check judges non-English (`core/language.py`). Off: unchanged. See [English-only features](#english-only-features-i23). |
| `read.perspective_mode` | `off` | I39: `subject_weight` = an RRF leg `perspective` of the records about the question's subject plus a multiplier `1 - perspective_weight * (1 - match)` on each candidate's relevance (about the subject 1.0, the subject speaking of others 0.6, an unresolved "he / she" 0.5, about someone else 0.0; untagged records neutral). `subject_filter` = the same, and candidates about someone else are dropped (never below `perspective_min_keep`). Needs the tags of `memories.episodic.policies.perspective`. Forensics: `perspective` (asker, about, targets, factors, dropped). Off: unchanged. |
| `read.perspective_weight` | `0.4` | I39: strength of every perspective multiplier (0 = none, 1 = a mismatch scores zero). |
| `read.perspective_min_keep` | `3` | I39: `subject_filter` never leaves fewer candidates than this (the best dropped ones are put back). |
| `read.perspective_asker` | `null` | I39: who asks ("caroline", "user"). None = the `asker_scope(...)` context value (`memspine.core.perspective`; the eval adapter sets it from a question's `asker` / `persona`), else `user` when the store has no named participants, a lone participant, else unresolved (first-person questions stay neutral). |
| `read.perspective_axes` | `["subject"]` | I39: the axes the read uses: `subject` (whose / about whom), `modality` (a plan, wish, hypothetical, question or request is weaker evidence of a fact, unless the question asks for plans / wishes / questions), `polarity` (a purely negated statement is weaker evidence for a positive question, kept for "not / never / ever / dislike" questions), `scope` (a one-off event is weaker evidence for a trait question: "what does X like"), `sensitivity` (a `sensitive:*` record needs the question to touch the same category), `hearsay` (I43: a `rep:` record is second-hand evidence about its source, or about anyone when the source is unspecified), `certainty` (I44: a `cert:hedged` record is weaker evidence unless the question itself hedges: "do you think", "probably"), `ack` (I47: an assistant statement about the user that no later user turn acknowledged is weaker evidence for a question about the user; dropped under `subject_filter`). Each multiplies relevance by `1 - perspective_weight` (`scope`, `hearsay`, `certainty`: half). With `certainty` selected, `read.perspective_marker` also shows `[hedged]`. |
| `read.perspective_marker` | `false` | I39: show `[about: Caroline's cousin]` before a record whose subject differs from its speaker (stored content unchanged). |
| `read.owner_check` | `off` | I59: read-side owner check after retrieval, before the reader. `mark` prefixes `[Melanie, about Melanie]` (speaker, subject) on tagged lines; `note` adds one reader note when no line is about the person the question names and some line is about someone else; `both`. Never drops a line; a question naming two people keeps both. Needs the `memories.episodic.policies.perspective` tags; `decider_tasks: [about_target]` lets the decider judge untagged lines. |
| `read.entity_check` | `off` | I60: `note` adds "<Name> is not mentioned in the memories" for a capitalised name in the question that occurs nowhere in the store (exact match; a 3+ letter prefix of a stored word counts as present). |
| `read.user_header` | `off` | I63: `on` opens the context with "You are the assistant. The user is <asker>. Memory lines are labelled with their speaker." when the asker is known (`perspective_asker` or the caller's `asker_scope`); nothing when unknown. |
| `read.query_contract` | `off` | A03: a typed query contract built from the question (`core/query_contract`): subjects, relation, answer type (person / place:city, country, region / date, time, year / duration / count / quantity / title / name / yes-no / choice / reason / description / entity), time scope, cardinality (one / many / count) and request kind (recall / inference / recommendation). `heuristic` = rules only (English surface cues, no model, no dataset vocabulary); `llm` = the rules first, and one `plan@contract` call only when the rules are unsure of the answer type (it may refine, never blank, the contract; a failure keeps the rules). The contract is always logged in the forensics (`query_contract`). An unknown type adds nothing, so ambiguous questions take the general path. |
| `read.query_contract_use` | `[header]` | A03: where a built contract goes. `header` = one lead line for the reader ("Answer type: place:city; expected: one.", only for a known type); `rerank` = the same hint appended to the reranker's query (never the embedder's). Ignored while `query_contract` is `off`. |
| `read.tiers_v2` | `false` | R3-01 (LIB-47): the v2 read entry. On: the question is NFKC-normalised (whitespace collapsed), the viewer is bound to the principal, and a depth tier (fast / standard / deep, with generative-call and rerank-pool caps) is chosen; the tier is recorded in the wiretap and forensics (`read_plan`). Off: unchanged. |
| `read.tiers_v2_depth` | `fast` | R3-01: default depth under `tiers_v2` (`auto` / `fast` / `standard` / `deep`); `auto` picks deep only for join, list and history shapes when a worker model is bound. |
| `read.contract_rules` | `false` | R3-02 (LIB-16): build the typed query contract by rules and put its `[contract] ...` header first in the context. Independent of `read.query_contract` (A03, screened +15/-7 HOLD), which stays available; with both on both headers appear. Off: unchanged. |
| `read.router_rules` | `false` | R3-03 (LIB-23): choose the optional entity / graph / episode legs from a rule `RoutePlan` over the layers the store actually has populated; legs the plan skips do not run even when their own flag is on, and each skip is recorded (`read.router.plan`). Core legs and LLM rewrites (deep tier only) are unchanged. Off: unchanged. |
| `read.freshness_v2` | `false` | O4-01b (O-06). `true` (stamps need `offline.budget_v2`): each search reports, per derived layer, the gap between the namespace's latest raw-turn seq and the minimum `covers_seq` of the layer's stamped records, and flags the layer stale when the gap exceeds `read.freshness_stale_gap`. Unstamped records are counted but never make a layer stale. Metadata only: results are unchanged. Also `Engine.freshness(namespace)`. Trace: forensics `freshness`; tap `read.freshness` (stale, stale_layers, gaps, fallback_added). |
| `read.freshness_stale_gap` | `20` | O4-01b: raw-turn event seqs a derived layer may lag before it counts as stale. |
| `read.freshness_fallback` | `false` | O4-01b: with `read.freshness_v2`, when a layer is stale the raw turns newer than its `covers_seq` join the candidate pool as one more fused leg (newest first), so answers are not missing new facts. |
| `read.freshness_fallback_max` | `20` | O4-01b: raw turns the stale fallback may add to one search. |
| `read.reinjection_penalty` | `0.0` | I64: with a `session_id`, relevance of a record injected in the last `reinjection_window` replies of that (namespace, session) is multiplied by `1 - penalty * uses / window`. 0 = off; no session id = never active. |
| `read.reinjection_window` | `5` | I64: how many recent replies of a session count as "recently injected". |
| `read.perspective_as_of_subject` | `false` | I42: under an as-of read (`as_of`) with a question that resolves to a subject, a superseded record that was current at that time is admitted as history only when it is about a target or carries no subject tags; live records are never dropped. Needs `perspective_mode != off`. |
| `read.profile_subject_card` | `false` | I50: with `profile_slots_header`, the slots block becomes a per-subject card: live `kind:state` facts rolled up per subject (`sub:` tag, else entity), one block per subject the question's persons name (a name, I / my = the asker, `mother@caroline` for "Caroline's mother"), same sensitivity and I33 gates. No resolved person: nothing injected. |
| `read.relative_week` | `calendar` | #58: with `resolve_relative_dates`, the span of "last/past week" and "next week": `calendar` (the previous / next Monday-to-Sunday week, unchanged) or `preceding_7_days` (the seven days before / after the record's own day, LoCoMo's "the week before <session date>"; the label stays an absolute span). Also used by `consolidation.mine_event_dates` and `read.count_dedupe`. |
| `read.resolve_durations` | `false` | C5: with `resolve_relative_dates`, annotate unambiguous durations against the record's own day, always "about": `for 3 years` (with an ongoing cue such as "has been") and `3 years now` / `a month now` → `[= since about 2020]` / `[= since about 2023-04]`; `since 2019` / `since March 2019` → `[= about 4 years as of 2023-05-20]`. Digits and words one..twelve; units year/month (week/day only with "now"). `for years`, `for a while` and finished spans ("stayed for 3 years") are left alone. Off: byte-identical. |
| `read.relative_dates_weekdays` | `false` | I79: with `resolve_relative_dates`, annotate a bare `on <weekday>` against the record's own day by the sentence's tense: a past cue (`won`, `went`, a `-ed` verb) gives the most recent such weekday before it, a future cue (`will`, `going to`, `'ll`) the next one after it (`won it on Friday` → `[= the Friday before 2022-07-10 (Fri 2022-07-08)]` with `relative_dates_anchored`). No clear tense, both tenses, the plural (`on Fridays`) and the record's own weekday are left alone. Off: byte-identical. |
| `read.relative_dates_happened` | `false` | I79: with `resolve_relative_dates`, write each date label as `[= event <date>]`, so the date the event happened reads distinct from the line's leading said-date. Duration labels (`since about 2020`) are unchanged. Off: byte-identical. |
| `read.anticipatory_cues` | `false` | C8': a search hit on a cue written with `Engine.add_cues` resolves to its target record; cues are keys, never content (off: invisible). |
| `read.cue_min_trust` | `0.5` | C8': cues below this trust are ignored, so a low-trust source cannot plant cues that redirect retrieval. Cue trust is capped at the target's. |
| `read.query_encoder` | `none` | #61 (ADR-050): read-time query encoder, no LLM. `none` = no encoder (byte-identical reads). `cues` matches the query against stored anticipatory cues (H8 / `add_cues`) by content-word overlap (a cue matches when the query holds at least half its content words) and adds the cued records as one more fused retrieval leg; a cue below `read.cue_min_trust` is ignored and the cued records pass every read gate. Works with `read.anticipatory_cues` off. A trained (Madeleine-style) encoder is future work. |
| `read.topic_timelines` | `false` | H22 (Mnemon): open the volatile context with one dated timeline per topic entity of the retrieved keyed facts: every live fact on that entity (any attribute) plus its superseded history, oldest first, `(until …)` on ended facts. Built at read time from the stored facts (no model, nothing stored). Entries pass the context gates and must be clean (not instruction-flagged, not below `integrity.untrusted_wrap_below`); an entity with a single entry gets none. Applies to `assemble` and `read` (retrieve / replay), not to shared reads. Pair with mined facts (H2): without keyed facts there is nothing to index. |
| `read.timeline_entities` | `3` | H22: how many entities get a timeline, best-scored first. |
| `read.timeline_items` | `8` | H22: the newest entries kept per timeline. |
| `read.standing_instructions` | `false` | H22: right after the pinned persona, a `USER-STATED PREFERENCES` block of the requests and preferences the user stated ("from now on …", "please always …", "I'd prefer …"), newest five, dated. Only `user`-role records at or above `read.standing_min_trust` that pass the context gates; external documents and tool output never qualify. The block is labelled as the user's words, not system instructions. **Risk:** in the trust-horizon threat model a query-only attacker writes as a user (MINJA); leave this off for agents that serve untrusted users. |
| `read.standing_min_trust` | `0.7` | H22: the least (view) trust of a record shown in the standing block. |
| `read.standing_v2` | `false` | O4-08 (LIB-32, opt-in L5). `true`: a lead block of the precomputed standing answers (`offline.standing_v2`) for the topic entities of the retrieved keyed facts (`select_for_context`), plus the guarded standing instructions (firm user-stated, trust at or above `read.standing_min_trust`, source still live; shown only while `read.standing_instructions` is off, which keeps its own block). An answer whose fact group changed since the build is stale: never shown, the reader uses the live evidence. Read-only. Tap: `compose.standing` (selected, skipped_stale). |
| `read.standing_answers_budget` | `120` | O4-08: words the standing-answer block may use. |
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
| `read.latest_wins` | `off` | I17 (`core/latest_wins.py`): a topic that the retrieved evidence states at several times reads as `[latest]` (newest) and `[earlier statement; a later one on this topic is dated D]` (older). The older value stays in the context as history; the wording is hedged, so two coexisting values (two pets) are not told that one replaced the other. Keyed records match on `entity` + `attribute`; raw turns match when one speaker said both and their content words overlap by `latest_wins_min_overlap`. Records tagged `disputed` (an unresolved contest) get a disputed mark and no ordering claim. `annotate_recent_first` also gathers the marked records, newest first. Off: unchanged. |
| `read.latest_wins_min_overlap` | `0.5` | I17: overlap coefficient (shared content words / the smaller set) at which two raw turns by one speaker count as one topic for `latest_wins`. Both turns must also carry words the other lacks (a differing value, not a repeat). |
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
| `read.asset_evidence` | `"off"` | E04: image evidence for a question that needs what a picture shows (generic cue: what / which / who / where plus an object, title or place word, or "in the photo"). Needs `ingest.assets: on`. `cached`: only evidence already computed (no network, no model); a retrieved turn with an unresolved attachment gets an explicit "unavailable" line. `fetch`: also downloads the turn's own referenced URI once and describes it through the vision port (needs `ingest.asset_dir`). The line is `[image evidence asset:<hash>, turn <id>; …] <description>`; the source's search hint is never used. |
| `read.asset_max_per_read` | `2` | E04: at most this many attachments are resolved for one read. |
| `read.external_evidence` | `"off"` | E03: public knowledge for an invited inference or recommendation question only (would / likely / might / could, "recommend"; never a did / owns / when-did fact question). `cache`: cached results only (no network). `web`: also calls the provider, within `external_max_calls`. Only generic lowercase topic words of the question leave the process (every name, place, number, URL, e-mail, quoted span and capital word of the retrieved context is removed first; a final guard refuses a query that still holds one). The result is added as a `[public knowledge]` block carrying its own clause (it never shows that a person did, owns, visited or said anything), is never stored, and is ignored by the no-record check. |
| `read.external_provider` | `"none"` | E03: `none` (inject one with `Engine.set_external_provider`) `wikipedia` (keyless MediaWiki search plus REST summary, identifying User-Agent, 1 s between requests; titles and URLs are the citations), or `http`: `GET $MEMSPINE_EXTERNAL_SEARCH_URL?q=<query>&n=<count>`, optional credential in `MEMSPINE_EXTERNAL_SEARCH_KEY` (header `MEMSPINE_EXTERNAL_SEARCH_HEADER`, default `Authorization: Bearer`); no key is ever in config. Unset: each call reports `provider_unavailable` and the context is unchanged. |
| `read.external_max_calls` | `20` | E03: network calls the engine may make in its lifetime (cache hits are free; the budget is checked before the call). |
| `read.external_max_results` | `3` | E03: snippets kept per query. |
| `read.external_cache_dir` | `null` | E03: directory of the persistent query cache (`external_cache.json`); null keeps it in memory. |
| `read.external_v2` | `false` | R3-13 (LIB-45): with `external_evidence` `cache` or `web`, the planner decides whether to look up (a generic or explicitly public question, within the call cap), and the snippets (freshness, https, dedupe, per-domain cap, token budget) form one separate labelled `[public]` block with the never-proves clause. Never stored. Does not enable lookups by itself. |
| `read.external_v2_budget_tokens` | `300` | R3-13: token budget of the `[public]` block. |
| `read.cache.enabled` | `false` | R3-15 (LIB-19, ER-31 B): cache whole read results, keyed by scope, viewer, query, config, model ids and store / erasure generations. Every hit is revalidated against the live store (POL-1); a partial revalidation recomputes. Reads naming a `session_id` are never cached. |
| `read.cache.max_entries` | `256` | R3-15: read-cache capacity (oldest evicted). |
| `read.cache.ttl_seconds` | `null` | R3-15: optional read-cache entry lifetime in seconds. |
| `read.parallel_legs` | `false` | R3-15: run the query-only retrieval legs (vector, lexical, metadata, encoder, probes) with `asyncio.gather`; fusion order is unchanged. |
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
| `read.lexical_strip_names` | `false` | Drop speaker names (the `Speaker:` prefixes of the namespace's records) from the BM25 query only; the vector leg is untouched. If nothing is left, the original query is used. |
| `read.session_leg` | `false` | RRF leg `session`: sessions (`group_id`) ranked by the cosine of the query with the mean of their record vectors; contributes the best records of the top sessions. Record vectors are embedded once and cached. |
| `read.session_leg_top_sessions` | `3` | sessions the session leg draws from. |
| `read.session_leg_per_session` | `2` | records the session leg takes from each session. |
| `read.list_mode` | `false` | Gap B1, list / set questions ("What activities does X do?"). For a question matching `list_trigger` the routed replay/retrieve read (a compose-routed aggregation read is not affected) (1) adds the `speaker_vote` RRF leg when exactly one speaker is named: the vector leg fetched `list_vote_depth` deep, kept to that speaker's turns (`speaker:` tag, else the `Name:` prefix), top `list_vote_top_k`; (2) draws `max(candidate_pool, list_pool) x top_k` candidates and skips the `rerank_keep` cut; (3) gives only the best `window_full_hits` hits their neighbour window, the others are single turns. Any other question reads as before. |
| `read.list_trigger` | `set_question` | `set_question` = `query_shape.is_set_question` (what/which/who + a plural set noun, "what kind of", "what do/does/has X ..."); `set_question_wide` (R2-3) = `is_set_question_wide`: that, plus "How did/has/does X <multi-way verb> ..." (promote, support, spend, relax ...) and what/which questions with a plural head noun ("What gifts did X buy?"); `aggregation` = `is_aggregation` or `is_count`; `intent` (I4) = `is_intent_list`, a no-model generic heuristic: aggregation cues (how many, total, so far, all the, each, both, in common, list), plural answer heads, "what kind/type of", enumerating conjunctions; quiet on one-item questions ("most recently", "last", "favourite", "most"). With list mode on, a question naming TWO speakers with a comparison cue ("both", "in common", "each", "share") runs the speaker vote once per speaker (legs `speaker_vote_a`, `speaker_vote_b`; R2-2). |
| `read.speaker_vote_mode` | `name` | I5. Who the list-mode speaker vote is for. `name`: the one speaker the question names. `subject`: that, else the question's subject: first-person "I / my / me" votes on the `user` turns, second-person "you / your" on the `assistant` turns (role from a `user:` / `assistant:` text prefix, else the record's source role; both roles must be in the store); a pronoun or mixed subject casts no vote. `perspective` (I39): the vote on the resolved question perspective (see [Perspective layer](#perspective-layer-i39-i46)): a named participant, the asker, the assistant, or a relation-bound third party; falls back to `subject`. |
| `read.bridge_hop` | `false` | R2-1. After the first search, up to 3 key noun phrases (first two words of a content-word run) of the top 3 hits that the question lacks ("home country") plus the question's subject form ONE extra search of `bridge_hop_top_k` hits, fused into the final search as the `bridge` leg. One hop at most, fails soft; the phrases are recorded as `bridge_phrases` in `search_forensics`. |
| `read.bridge_hop_top_k` | `10` | Hits fetched by the bridge-hop search. |
| `read.bridge_hop_gate` | `"always"` | R2-1b. When the bridge hop fires. `always`: every read (R2-1 behaviour). `cue`: only when the question describes its answer's entity instead of naming it ("home country", "where ... move from", "the studio that X opened", "the book that X recommended", possessive + relation noun such as "her son's"); about 1 of 80 LoCoMo dev questions. `weak`: only when the first pass found one confident anchor without support, i.e. the second-best raw reranker score is under `bridge_hop_weak_threshold` (needs a reranker; with none the hop is skipped). `cue_or_weak`: either. `search_forensics` records `bridge_gate` as `always`, `cue`, `weak` or `skipped`. |
| `read.bridge_hop_weak_threshold` | `0.4` | Second-best raw reranker score under which the `weak` gate fires. Picked offline on the dev forensics: on the 233 screen questions it fires on 31 (13%), including the target question 0-11 (0.349) and none of the 5 reader-drift losses; 0.35 is the edge of that question, 0.5 fires on 40. Tuned on one sample, so confirm on a run before relying on it. |
| `read.agentic` | `false` | I67: opt-in agentic multi-step read. Step 0 is the normal read, unchanged. Then, up to `agentic_max_steps` times, the `sufficiency` role (else `plan`; prompt `sufficiency@agentic`) sees the question and a compact view of the evidence (about 800 tokens) and returns one structured action validated through `structured_call` (with the opt-in `llm.structured` repair and retry): `answer_ready`, `search` (a query) or `search_person_time` (a person and/or a time). It is not native tool calling. Each search goes through the same gated search of the same namespace (the model supplies only text; a namespace field is ignored; firewall-quarantined records never appear). New hits of all steps are RRF-fused, de-duplicated by record id, and appended after the step-0 records under the same token budget. Stops on `answer_ready`, `max_steps`, `no_new`, `budget`, `repeat` or `error` (an error or an unbound role keeps the step-0 read). Every step goes to `search_forensics()["agentic_steps"]` (action, query, why, new record ids, LLM calls and seconds, search seconds) and totals to `["agentic"]` (trigger, fired, stop, llm_calls, new_ids, displaced_ids). Off: no call, byte-identical. |
| `read.agentic_max_steps` | `2` | I67: most action steps per question, 1 to 5. Each step is one LLM call (two with the structured retry) and at most one extra local search. |
| `read.agentic_trigger` | `multi_hop` | I67: when the loop fires. `always`; `multi_hop`: a generic surface heuristic (two or more capitalised names, a relation word such as both / same / before / after / while, a possessive chain, or a bridge cue); `decider`: the `needs_more_evidence` task of the OpenDecider port (needs `read.decider: opendecider` and the task in `decider_tasks`; otherwise, or when unsure or failing, the `multi_hop` heuristic answers). A question that does not fire makes no LLM call. |
| `read.agentic_top_k` | `8` | I67: hits one extra search keeps, 1 to 20. |
| `read.agentic_max_new` | `6` | I67: most new records the loop adds over all steps, 1 to 20. |
| `read.agentic_first_share` | `0.6` | I67: share of the read budget reserved for the step-0 hits, taken in their order. New records may displace only the step-0 records after that share, last first, and only as far as they need room. |
| `read.event_ledger` | `"off"` | A02 read-time arm. `on`: one `extract@events` call (role `extract`) per triggered read over the evidence already in context returns candidate event mentions (actor, action, object, status planned / done / cancelled, exact source span, the quoted time phrase). Code drops a mention whose span is not on its line, resolves the EVENT time only from the quoted phrase (an absolute date, or a relative phrase against the line's own date; never the mention date), links repeated mentions into one event only on identity evidence (the later line names the earlier mention with a quoted cue such as "the same trip" / "as planned" / "finally", same actor, overlapping action or object, no conflicting interval) and shows a compact ledger block: event, state (the latest mention's), event time, mention times, source lines. Raw turns stay authoritative and nothing is stored. Any failure, no valid mention or an unbound `extract` role keeps the read unchanged. Off: no call, byte-identical. Forensics: `search_forensics()["event_ledger"]` (mentions, valid, dropped with reasons, refused links with reasons, events). |
| `read.event_ledger_trigger` | `"event_questions"` | A02: when the ledger call fires. `event_questions`: a date / duration / count question, or one that uses the grammar of plans and outcomes (planned, cancelled, still, already, how many times, before / after ...); `always`. |
| `read.decider` | `"heuristic"` | I28. Who answers the read path's yes/no decisions. `heuristic`: the existing regexes and rules, unchanged and with no model. `opendecider`: OpenDecider-nano (`manjunathshiva/opendecider-nano`, Apache-2.0, ~400M parameters, one forward pass, a calibrated probability, no text generated) through memspine's own inference code (`services/decision/opendecider_nano.py`; the `opendecider` package is not needed). Install the `[decider]` extra (torch, transformers, safetensors, huggingface-hub). A decision below `read.decider_min_confidence`, or any failure (dependencies or weights missing), keeps the heuristic answer. Every decision is recorded in `search_forensics` under `decisions` (task, label, confidence, adapter, `used`, the heuristic label). `Engine.set_decider(obj)` injects any object with `async decide(task, question, context) -> Decision`. |
| `read.decider_model` | `"manjunathshiva/opendecider-nano"` | Hugging Face id or local folder; loaded lazily, once per process and device (0.8 GB download, about 2 GB RAM in fp32). |
| `read.decider_device` | `"cpu"` | `cpu`, `cuda`, `mps` or `auto`. CPU by default because the GPU is usually shared with the reader; one decision takes about 0.2-0.4 s on a CPU, 17 ms on an L40S. |
| `read.decider_tasks` | `["list_mode", "bridge_hop"]` | The decision points that use the decider when `read.decider` is `opendecider`. `list_mode`: replaces the set-question regex (only when `read.list_mode` is on). `needs_more_evidence`: the `decider` trigger of `read.agentic` (input is the question and the first-pass hits; labels `needs_more` / `enough`). `bridge_hop`: replaces `read.bridge_hop_gate` (only when `read.bridge_hop` is on; input is the question and the first-pass hits). The refusal retry is a harness decision point: `--decider opendecider` with `--retry-refusal` in `memspine_evals`. |
| `read.decider_min_confidence` | `0.5` | Confidence (probability of the chosen label) the decider needs for its answer to replace the heuristic. 0.5 uses the calibrated decision as it is (the winning label always has at least 0.5); raise it to fall back to the heuristic more often. A fixed principled cut, not tuned per dataset. |
| `read.decider_threads` | `0` | I38. Intra-op CPU threads of the decider model; `0` = the physical cores. With `decider_workers` > 1 each worker gets `cores // workers`, so the machine is not oversubscribed. Inference always runs under `torch.inference_mode`. |
| `read.decider_backend` | `"torch"` | `torch` (fp32, as the model was evaluated) or `onnx` (the published 8-bit weight-only ONNX build `manjunathshiva/opendecider-nano-ONNX/onnx/model_q8.onnx`, 570 MB extra download, ONNX Runtime CPU provider, graph optimisation ALL; needs `onnxruntime`). Measured speed and agreement with torch are in the GAP_REGISTER row I28. |
| `read.decider_dtype` | `"float32"` | torch weights dtype: `float32` (as the model was evaluated) or `bfloat16` (about 1.5x faster on a 16-core CPU, 300 of 300 decisions identical to fp32, max probability difference 0.0075). |
| `read.decider_workers` | `1` | Concurrent decision calls (a bounded thread pool; the eval harness uses it for many queries). 1 keeps the engine path unchanged. `OpenDeciderDecider.decide_many([(task, question, context), ...])` answers decisions of several tasks in one batched call: inputs are sorted by length and bucketed (at most 16 per forward pass) so short inputs are not padded to long ones. |
| `read.relevance_gate` | `"off"` | I29. Whether retrieved memories are injected at all. `off`: always (today's behaviour). (The `decider` form is retired, G-22.) `store_calibrated` (implemented): the namespace's own off-topic level is learned lazily on its first gated read from a fixed set of 20 generic off-topic probes (`core/relevance_probes.py`), scored through the same vector and/or reranker path and cached per (namespace, embedder, reranker, leg); a message passes only when its RAW top score (vector cosine, or the reranker's raw score, before min-max) exceeds the probes' p95 by `relevance_gate_margin_sd` standard deviations, otherwise the read returns an empty abstained context. Recalibrated when the store has grown past `relevance_gate_regrow` times the calibrated size. Fails open on any error. Recorded in `search_forensics` as `relevance_calibration` (n_probes, mu, sigma, p95, raw_top, threshold, decision). In the harness `--no-memory-prompt` gives the reader a no-memory prompt for the empty context. |
| `read.relevance_gate_margin_sd` | `1.0` | I29/I37. The margin of the `store_calibrated` gate in standard deviations of the off-topic probes' top raw scores, above their 95th percentile. A fixed, portable default for every store, embedder and reranker; not tuned on any benchmark. |
| `read.relevance_gate_leg` | `"auto"` | I29. Which raw score the calibrated gate reads: `auto` = the reranker's raw score when a reranker is configured, else the vector cosine; `vector`, `rerank` force one; `any` passes when any available leg clears its own threshold. |
| `read.relevance_gate_candidates` | `10` | I29. Vector candidates per message (and per probe) that the gate scores; the reranker leg rescores only these. |
| `read.relevance_gate_regrow` | `2.0` | I29. Recalibrate when the namespace holds more than this multiple of the record count it was calibrated on. |
| `read.gate_v2` | `false` | R3-11 (LIB-12, ER-23): replaces the I29/I74 gate by the five-state gate (`answerable`, `conflicting`, `weak`, `missing`, `bypass`), each candidate record scored against the store's own off-topic level (same probes and legs as `relevance_gate`; the probe tops are kept as samples, sigma is 1.4826 x MAD). `missing`: empty abstained context. `weak`: see `gate_v2_weak`. `conflicting`/`answerable`: the context stays whole minus personal records on a generic question (typed contract `personal=false`, dropped below p95 + 3 sigma). `bypass`: enumeration/show-all always; as-of/history only when a candidate scores above the weak threshold; a named reference only when real (see `gate_v2_named`; an unknown name never bypasses). Lead/header blocks and persona records are not judged. Fails open. Forensics `relevance_gate_v2`; wiretap `read.gate.decision`. Off: byte-identical. |
| `read.gate_v2_weak` | `"keep"` | R3-11. `keep`: a `weak` decision passes its records (flagged `low_confidence` in the forensics). `abstain`: a `weak` decision returns an empty abstained context like `missing`. Only acts with `gate_v2`. |
| `read.gate_v2_named` | `"hit"` | R3-11. What makes a named reference real. `hit`: the name resolves in the store AND a candidate mentions it or scores above the weak threshold. `scored`: a candidate must score above the weak threshold. Only acts with `gate_v2`. |
| `read.deep_v2` | `false` | R3-12 (LIB-43, R-22 B): the deep tier after the first pass. Rules first (every obligation met: zero calls), then one sufficiency check (`sufficiency` role, else `plan`), at most one rewrite (`sufficiency@missing`), one extra retrieval through the gated `_search` of the same namespace; never a second round. New records join under the agentic merge budget (`agentic_first_share`, `agentic_max_new`). Unbound role: zero calls. Forensics `deep_v2`; wiretap `read.deep.step`. Off: no call, byte-identical. |
| `read.obligations` | `false` | R3-16 (LIB-15, ER-19): derive evidence obligations (single, join, duration, list, count, comparison, latest) from the typed query contract and check them against the read's keyed semantic records (raw turns are not claimed as facts). No model call. Forensics `obligations` (verdict, holes, caveats); wiretap `read.obligations.report`. Also feeds the rule check of `deep_v2`. Off: nothing runs. |
| `read.relevance_gate_bypass` | `"named"` | I74. Personal-reference bypass of the relevance gate (acts only when `relevance_gate` is on). A message that names the store's own people or entities (a participant of the namespace, or a capitalised name present in the store; the I60 `entity_check` rules: exact, or a 3+ letter prefix) needs memory, so the gate is skipped and the memories are injected as normal. `none`: no bypass. `named`: names only. `named_or_first_person`: also "I / my / me" (separate option, since OP-Bench baiting probes are first-person). Recorded in `search_forensics` as `relevance_bypass` (mode, fired, kind, names). |
| `read.abstain_on_raw` | `false` | I30. Judge `assembly.theta_abstain` and `assembly.relative_floor` on the reranker's RAW scores (before min-max), so they work under rerank where min-max makes the top candidate 1.0. Raw scores are model-specific (the floor assumes non-negative scores); the portable alternative is `relevance_gate: store_calibrated`. Recorded as `abstain_on_raw` in the forensics. |
| `read.naive_timezone` | `"UTC"` | F5. The IANA zone a naive datetime (`valid_from` or a message `timestamp` without an offset) is read in. Default UTC as before, with a one-time `write.naive_datetime` warning; aware values are never changed. |
| `read.session_sequence` | `false` | F5. When several turns of one session carry the same event time, the n-th is stored n microseconds later so event-time order within the session equals write order. Off: stamps are stored as given. |
| `read.sensitivity_gate` | `"off"` | I52. Graded-sensitivity read bar (`core/sensitivity.py`). `off`: as today. `on`: a record graded `medium` or `high` at write time (`write.sensitivity`; a W16 `sensitive:*` tag counts as a grade) is injected only when the question is about its category (a category cue in the question), names its subject (`medium` only) or shares enough content words with it (1 for `medium`, 2 for `high`); `none` and `low` are never gated. Applied in every leg and in `full`/`replay` neighbours, so a generic question ("recommend a gift for my sister") never pulls in the user's gender identity. `decider`: as `on`, and when the question names no sensitive topic the decider task `sensitivity_scope` (`read.decider: opendecider`) may open the gate when sure; it never closes it. |
| `read.inferred_gate` | `"off"` | I48. `on`: a record tagged `src:inferred` (`write.inferred`) is used only once it has `read.inferred_min_support` distinct supporting user turns (`support:<turn id>` tags). `Engine.review_inferred(namespace)` lists the ones still short. |
| `read.inferred_min_support` | `2` | I48: distinct user turns an inferred record needs before a read uses it. |
| `read.list_vote_depth` | `100` | how deep the vector leg is fetched for the speaker vote. |
| `read.list_vote_top_k` | `30` | hits the `speaker_vote` leg contributes to fusion. |
| `read.list_pool` | `3` | candidate pool (x `top_k`) of a list-mode read. |
| `read.window_full_hits` | `10` | list mode only: replay hits ranked beyond this get no neighbour window (single turns); `null` = every hit keeps its window. |
| `read.graph_node_search` | `false` | GR-6: an RRF leg (`graph_nodes`) of the records that mention the entity nodes best matching the question (cosine on embedded names + text match, fused in the graph store), best entity first. Needs `graph.entity_embeddings` and associative entity nodes. |
| `read.view_tag_leg` | `false` | G-16: an RRF leg (`view_tags`) of records whose location / topic / person view tags (`loc:` / `topic:` / `person:`, from `consolidation.mine_multiview`) share words with the question. Ranked by most shared words, then location over topic over person. |
| `read.gist_after` | `None` | G-22 (SimpleMem pyramid retrieval): the best N hits keep their full text, and the rest are shown as their one sentence sharing most words with the question. This happens before the budget fit, so more distinct evidence fits. Never applied to verbatim questions. |
| `read.session_digest` | `false` | N31: a read header (`SESSION DIGESTS`) with one line per episodic session of the first `SESSION_DIGEST_HITS` (5) hits, up to `SESSION_DIGEST_SESSIONS` (3): the `SESSION_DIGEST_SENTENCES` (2) sentences sharing most content words with the question, within `SESSION_DIGEST_SHARE` (10%) of the budget. Unlike other headers it does not hide the quoted turns from the read. |
| `read.facts_to_sources` | `false` | N54: a mined fact (`atomic_fact`) in the hits gives its rank and score to its live source turns (its parents); a turn already listed is not repeated, and a fact whose sources are gone keeps its slot. |
| `read.type_quotas` | `{}` | N55: the most records of each memory type a read keeps, e.g. `{semantic: 3}`; types not listed are uncapped. |
| `read.recent_exchanges` | `0` | C2: a read header (`RECENT CONVERSATION`) with the namespace's last N live episodic turns, oldest first, within `RECENT_SHARE` (15%) of the budget. A turn identical to the question (the in-flight message) is left out, and the turns stay readable in the retrieved part. |
| `read.leg_min_scores` | `{}` | C6: per-leg score floors applied before fusion, e.g. `{vector: 0.3, lexical: 1.0}` (vector = cosine, lexical = BM25). Weak hits never enter the fusion. |
| `read.section_captions` | `false` | C7: when read headers lead the context, the retrieved part gets its own caption (`RETRIEVED_CAPTION`), so the reader sees which part is which. |
| `read.short_query_lexical_weight` | `None` | N33: the BM25 leg's RRF weight for a question of at most `SHORT_QUERY_WORDS` (5) content words. |
| `read.entity_leg` | `false` | N59: an RRF leg of raw turns that name the question's proper nouns and years, beyond the conversation's speakers. Ranked by names matched, then content words shared, then time order. |
| `read.entity_leg_v2` | `false` | R3-05a: entity leg over resolved entity ids (associative entity index) with the Mem0 boost (max over matched entities, capped at 0.5) and hub damping; joins fusion as `entity_v2`. |
| `read.legs_v2` | `false` | R3-04 (R-05/06/07): candidate generation through the leg registry (`memspine.read.legs`): `vector`, language-aware BM25 `lexical` (runs when `hybrid` is on, replaces the tantivy leg) and `temporal` (event intervals in the anchored window, replaces `temporal_leg`). An empty leg reports a reason (wiretap `read.leg`), never an error. |
| `read.temporal_precision_aware` | `false` | W2-14 (R-07, M-10; needs `read.legs_v2`): the `temporal` leg reads the precision the write path stored. A `happened:` period still matches any window it overlaps, but its score is weighted by the share of the period the window covers, so a day-precision fact outranks a year-precision one for a day question. Validity intervals are not weighted. `false` (default): unchanged. |
| `read.lexical_scorer` | `bm25` | R3-04: the scorer behind the `lexical` leg under `legs_v2`; only `bm25` today, others register through `memspine.read.legs.register_scorer`. |
| `read.lexical_language` | `en` | R3-04: default tokeniser language of the `lexical` leg under `legs_v2` (ISO 639-1 stop words and Snowball stemming; other values: no stemming; CJK is always character bigrams). |
| `read.episode_leg_v2` | `false` | R3-05c (R-10; needs `read.legs_v2`): an `episode` leg (family `derived`, layer L2) searches episode records by vector and BM25 and lifts each hit to its member turns through lineage (`source.parents`, or the lineage table under `write.lineage_table`); fusion receives turn ids. Lifted turns pass the read gates again (status, quarantine, consent, erasure, date and scope), so forgotten or erased turns are dropped. A namespace with no L2 record gives an empty leg (`no_records:L2`). Wiretap `read.leg` and `read.lift`. |
| `read.graph_leg_v2` | `false` | R3-05b (R-09; needs `read.legs_v2` and `graph.edge_compiler_v2` with a sleep that compiles edges): the graph one-hop leg `OneHopConvergent` (family `structured`, layer L3). From the query's entity nodes plus the entities of the best base hits it walks one hop over the compiled edges, drops tentative ones, and keeps nodes reached from at least `read.graph_leg_min_seeds` seeds; the records on the connecting edges are lifted to their evidence turns through lineage and re-gated (consent, erasure, scope). Empty with `no_records:L3` when L3 is not populated. Wiretap `read.leg` and `read.graph_hop` (seeds, reached, convergent, dropped_tentative). |
| `read.graph_leg_min_seeds` | `2` | R3-05b: distinct seeds that must reach a node for it to be convergent. |
| `read.graph_leg_seed_hits` | `3` | R3-05b: how many of the best base hits add their entity as a seed (0 = query entities only). |
| `read.graph_leg_lift_max_turns` | `8` | R3-05b: the most evidence turns one reached record lifts to. |
| `read.graph_leg_carry` | `0.9` | R3-05b: score carry of the lift to evidence turns (`score * carry ** hops`). |
| `read.episode_lift_max_turns` | `8` | R3-05c: the most member turns one episode hit lifts to. |
| `read.episode_lift_carry` | `0.9` | R3-05c: a lifted turn scores `episode_score * carry ** hops`; the best episode wins when several lift the same turn. |
| `read.lexical_namespace_languages` | `{}` | R3-04: per-namespace language override for the `lexical` leg, e.g. `{"ns-de": "de"}`. |
| `read.timeview_v2` | `false` | R3-14: as-of reads through the three-valued time view (event time for true-at, recorded time for known-at); undecidable records are kept and flagged, not dropped. |
| `read.neighbours_v2` | `false` | R3-10: neighbour windows after rerank via the neighbours library: budget-aware, viewer-checked, overlapping windows merged; replaces the G2c expansion. |
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
| `read.reference_edges` | `off` | A07: reply / reference edges inferred from the turn order. `on`: in `write_messages` a short reply from another speaker, and a turn that opens with a deictic reference (`this`, `that`, `these`, `those`, `we`, or a short frame such as "like that"), is linked to the immediately preceding turn through `reply_to:<id>` plus a `ref:short_reply` / `ref:deictic` tag (an explicit `reply_to` is never overridden). In a replay read the antecedent of a retrieved turn that carries a `ref:` tag is attached (same namespace, within the budget, at most `reference_max_per_read`); every decision lands in the forensics as `reference_edges` (attached / present / over_cap / over_budget / missing). Off: byte-identical. |
| `read.reference_short_words` | `6` | A07: a turn of at most this many words (speaker prefix excluded), by another speaker than the previous turn, counts as a short reply (1-20). |
| `read.reference_max_per_read` | `4` | A07: the most antecedents one read attaches (1-16). |
| `read.entity_summaries` | `false` | GP-6 (#17): after the cards header and the graph facts, an `ABOUT` block with one `About <Name>: …` line per entity the query names (the graph leg's seeds) that has a live entity summary (`memories.associative.policies.entity_summaries`). Summaries pass the graph admission gate (status, quarantine, `graph_min_trust`, integrity) and the context wrappers. Shares `cards_budget_share` and counts toward the header-share check; the shown summaries are left out of the read below. Off: byte-identical. |
| `read.graph_communities` | `false` | GP-9 (#23): community summaries (`reorganize` parents) are read only when a seed entity of the graph leg is a member of the community, that is, when one of the records the entity mentions is a member (communities are built over association edges, `mentions` excluded). Every other community summary is dropped from search results; with `graph_leg` on, the admitted ones join the graph leg. Seeds as for `graph_leg` (query names, else the entities of the best 3 hits). Off: byte-identical. |
| `firewall.enabled` | `true` | `false` keeps trust scoring but disables flagging, anomaly checks and quarantine: the N1 ablation arm only. |
| `firewall.skip_message_roles` | `[]` | H21: `write_messages` never deposits turns with these roles (e.g. `["system", "tool"]`). |
| `firewall.skip_injected_recall` | `false` | H21: never re-deposit a turn carrying memspine's own assembly markers (recalled memory echoed back into the conversation). |
| `firewall.tag_assistant_claims` | `false` | H21: tag assistant turns `assistant_claim`, a proposal rather than an observed fact. |
| `firewall.hearsay_trust_cap` | `null` | I43: a record carrying a `rep:` tag ("my mom said X", "I heard X"; needs `memories.episodic.policies.perspective`) gets at most this trust, so it ranks below a first-hand statement and everything derived from it inherits the cap through the parent trust. None: off. |
| `firewall.gates_v2` | `false` | W2-04 (LIB-38): the Memory Firewall at its gates through `trust.gates`. Gate 1 screens each write (instruction signals in en/de/nl/es, MINJA recall-phrase bridge, same trust matrix and quarantine rule); gate 2 filters quarantined or unreadable turns from extraction input; gate 3 holds a derived record whose parent is quarantined, unreadable or not live, or whose own text is instruction-shaped (stored inert, trust still capped by the least-trusted parent); gate 4 admits evidence at read; quarantine release also needs an independent person, operator or system family (S4-4). Needs `firewall.enabled`. Wiretap: `trust.gate`. Off: unchanged. |
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
| `firewall.signals.minja_bridge_exempt_roles` | `[]` | I21: roles whose writes skip the prefix-repeat signal. Chat assistants repeat templated openers ("Sure! Here's ..."); on a synthetic chat 39 of 40 such turns were quarantined. `[assistant]` is the chat-data setting (the `chat_roles` preset); it removes the prefix defence for that role. |
| `firewall.signals.anomaly_exempt_roles` | `[]` | I21: roles whose writes skip the embedding-outlier signal. |
| `firewall.signals.minja_bridge_prefix_chars` | `null` | I21: the shared-prefix length the bridge signal compares (`null` = 96). |
| `firewall.sensitive_topics` | `false` | W16 (ADR-059): tag GDPR art. 9-style topics (health, religion, sexual orientation, politics, ethnicity, legal, financial hardship; `core/sensitive.py`) as `sensitive:<topic>` and raise `pii_tier` to at least `high`. The text is kept. |
| `write.sensitivity` | `"off"` | I52. Grade each record `none`/`low`/`medium`/`high` with a category (health, sexuality_gender, religion, ethnicity, credentials = high; politics, finance, legal, location = medium) and tag it `sens:<grade>` / `sensc:<category>` (labels only: the text is never copied into a tag, log line or forensics entry; a `high` record also gets `pii_tier` >= `high`). `heuristic`: the fixed English lexicon. `decider`: the lexicon, raised to `medium` (category `other`) by the decider `sensitivity` task when it is sure (`read.decider: opendecider`); the decider never lowers a grade. |
| `write.participants` | `"off"` | I53. `session`: `write_messages` tags each turn `participant:<name>` for every speaker of the call (the turn's own `speaker`/`name`, else its role, first) and honours a turn's `visibility`; a derived record (mined fact) inherits its parents' participants and the strictest visibility. `write(participants=, visibility=)` sets them directly (`private` = the speaker only, `participants`, `owner_shared`). They matter only to a read with `viewer=`; the namespace stays the hard isolation boundary. |
| `write.inferred` | `"off"` | I48. `on`: tag an engine-derived record (mined, reflected or consolidated, or assistant-proposed with parents) `src:inferred` unless one user turn already contains `write.inferred_explicit_overlap` of its content words (then it is the user's statement), cap its trust at `write.inferred_trust_cap` and record the distinct supporting user turns as `support:<id>` tags. Pair with `policies.conflict.inferred_defers` so an inference never overrides a stated fact. |
| `write.inferred_trust_cap` | `0.4` | I48: trust ceiling of an inferred record (user-stated 0.7, assistant 0.5). |
| `write.inferred_explicit_overlap` | `0.6` | I48: share of a derived fact's content words one user turn must contain for the fact to count as stated, not inferred. |
| `write.inferred_support_overlap` | `0.3` | I48: share of its content words a user parent turn must contain to count as support. |
| `write.window_extract` | `"off"` | W1 adaptive-window write, call 1. `facts`: one structured `window_extract` call per window of consecutive turns yields atomic, dated, grounded facts (a verbatim `span` of a cited turn), deposited as semantic `atomic_fact` records with the cited turns as parents. `facts_graph` adds the entity-edge section (handed to the sleep stage `extract_graph`); `all` adds the episode-fragment section. Needs `llm.roles.window_extract` and `memories.episodic` + `memories.semantic`. Run with `Engine.flush_windows()` (batch) or in the background (`window_async`). Counters: `Engine.window_stats()`. |
| `write.window_max_turns` | `6` | W1: a window closes at this many turns. |
| `write.window_max_tokens` | `1500` | W1: a window closes before the turn that would pass this many prompt tokens (4 characters each). |
| `write.window_topic_k` | `2.0` | W1: a window closes at a topic shift: the similarity of two consecutive turns falls below the store's own running mean minus k standard deviations of that similarity (per namespace, learned from at least 20 pairs, no LLM). 0 turns the topic boundary off. |
| `write.window_idle_s` | `120.0` | W1 (async mode): seconds without a write in a namespace before its open window is cut. 0: never. |
| `write.window_split` | `"auto"` | W1: `auto` splits a window's call into facts+edges / episode after repeated parse failures of the joint call; `always`; `never`. Only matters with more than one section. |
| `write.window_async` | `true` | W1: `true` runs window calls in a background task after the write returns (idle debounced); `false` runs them only at `flush_windows()`, `close_session()` or `sleep()`. Same result either way. Ignored (batch) on `:memory:` SQLite. |
| `write.window_cache` | `true` | W1: cache each call by (model, prompt version, sections, window text), so a re-run on the same history makes no call. A forget deletes the cached replies of windows that held the erased turn. |
| `write.intake_v2` | `false` | W2-01 (LIB-30; W-01, W-02, W-03, W-24). `true`: `write_messages`, `write_episode` and `ingest` go through one intake gateway. Each call is a `SourceEnvelope`; every segment gets an S3-3 write id (scope, source, version, position, content), so a retry (a re-sent batch, or a re-send after a crash) writes nothing new and returns the original receipt. A call commits one ledger transaction (tables `intake_source`, `intake_write`, `intake_intent`, created on first use) and then runs the old write door on the new segments only, each tagged `idem:<write id>`; `start()` finishes any write that was committed but not applied, so a crash at any boundary gives exactly one record per segment. An `ingest` of an edited document is a new source version: unchanged chunks stay, changed ones are written, and vanished or changed chunks are archived together with the derived records that have no other live parent (records that keep a live parent are re-pointed; with `write.lineage_table` the edges are dropped and a `version_of` edge is added). The receipt carries readiness per capability and never claims one that is not configured: `raw_search` is `ready` at return; `facts`, `graph`, `summaries` are `pending` only when the window pipeline makes them and become `ready` only when the `workers.outbox` barrier covers the write and none of its turns sits in an open window (without the outbox they stay `pending`); `profile` is `unavailable`. Taps `write.intake.*`. `false` (default): the old write path, unchanged. |
| `write.asset_describer` | `off` | W2-11 (W-23). `off` (default): `Engine.ingest_asset` is refused and nothing else changes. `ingest`: `ingest_asset(path_or_uri)` sends an `asset` envelope through the intake gateway (needs `write.intake_v2`), stores the asset REFERENCE (content hash, path/URI, mime, size; never the bytes) as a `resource` record tagged `asset_ref`, and then writes a description as a derived record (`channel: asset_description`, parent = the asset record, lineage edge when `write.lineage_table`, trust capped at 0.6 for model text) through `derive/write.py`. `on_read`: the reference is stored without a description; `Engine.search` describes a returned asset record that has none, once (the description is a record, so it is cached), and places it after its asset. An ingest description wins; no two descriptions share (asset hash, model, prompt hash). The describer is the optional LLM role `describe_asset` (a vision-capable model; the image goes as an `image_url` part for a URL or a local file up to 5 MB); with no role bound it writes a metadata-only description (filename, mime, size, alt text, EXIF image description) and makes no model call. Erasing the asset record erases its descriptions (derived cascade). Taps `write.asset.*`. |
| `write.asset_describe_timeout_s` | `3.0` | W2-11. Longest an `on_read` search waits for asset descriptions. Past it the search returns without them and the work continues in the background (one queued task per asset; cancelled at `stop()`). |
| `write.stamping_v2` | `false` | W2-02 (LIB-29; W-04, W-05, W-08, M-08). `true`: every raw turn written by `write_messages` / `write_episode` is stamped. Three clocks: `source_at` from the turn's own `timestamp` (never "now"; an undated turn stays undated), `source_tz` from the turn's `tz` / `timezone` key, else `unknown`, `recorded_at` the engine clock. A naive time with no zone is kept as wall clock and recorded as `source_tz = unknown` (flagged `tz_unknown`; it is not shifted by `read.naive_timezone`); a future-skewed or out-of-order time is flagged and kept. Speaker, addressee and a `participants` list become typed columns (`spk`, `addr`, `participants`; participants are filled from the call's speakers, a turn without a name counts as its role, on by default). The provenance class comes from the role (`user_stated`, `assistant_proposed`, `tool`, `document`, `external`). A user turn that answers an assistant turn of the same call goes through the assent check: a real yes ("yes please", "sí, adelante", "ja, genau") is tagged `assent_of:<assistant record id>` for the derivation step to promote the proposal to `user_confirmed`; "ok, thanks", a refusal, a question or a conditional yes is not. Runs on the direct path and inside the intake gateway (`write.intake_v2`). The event interval and its precision are filled at derivation, not here. New columns `source_tz`, `participants` (Alembic `w202_stamping`). Taps `write.stamp.turn`, `write.stamp.assent`. `false` (default): turns are stamped as before. |
| `write.stamp_participant_tags` | `v1` | W2-02 (W-05). With `write.stamping_v2`: `v1` also writes the v1 `participant:<name>` tags (kept for the dual-read period); `none` writes only the typed column. The tags feed the `viewer` read filter (a record with participants and no visibility tag is a participants-only record), so a deployment that reads with `viewer=` can set `none` and rely on the typed column. No effect while `write.stamping_v2` is `false`. |
| `write.stamp_default_tz` | `null` | W2-02 (W-04). With `write.stamping_v2`: the zone a naive source time with no declared zone is read in as wall clock (the record is still marked `unknown`). `null`: `read.naive_timezone`. |
| `write.role_policy_v2` | `false` | W2-03 (LIB-13; W-06, W-09, I-8). `true`: raw storage keeps every role, and derivation reads only what the role allows. A system or tool turn is written (not skipped by `firewall.skip_message_roles`) and tagged `derive_use:excluded` / `derive_use:evidence`; user and operator turns are `stated`, assistant turns `proposal`. Window extraction (`write.window_extract`) and the session-close and session-stage inputs drop system turns and echoes (`turns_excluded_excluded` / `turns_excluded_echo` in the window counters); a fact backed only by assistant or tool turns is tagged `derive_use:proposal` / `evidence`. The raw record's provenance class comes from the role (assistant `assistant_proposed`, tool `tool`) and its trust is capped at the user-stated level, never raised. Echo guard: a non-user turn that carries a recall marker (the engine's own context wrappers or `wrap_recall`) or overlaps a memory the session's last read returned (max of Jaccard and containment of 3-token shingles, at least `write.echo_threshold`) is tagged `echo` and `echo_of:<id>` and is never re-derived as a new fact; a user turn that restates a shown memory is tagged `assent_candidate` and never counts as support. Needs a `session_id` on both the read and the write for the overlap check. With `write.intake_v2`, a turn that carries `entity` + `attribute` (optionally `value`), or `memory_type` skill / plan, is also written as a typed record with the raw turn as its parent (tag `routed`, idempotent on resume); unkeyed turns stay raw. Counters on `engine._role_metrics`. Taps `write.role.*`, `write.echo.*`, `write.route.*`. `false` (default): unchanged. |
| `write.echo_threshold` | `0.6` | W2-03 (I-8). With `write.role_policy_v2`: the shingle overlap at or above which a turn counts as an echo of the session's last read. |
| `write.segmentation_v2` | `false` | W2-05 (LIB-14; W-11). `true` (needs `write.window_extract`): windows also cut at a source-time gap and a calendar-day change, and grow adaptively (a window past `write.window_max_turns` keeps taking short turns until `write.segment_grow_tokens` or `write.segment_max_turns`; `write.window_max_tokens` always binds). Closed windows of one namespace wait until `write.batch_max_tokens` / `write.batch_max_windows` / `write.batch_max_age_s` and then share ONE extraction call, each window a separately numbered block; grounding stays per window (an item citing two windows is dropped and counted), episodes stay one call per window. With `workers.outbox` each window is still its own durable row, so a crash loses nothing. A `flush_windows`, `close_session` or `sleep` never holds windows back. Taps `write.segment.*`, `write.batch.*`. `false` (default): the old segmenter and one call per window, unchanged. |
| `write.segment_gap_s` | `1800.0` | W2-05. Seconds of source-time silence that cut a window (0: no gap cut). Only with `write.segmentation_v2`. |
| `write.segment_calendar_day_cut` | `false` | W2-05. Also cut when the source date changes. Only with `write.segmentation_v2`. |
| `write.segment_grow_tokens` | `400` | W2-05. A window past `write.window_max_turns` keeps growing until it holds this many (estimated) tokens. Only with `write.segmentation_v2`. |
| `write.segment_max_turns` | `24` | W2-05. Hard turn cap of a growing window (never below `write.window_max_turns`). Only with `write.segmentation_v2`. |
| `write.batch_max_tokens` | `3000` | W2-05. Token budget of one batched extraction call. Only with `write.segmentation_v2`. |
| `write.batch_max_windows` | `8` | W2-05. Most windows in one call; 1 turns batching off. Only with `write.segmentation_v2`. |
| `write.batch_max_age_s` | `30.0` | W2-05. Longest a closed window waits for company before its batch goes out (seconds). Only with `write.segmentation_v2`. |
| `write.envelope_v2` | `false` | W2-06a (LIB-05; W-14, W-15, P-05, P-06). `true` (needs `write.window_extract` with `facts`): the `facts` section is extracted as `ItemV1` items (compact keys; content only; each cites the numbered lines it came from) and every item is validated alone and compiled into a stored `AssertionEnvelope` (time axes from the turn stamping, provenance class and trust from the cited line's role, evidence roles support / binding / context). An invalid item (no citation, a line out of range, only CONTEXT lines cited, a reserved system name such as `trust` or `user_id`) is dropped and its siblings are kept. At most ONE repair re-ask per extraction call carries only the invalid items and their typed errors; an item still invalid goes to the persisted dead-letter list (reason code, attempts, item hash; no content unless the tap content opt-in is on). A new kind is accepted with a safe default (stored as `event`, so it never supersedes) until 64 kinds exist, then it becomes `other`. The repair call goes through the LLM router, so `economy.ledger_v2` counts it. `edges` / `episode` keep their own prompt. Taps `write.item.valid`, `write.item.repaired`, `write.item.dead`. `false` (default): the old fact section, unchanged. |
| `write.temporal_v2` | `false` | W2-14 (LIB-01; W-04, M-10, R-07; needs `write.envelope_v2`): temporal anchoring as an envelope stage (order 100). For each assertion it fills the event interval with its precision and basis by the anchor ladder: (1) an explicit date in the model's time text, the cited span or the assertion text; (2) a relative phrase ("last Tuesday", "letzte Woche", "il y a trois jours", "hace dos semanas") resolved against the cited turn's `source_at` (`write.stamping_v2`), else the window's nearest stamped turn (basis `source_time`); (3) with `write.stamping_v2` off only, the window `valid_from` as an approximate `inferred` session anchor; (4) else unknown, never "now". Precision is never finer than the evidence ("in 2019" is year precision, never a made-up day). `before` / `after` / `during` / `since` / `until` between two assertions of one window ("the week before her trip") are stored as a typed relation with no date (`since` as `after`, `until` as `before`; the spoken word is kept in `extras.temporal`). The deposited `happened:` label carries the written precision. Tap `write.temporal` (rung counts). `false` (default): the model's time text is kept as text only. |
| `write.lineage_table` | `false` | F-16a (M-09, G-08, ER-22, POL-2). `true`: every derived record written through the derived-write door (`derive/write.py`: mined and window facts, calibration, cues, list cards, episode fragments, lessons) gets one `lineage` row per parent (role `support` unless the site names one), its trust capped at the least-trusted parent, `layer:` and `deriv:` tags, and is refused when it has no parent. The lift stage reads the table when `read.lift_before_rerank` is also on. Taps: `derive.lineage.write`, `derive.lineage.refused`. `false`: nothing is written to the table. |
| `offline.patterns_v2` | `false` | O4-05 (LIB-31, opt-in L4). `true`: the `pattern_observations` sleep stage (after `reflect_profile`; no model call) mines recurrence, frequency, trend, co-occurrence and preference-stability observations from the stored mined facts and writes each as a derived semantic record (channel `observation`, tags `observation:<kind>`, `obs:<id>`) through the derived-write door: parents = the supporting facts, trust capped at the least-trusted parent. A supporting fact that is no longer live invalidates its observations by group; an observation the next run no longer produces is retired. Observations are never inputs of a later run. Note: identical repeated sentences are merged by the write-time dedup, so a recurrence needs distinct wording or `happened:` dates. Tap: `job.patterns` (by_kind, emitted, invalidated, retired). Cost: 0 model calls. |
| `offline.patterns_kinds` | `[]` | O4-05: observation kinds to mine (`recurrence`, `frequency`, `trend`, `co_occurrence`, `preference_stability`); empty = all five. |
| `offline.patterns_max_new` | `50` | O4-05: new observation records per namespace per sleep; the rest wait a cycle. |
| `offline.standing_v2` | `false` | O4-08 (LIB-32, opt-in L5). `true`: the `standing_answers` sleep stage (after `pattern_observations`; no model call) builds one answer per keyed state fact (current value, from value histories) as a derived semantic record (channel `standing`; tags `standing_answer`, `saq:`, `saf:` fingerprint), rebuilds only the answers whose group fingerprint changed, and evicts idle or surplus ones (`eviction_candidates`). Parents = the supporting facts, trust capped at the weakest. Answers are caches: losing one costs a rebuild. Read side: `read.standing_v2`. Tap: `job.standing` (built, stale, evicted). Cost: 0 model calls. |
| `offline.standing_max_answers` | `50` | O4-08: standing answers kept per namespace; new answers only fill the room left. |
| `offline.standing_idle_after` | `5` | O4-08: answer-set revisions without a read hit after which an answer is an eviction candidate. Applied only once this process has logged reads (the hit log is process-local), so a cold start never evicts by idleness. |
| `write.feedback_v2` | `false` | O4-11 (LIB-44, O-25). `true`: `Engine.feedback` takes explicit feedback only. The `channel` (default `api`) must be `user_message`, `ui_control`, `operator_edit` or `api`; silence, dwell, regeneration, retry, copy, click, abandonment and classifier signals are refused. The actor must be the memory owner (`user` or the namespace) or a name in `write.feedback_operators` (never for a confirmation); an agent (`assistant`, `agent`, `system`, `tool`) never may. Adds the signals `correction` (`new_value`, optional `new_text`), `rejection`, `confirmation` and `rating` (`rating` 1..5, logged only) to `like` / `dislike` / `note`. A correction, rejection or confirmation never edits a record: it becomes a bound assertion (the owner's new value, a negation of the recalled one, a restatement) written through the write door and the conflict ladder; the first written record is returned. Adds `Engine.correction_latency` (reads between a wrong memory first being surfaced and its correction, from the log; needs `event_log.mode` other than `ephemeral`). Taps: `api.feedback` (kind, channel, accepted, reason, assertions), `api.feedback.latency`. `false`: the like / dislike / note verb as before, and the new keywords are refused. |
| `write.feedback_operators` | `[]` | O4-11. Principals that may give operator feedback (corrections, rejections, ratings) under `write.feedback_v2`. |
| `offline.communities_v2` | `false` | O4-06 (LIB-36). `true`: `reorganize` clusters the entity graph (deterministic label propagation over entity pairs co-mentioned by live records; hubs kept out; stable community ids) and rewrites a summary only for a created or changed community, planned inside `offline.community_summary_budget_tokens` (the rest stay dirty); each summary is a `community_summary` record (`com:` and `cm:` tags, parents = the member records, trust at most the least-trusted parent). `summarize_entities` makes its model call outside the namespace lock and writes through `derive/write.py`. Extra model calls: one `summarize` per planned community of 3 or more entities, plus the existing entity batches. Taps: `job.communities`, `job.communities.summary`, `job.communities.entities`. `false`: today's record-level reorganizer and summariser. |
| `offline.community_summary_budget_tokens` | `20000` | O4-06: token budget of the community summary jobs one sleep may plan (each costs about 200 + 40 per assertion read). |
| `offline.community_min_size` | `3` | O4-06: communities with fewer entities get no summary. |
| `offline.community_max_size` | `200` | O4-06: a larger community is flagged oversized and gets no summary (the label-propagation collapse guard). |
| `offline.community_hubs` | `[]` | O4-06: canonical entity names kept out of clustering (the owner self-entity). |
| `offline.community_hub_fraction` | `0.5` | O4-06: an entity linked to at least this share of the other entities (graphs of 10 or more entities) is a hub; `0` turns the rule off. |
| `offline.community_max_entities_per_record` | `8` | O4-06: a record naming more entities than this adds no entity-pair edges (counted as `wide_records`). |
| `offline.decay_v2` | `false` | O4-09 (LIB-33). `true`: `decay_sweep` scores each record (recency, frequency, importance) and moves its tier with hysteresis; protected records (tags `pinned`, `standing`, `contested`, or a kind in `offline.decay_protected_kinds`) are never moved colder, legal-held and quarantined ones are not touched; `compress` also plans multi-resolution text jobs (paragraph, sentence, keyword) for cold groups. Nothing is deleted and tiers do not change ranking (O-22). Taps: `job.decay`, `job.decay.compress`. `false`: absolute idle-day tiers and zstd-only compress. |
| `offline.decay_boundaries` | `[0.5, 0.3, 0.15]` | O4-09: activation boundaries hot/warm, warm/cold, cold/dormant (strictly decreasing). |
| `offline.decay_margin` | `0.05` | O4-09: activation must pass a boundary by this much before a record moves back up (smaller than the gap between boundaries). |
| `offline.decay_protected_kinds` | `[]` | O4-09: memory types never moved to a colder tier. |
| `offline.decay_multires_jobs` | `0` | O4-09: multi-resolution jobs one sleep runs (each is one `summarize` call, outside the lock; the shorter levels are cut from the paragraph). `0` plans and reports only, no model call. |
| `offline.decay_group_min_size` | `2` | O4-09: a cold group needs at least this many eligible members. |
| `offline.reconcile_v2` | `false` | O4-03 (LIB-37, O-10..O-13, X-05). `true`: the sleep cycle runs a `reconcile` stage after `extract_graph`/`rule_edges`. Per namespace, under its lock, it re-folds every (entity, attribute) group of live semantic facts in event-time order (LIB-02 `integrate`; keyed attributes are cardinality one, as the write-time ladder assumes) and applies the difference through the write door: an older value that arrived after a newer one (reverse-order ingest) is archived with `evolve_to` the current record and its validity closed; true ties get the `disputed` tag. Fixes the P0-08 case `INV-ORDER-SWAP`. No model call. Pending entity merges and re-keys run in the library but need the `pending_merge` outbox (W-19 B), which does not exist yet. Tap: `job.reconcile.sweep`. `false`: the stage is not scheduled. |
| `offline.histories_v2` | `false` | O4-04 (LIB-20, O-13, O-14, M-13). `true`: the sleep cycle runs a `histories` stage right after `reconcile`. It merges near-duplicate facts of one key (shingle similarity `offline.near_duplicate_text`, guarded by numbers, dates, negation, modality and capitalised names): the earliest copy stays, the others are archived with `evolve_to` the canonical, plus one `merge_member` lineage edge each when `write.lineage_table` is on. A merged repeat is never counted as corroboration. It also rebuilds the disposable value histories (`build_value_history`). No model call. Tap: `job.histories`. `false`: the stage is not scheduled. |
| `offline.near_duplicate_text` | `0.80` | O4-04: character-shingle similarity at or above which two facts of one key are near-duplicate candidates (calibrate per corpus, RV17). |
| `offline.session_close_v2` | `false` | O4-02 (O-07..O-09, M-14; LIB-40). `true`: closing a session (`close_session`, a new session starting, `flush_windows(final=True)`) makes ONE structured call-2 model call (LLM role `session_close`, else `summarize`, else `extract`; the prompt is the library's stable prefix with no examples and the same-language rule, sent as raw messages, so no new prompt template). Code validates each item (reserved names, cited lines, derived time range), stitches episodes onto adjacent episode stubs (same speakers, topic overlap, time gap), and writes the summary and episodes through the derived-write door (`channel session_close`, parents = the cited turns, tags `kind:summary` / `kind:episode`, `extends:<stub>` for a stitched one). A repeat close of the same turns is a no-op (`stage_done` marker `session_close_v2` with the turn fingerprint); a late turn re-runs it as a new version. A model or parse failure leaves no marker. Cost: one model call per closed session. Taps: `job.close.call2` (status, turns, episodes, stitched, written, rejected by reason), `job.close.dedup`. `false`: the stub (a `session_closed` marker only). |
| `offline.session_close_budget` | `6000` | O4-02. Prompt budget of the call-2 plan in tokens (4 chars each); turns beyond it are deferred and the plan is marked incomplete. |
| `offline.profile_card_v2` | `false` | O4-07 (O-18..O-20, M-20; LIB-39). `true`: the profile ops call 2 proposes go through the guarded `apply_ops` (shape, evidence, stance, firm-evidence, staleness, POL-2 trust, kappa_P bound, EMA drift) and the card is kept in the event log (`profile_card_v2` marker, latest wins; the marker carries entry text, so it is not scrubbed by a turn forget: the next sleep expires entries whose support was forgotten). The `reflect_profile` sleep stage then invalidates entries whose support turns are gone and reports the reflection plan under `profile_card`. Needs `write.session_close_v2` to receive ops. Taps: `job.profile.ops` (accepted, rejected with reasons, contested, evicted), `job.profile.invalidate`. `false`: call-2 profile ops are only counted. |
| `offline.profile_kappa_p` | `24` | O4-07. kappa_P: the L5 profile bound, the maximum number of card entries; overflow evicts the least useful and stable entries and reports them. |
| `offline.profile_max_chars` | `2400` | O4-07. Character budget of the card (keys plus values). |
| `offline.profile_allow_inferred` | `false` | O4-07 (O-19). `inferred` ops are rejected unless this is on; an inferred op never overwrites a stated entry. |
| `offline.profile_alpha` | `0.3` | O4-07. EMA gain of the preference drift `P(t+1) = alpha p(t) + (1 - alpha) P(t)`. |
| `offline.sleep_v2` | `false` | O4-01a (O-01..O-03, LIB-07). `true`: `Engine.sleep()` returns a `SleepHandle` immediately (`status()` gives a `JobHandle`, `await wait()` returns the stage stats, `cancel()` stops at the next stage boundary) and the cycle runs as a namespace job under a persisted, epoch-fenced lease (one runner per namespace, even across processes; a second `sleep()` is `coalesced` into the holder) with a per-stage watermark (a restart resumes the open cycle and repeats no completed stage; an expired lease is reclaimed). A `TriggerPolicy` also fires jobs: (N deposits AND M minutes idle) OR context pressure C, behind a gap K; a daily maintenance job runs retention, decay and compress. Lease/watermark tables live in `<storage.path>.sleep` (in memory for `:memory:`). With `runtime.scheduler_v2` the job runs at background-derive priority. Taps: `job.sleep.handle`, `job.trigger`, `job.lease`, `job.watermark`. `false`: the blocking sleep, unchanged. |
| `offline.sleep_min_turns` | `20` | O4-01a: N, deposits since the last run before the deposit trigger can fire (`0` = the interval-loop migration). |
| `offline.sleep_idle_minutes` | `10.0` | O4-01a: M, minutes without a deposit before the deposit trigger fires. |
| `offline.sleep_context_pressure` | `0.7` | O4-01a: C, context pressure in (0, 1] that fires a job without waiting for N and M (still behind K). |
| `offline.sleep_min_gap_minutes` | `60.0` | O4-01a: K, minimum minutes between two trigger-fired runs of one namespace (manual `sleep()` bypasses it). |
| `offline.sleep_maintenance_hours` | `24.0` | O4-01a: hours between daily maintenance jobs; a completed full sleep counts as one. |
| `offline.sleep_lease_ttl_seconds` | `300.0` | O4-01a: seconds a namespace lease lasts without a heartbeat (heartbeats run between stages) before another job may take it over. |
| `offline.sleep_manual_only` | `false` | O4-01a: `true`: with `offline.sleep_v2` on, only a manual `sleep()` starts a job (no idle timers, no open-time catch-up); the deposit counters still persist. `false`: the trigger policy fires jobs on its own. |
| `offline.budget_v2` | `false` | O4-01b (O-05, O-06). `true` (needs `offline.sleep_v2`): a namespace job spends at most `offline.budget_calls_per_cycle` model calls and `offline.budget_tokens_per_cycle` tokens per cycle (read from the stage stats; a seam takes the economy ledger later). Once spent, the remaining model stages (not retention, decay, patterns, standing, reconcile, histories, edge compile) are deferred: recorded, persisted in the job store, counted per stage and toward the trigger N, never dropped; the next cycle runs them first. A started stage always finishes, so a cycle can overshoot by one stage. Every derived record a cycle writes is stamped `covers_seq` (in `RecordFingerprint`, no column): the newest raw-turn seq of its namespace when the cycle started. Read side: `read.freshness_v2`. Taps: `job.budget` (calls, tokens, exhausted), `job.deferred` (stage, pending, total). `false`: no cap, no stamp. |
| `offline.budget_calls_per_cycle` | `null` | O4-01b: model calls a namespace may spend per cycle; `null` = no cap on calls. 0 defers every model stage. |
| `offline.budget_tokens_per_cycle` | `null` | O4-01b: tokens a namespace may spend per cycle; `null` = no cap on tokens. |
| `offline.sleep_scoped` | `false` | FU-SLEEP (follow-up to O4-01a; needs `offline.sleep_v2`). `true`: a namespace job (trigger-fired, or `sleep(namespace)` on the scheduler) runs its stages over that namespace only (`PipelineContext.scope_namespace`); today every stage sweeps every namespace, so one namespace's trigger re-touches the rest. Its model calls are costed under the real namespace instead of the engine-wide pool `-`, and `economy.budget` applies per namespace (the namespace's write and sleep spend share one budget). A manual `Engine.sleep()` (lease key `*`) stays engine-wide. Converted: consolidate, mine_facts, project_facts, predict_calibrate, anticipate, reflect_profile, extract_graph, rule_edges, reconcile, histories, compile_edges, summarize_entities (v1 and v2), reorganize (communities v1 and v2), pattern_observations, standing_answers, check_watches, session_lifecycle, decay_sweep, compress, decay v2 multi-resolution, list cards. Not converted (engine-wide in every job, idempotent): `retention_expire`, `event_log_prune`, `sleep_compute`. `false`: every stage sweeps every namespace, as before. |
| `ingest.assets` | `"off"` | E04: a message's `attachments` (`[{"uri", "caption", "search_hint", "kind"}]`, plus `turn_id` for the source id) are ignored when off. On: each attachment's identity (source turn, original URI, source caption, availability) goes to the asset registry and the record is tagged `asset:<id>`. Nothing is downloaded at ingest. |
| `ingest.asset_dir` | `null` | E04: cache directory (gitignored in the harness): `registry.sqlite` and the downloaded bytes under `blobs/` by content hash. Null: in-memory registry, no blobs; `read.asset_evidence: fetch` is then refused. |
| `ingest.asset_max_bytes` | `5000000` | E04: a larger download fails explicitly (`too_large`). |
| `ingest.asset_timeout_s` | `15.0` | E04: download timeout in seconds. |
| `ingest.asset_allowed_mime` | `["image/jpeg", "image/png", "image/gif", "image/webp"]` | E04: types accepted; the bytes are sniffed and must agree with the declared type (`mime_mismatch`), a cross-host redirect fails (`redirected_offsite`). |
| `ingest.asset_vision` | `"none"` | E04: the vision/OCR evidence port. `none`: no description (the arm stays text-only; evidence is `skipped`). `ollama`: a local vision model through `/api/chat`. |
| `ingest.asset_vision_model` | `"qwen2.5vl:3b"` | E04: the Ollama model tag (documented only; the engine never pulls). |
| `ingest.asset_vision_url` | `"http://localhost:11434"` | E04: the Ollama base URL. |
| `ingest.asset_vision_timeout_s` | `120.0` | E04: timeout of one vision call. |
| `integrity.corroboration_roots` | `false` | W13 (ADR-059): corroboration counts independent lineage roots: a corroborating write whose `source.parents` lineage shares a root with the quarantined record or an earlier corroborator does not count. |
| `write(..., reply_to=)`, `write_messages` turn `reply_to` | — | G27 (ADR-061): the record a turn answers (record id, or in `write_messages` the index of an earlier message of the call). Stored as a `reply_to:<id>` tag; with associative memory a `reply_to` LINK too. A missing or foreign target is refused like a missing record. |
| `Engine.verify_forget(probe=...)` | — | W14 (ADR-059): erasure proven on recall — the erased text, searched for, must bring back no record holding it or a near-duplicate (`residual_recall`). |
| `read(as_of=...)`, `assemble(as_of=...)`, REST `/assemble` `as_of` | — | W7 (ADR-059): valid-time view — records begun by then; a fact superseded after `as_of` counts as current; relative phrases resolve against it. Add `recorded_before` for a known-at read. |
| `firewall.max_content_chars` | `null` | Quarantine non-privileged writes longer than this (size anomaly). |
| `firewall.protected_keys` | `[]` | Fact keys (`entity` or `entity.attribute`) only operator/system sources may write; others are quarantined. |
| `integrity.principal_reputation` | `false` | B7: multiply a principal's later write trust by min(1, 2 × Beta mean of its good vs bad records), where bad = quarantined, or the seed of a taint rollback. It only lowers trust, so the trust horizon still holds. `Engine.principal_reputation(p)` returns the factor. |
| `integrity.horizon_v2` | `false` | T5-01 (LIB-21): trust horizon on grants. A grant edge is a `trust.horizon.Grant` (horizon, kappa); foreign views, the deposit cap (POL-2) and the B4' walk use the library, and the cross-namespace edges of a lineage must form a visible grant chain within `grant_horizon`, else the record counts 0. Needs `attenuation: product`. Wiretap: `trust.horizon`. Off: unchanged. |
| `integrity.grant_horizon` | `null` | With `horizon_v2`: how many further hops a deposit may travel beyond its first grantee (0 = read but never re-shared). None: no explicit cap (only the implicit horizon where `kappa^n` falls under `admission_threshold`). |
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
| `retention.v2` | `false` | T5-05 (LIB-41): `expire_retention` / the `retention` stage plan with the retention library (`trust/pipeline/retention_v2.py`). Each record is classified (type, layer, `pii_tier`, first purpose, `recorded_at`); a matching `retention.classes` entry sets the TTL, else the `retention.profile` template, else it never expires. Layer L0 (turns) never expires under `event_log.mode: full`; legal holds, types that refuse deletion and records waiting in the erasure re-derivation queue are held. Expired records go through `forget(hard=True)` (lineage edges stay: expiry keeps lineage). The result adds `held_by_reason`, `tombstones`, `l0_protected`, `rebuild` (`full` / `limited` / `unavailable`) and `profile`; `Engine.retention_boundary(ns)` returns the last boundary, and a rolling log (`event_log.mode: rolling`) advertises a limited rebuild from the `event_log_prune` stage too. Tap: `job.retention.plan`, `.done`, `.prune`. Off: the `classes` walk above, unchanged. |
| `retention.profile` | `null` | T5-05: a LIB-41 template (`assistant`, `coding`, `ephemeral`, `multi_agent`, `regulated`) whose retention classes and read-audit setting apply under `retention.v2`; the event-log mode always follows `event_log.mode`. With `v2` on, a profile alone (no `classes`) is enough for the `retention` stage to run. |
| `audit.reads` | `false` | #49: every `search` / `assemble` / `read` / `retrieve` / `shared_search` / `export` appends one `memory.read_audit` event: principal (`event.actor`, from the REST auth binding or `principal_scope`, else `anonymous`), namespace, returned record ids, purpose and time. A verb that calls another public read verb audits once. Hash-chained (see `audit.actions`). No projector reads it. |
| `audit.actions` | `false` | #49: `forget` (with `actor=` / `reason=`), `correct`, retention expiry, `export`, `erase_subject`, `erase_namespace`, `approve_quarantined`, `reject_quarantined`, `feedback` (the signal, never the note), `grant` and `revoke` append a `memory.audit` event with the actor, principal, reason and record ids. Both audit kinds share one SHA-256 hash chain (`payload.chain.prev` / `.hash`); `Engine.verify_audit_chain()` / `audit_chain_ok()` validate it (a rolling log anchors at its oldest surviving audit event). I72: a sleep-cycle deposit skipped because a parent turn was forgotten meanwhile appends a `derived_deposit_skipped` audit (site and vanished parent ids, never content). |
| `audit.chain_v2` | `false` | T5-05 (LIB-41): audit decisions seal one hash-chained `AuditRecord` (actor, action class, op, ids, purpose, time, outcome, a short reason code; never content) as a `memory.marker` event (`audit_chain_v2`) instead of the `memory.audit` / `memory.read_audit` events. What is audited follows the retention profile: erase, consent and admin actions always, `correct`, reads only under `audit.reads` or a profile that audits reads (`multi_agent`, `regulated`). `Engine.verify_audit_chain_v2()` re-reads the chain (seq, links, hashes, time order); `audit_chain_ok()` uses it while on. Tap: `trust.audit.append`, `trust.audit.verify`. Off: the events above, unchanged. |
| `privacy.erasure_v2` | `false` | T5-04 (LIB-09): a hard `forget` plans over the lineage graph (the lineage table under `write.lineage_table`, plus `source.parents` and the summary member lists in the log) instead of cascading. The erased record is removed; each dependant gets one action: `keep` (another live support parent), `hide_and_rederive`, `unbind_and_hide`, `rerun_summary`. Erased and hidden ids are invisible at once (every read leg ends in `_consent_ok`; cache hits are revalidated, POL-1); a content-free manifest (ids and the erasure generation) is stored as a `memory.marker` (`erasure_manifest`); the lineage edges of the erased record are removed (not on retention expiry); a purge job removes erased and hidden ids from the vector, lexical and graph indexes and bumps the cache generation, one task at a time (`Engine.erasure_status()`, `erasure_resume_purges()`); `hide_and_rederive` / `rerun_summary` items wait in `Engine.erasure_queue()` for the offline runner, which calls `erasure_release(ids)` once they are re-derived. `cascade` is ignored while on (`cascade=False` plans no dependants); soft forget, `erase_subject` and `erase_namespace` are unchanged. State is rebuilt from the markers at start. Tap: `trust.erasure.plan`, `.purge`, `.done`, `.release`. Off: the hard-delete cascade, unchanged. |
| `privacy.erasure_redact_hidden` | `false` | FX-EX1A (EX-1a finding F2; needs `privacy.erasure_v2`): re-derivation of hidden dependants is not implemented, so under `erasure_v2` a hidden dependant keeps its row and its WRITE events (the erased text on disk, never served). On: each hidden dependant is hard-forgotten instead (row, log payloads, lineage edges, indexes, caches), and its queue entry is marked `dropped_no_rederive` (kept out of `Engine.erasure_queue()` unless `include_dropped=True`; counted as `dropped_no_rederive` in `Engine.erasure_status()`). A dependant under legal hold stays hidden and queued. Tap: `trust.erasure.redact_hidden`. Off: dependants stay hidden and queued for re-derivation. |
| `observability.write_timers` | `false` | I73: time each step of the write door (validation, firewall, redaction, embedding, projection, dedup, conflict ladder, perspective / sensitivity tagging, inline LLM steps) with the monotonic clock. `Engine.write_timers(reset=)` and `describe()["write_timers"]` return per step `count`, `total_ms`, `mean_ms`, `p50_ms`, `p95_ms` (steps are inclusive and nest); the eval harness writes the per-batch delta on the first `ingest.jsonl` line. Off: nothing is wrapped. |
| `observability.ops_ledger` | `false` | T5-06 (G-20): time every read verb (`search`, `retrieve`, `assemble`, `read`), write verb (`write`, `write_messages`, `write_episode`), window-extract job and sleep stage in `memspine.ops.ledger` (runs, errors, items, dropped, deferred, p50 / p95 / total ms; ids, counts and durations only). `Engine.ops_report(namespace=None)` (admin REST route `GET /ops/report`) merges it with the SLO ledger (`runtime.scheduler_v2`) and the write cost ledger (`economy.ledger_v2`: model, embed and rerank calls, tokens, budget). Off: nothing is timed; the report holds only what the other two ledgers hold. See `docs/v2/WIRETAP.md`. |
| `observability.otel.enabled` | `false` | T5-06 (G-20): export request -> stage spans through OpenTelemetry. Needs the `memspine[otel]` extra: `Engine.start()` raises `MissingServiceError` naming it when `opentelemetry` is not importable. Switches the wiretap on if `observability.tap.enabled` is off (no sink of its own, content off) and adds the exporter as a second sink. Span attributes are ids, hashes, counts, codes and durations only; an attribute that looks like text is dropped and counted in `ops_report()["otel"]["rejected_attributes"]`. Off: nothing is imported or exported. |
| `observability.tap.enabled` | `false` | WT-01 wiretap: a per-request trace of every pipeline stage (ids, scores, counts, decisions, drop reasons), for debugging and development only. Off: no event is built and no file is touched. `Engine.start()` configures it; `search`, `retrieve`, `assemble`, `read`, `write*` and `sleep` each open one request. See `docs/v2/WIRETAP.md`. |
| `observability.tap.sink` | `jsonl` | Where finished requests go (used only when enabled): `jsonl` (one line per request, one file per day), `sqlite` (requests + events tables), `memory` (ring buffer, `tap.memory_traces()`), `otel` (needs opentelemetry), `log` (logger `memspine.tap`), `none`. |
| `observability.tap.dir` | `runs/_tap` | Directory for the `jsonl` and `sqlite` sinks. |
| `observability.tap.content` | `false` | Keep text wrapped in `tap.content(...)` (truncated to `max_content_chars`, redacted). Off: text never reaches a trace. |
| `observability.tap.max_content_chars` | `200` | Truncation length for opted-in content. |
| `observability.tap.sample_rate` | `1.0` | Fraction of root requests traced. |
| `observability.tap.kinds` | `[read, write, job]` | Request kinds traced. |
| `consent.enforce` | `false` | #50 purpose limitation: records carry purposes (`write(..., purposes=[...])`, stored as `consent_tags`; `*` = any purpose) and reads pass `purpose=`. On, a read returns only records whose purposes include the read's purpose; a read without a purpose sees only untagged and `*` records. Applied in the search gates and to every returned list or assembled context. |
| `consent.untagged` | `allow` | #50: with `enforce`, whether records with no purpose are visible to every read (`allow`) or to none (`deny`). |
| `consent.v2` | `false` | T5-03 (LIB-18): privacy tiers, a consent registry with a generation, purpose narrowing and a per-subject do-not-derive switch (implies `enforce`). Sensitive and regulated `pii_tier` records are default-deny until `Engine.consent_grant(owner, purpose)`; `consent_revoke` hides without deleting; `consent_do_not_derive(owner, subject)` holds derived records about the subject. The registry is rebuilt from the log on start and its generation is in the read cache key. Wiretap: `trust.consent`. Off: unchanged. |
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
| `workers.outbox` | `false` | F-04: durable outbox. Window jobs are rows first, an inline runner drains them after each write and at start, a stop keeps unfinished jobs; model calls run outside namespace locks. `false` keeps the in-memory queue. |
| `workers.transport` | `"inline"` | RT-01 (G-19, W-13): `"taskiq"` (with `workers.outbox` on) also sends each enqueued outbox job id over a taskiq broker and re-sends every unfinished row (queued, or lease expired) at start. The message carries only the id; the outbox stays the truth. `"inline"` is unchanged. Tap: `runtime.transport.*`. |
| `workers.transport_broker` | `"memory"` | RT-01: broker for `workers.transport="taskiq"`. `"memory"`: taskiq `InMemoryBroker`, drained in this process (library mode). `"redis"`: Redis stream on `workers.broker_url` (needs `memspine[taskiq]`); this process only enqueues, and `python -m memspine.api.worker` drains. |
| `workers.transport_broker` | `"memory"` | RT-01: broker for `workers.transport="taskiq"`. `"memory"`: taskiq `InMemoryBroker`, drained in this process (library mode). `"redis"`: Redis stream on `workers.broker_url` (needs `memspine[taskiq]`); this process only enqueues, and `python -m memspine.runtime.worker` drains. |
| `runtime.scheduler_v2` | `false` | RT-02 (P-20, P-21, W-25, G-19 A): micro-batched encoder executors (document and query embedding), backpressure on the durable outbox (needs `workers.outbox`), background-derive priority for outbox work, and the SLO ledger (`economy.slo`: read p95, time-to-searchable). `false` changes nothing. Tap: `runtime.sched.*`, `runtime.backpressure.*`. |
| `runtime.model_concurrency` | `0` | RT-02 (P-20): number of model calls (LLM, embedding, rerank) allowed at once across all pipelines in this process; waiters are served foreground read, then foreground write, then background derive. `0` = no gate (unchanged). |
| `economy.ledger_v2` | `false` | W2-12 (W-26, P-22 C, LIB-27): count model calls, tokens in/out, latency and embed calls of every write, per layer, per namespace and per stage (`window_extract`, each sleep stage). Read with `Engine.cost_report(namespace)` or `Memory.cost_report()`. `false` records nothing and changes nothing. Tap: `economy.cost`. |
| `economy.ledger_persist` | `false` | FU-SLEEP (needs `economy.ledger_v2`). `true`: every ledger entry is also kept as a compact row (ids, namespace hash, layer, stage, kind, calls, tokens, latency; no names, no content) in a sibling SQLite file of the storage database (`<storage.path>.cost`; with Postgres `<storage.data_dir>/memspine.cost`), buffered and written in batches, and the totals are reloaded at start, so `cost_report` survives a restart. A reloaded row carries only the namespace hash: `cost_report(namespace)` matches it by hash and `by_namespace` names it once the namespace is seen again. Budget spend is not reloaded (it is per period). `:memory:` storage: not persisted (a warning is logged). `false`: in-process only. |
| `economy.budget` | `null` | W2-12 (W-26 D, P-22 C; needs `economy.ledger_v2` and `workers.outbox`): optional spend limit per namespace. When spent, window extraction pauses and the windows are parked in the outbox marked underived (never dropped); `Engine.resume_deferred(namespace)` derives them once the budget allows. Sleep stages are paused as one engine-wide pool (`-`), or per namespace under `offline.sleep_scoped`. Sub-keys: `max_calls` (generative calls per namespace per period), `max_tokens` (generative tokens in + out per namespace per period), `period_s` (period length in seconds; spend resets when it rolls; `null` = one period for the run). Tap: `economy.budget`. |
| `runtime.background_reserve` | `1` | RT-02: model slots background derive can never take (it holds at most `model_concurrency - background_reserve`, minimum 1). |
| `runtime.encoder_batch_max` | `32` | RT-02: texts per micro-batch of an encoder executor (`runtime.scheduler_v2`). |
| `runtime.encoder_batch_wait_ms` | `5.0` | RT-02: how long an executor lingers to fill a batch of background work; a foreground read never waits. |
| `runtime.backlog_coalesce_items` | `200` | RT-02 (W-25 B): per scope and stage, unfinished outbox items from which new work is coalesced onto the waiting job and marked underived (still queued, never dropped). `0` disables. |
| `runtime.backlog_coalesce_age_s` | `60.0` | RT-02: same, by the age in seconds of the oldest unfinished item. `0` disables. |
| `runtime.backlog_max_items` | `2000` | RT-02: hard cap per scope and stage; over it new background work is rejected BEFORE it is accepted (a `BackpressureRejected` receipt; the window writer keeps it and retries). `0` disables. |
| `runtime.backlog_max_age_s` | `900.0` | RT-02: hard cap by the age of the oldest unfinished item. `0` disables. |
| `runtime.backlog_max_total_items` | `0` | RT-02: hard cap over the whole outbox. `0` disables. |
| `runtime.backlog_retry_after_s` | `1.0` | RT-02: the retry hint in a backpressure receipt, and the delay before the window writer offers deferred items again. |
| `runtime.read_p95_budget_s` | `null` | RT-02 (P-21): optional read p95 budget; only colours the SLO report. |
| `runtime.lag_p95_budget_s` | `null` | RT-02 (P-21): optional time-to-searchable p95 budget; only colours the SLO report. |
| `prompts.overrides` | `{}` | Per-prompt overrides (body/system/format/version/output_model/token_budget) (D-43). |
| `prompts.partials` | `{}` | Override fragments for shared Jinja `{% include %}` partials (anti-injection block, output footer); `<name>` → replacement text, consulted before the shipped `_partials/` dir (B1). |
| `prompts.layout_v2` | `false` | W2-07 (W-18, P-07, P-08, P-10): prompt layout v2 for the write prompts `window_extract`, `extract@session3`, `extract@session4`, `extract_edges`, `extract_edges@session`. No worked examples; the system text is byte-identical for every call (window sections move to the user part) and its SHA-256 is the `PromptIdentity` hash (cache reuse, provenance); the coarse-date partial sits in that stable part; the same-language partial (`_partials/same_language.j2`) is added to the user part for non-English input only (script and stop-word detection, `prompts.layout_language`). Taps `prompt.render`. Off: every prompt renders as before. |
| `prompts.layout_partials` | `[same_language, coarse_date]` | W2-07: which layout-v2 partials stay on (one at a time for the per-partial screens, RV15). Ignored while `prompts.layout_v2` is off. |
| `prompts.selection` | `{}` | Per-role default scenario selectors: `<role>` → map of optional `memory_type`/`condition`, merged into every `select(role)` query so a deployment can pin a prompt variant without code (B2). Shipped `chat` conditions: `dated` (H12, `chat@dated`), `dated2` (G12, `chat@dated2`: `chat@dated` plus "a line's leading date is when it was said, `[= …]` is when the event happened; answer *when* questions with the happened date"), `infer` (G10, `chat@infer`), `dated3` (#34, `chat@dated3`: brief reasoning then a final `Answer:` line, which `Engine.final_answer()` extracts; quote the specific detail; dates in the granularity asked; "a date in brackets is when it was said; the event may be earlier"; merge repeated mentions before counting; "Not mentioned" only when nothing bears on the question). `temporal` (C1/H11, `chat@temporal`: `chat@dated` plus "infer a date not stated outright from the line's date and any relative phrase; answer dates as DD Month YYYY"), `inference` (C1/H11, `chat@inference`: `chat@dated` with the refusal replaced by "give the most plausible answer and say 'likely'"). C2: the entry `chat_by_shape` (default unset = unchanged) maps a question shape (`temporal`: `query_shape.is_temporal`; `inference`: `is_inference`, a "would / likely / might / could ...?" question; temporal wins) to the `chat` condition `Engine.chat_messages` uses for that question, e.g. `chat_by_shape: {temporal: temporal, inference: inference}` (the harness `--qa-prompt routed` mapping) or `{temporal: infer, inference: infer}`; other questions keep the `chat` selection, a per-call `condition` wins, and an unknown shape or condition is a config error. `plan` condition `v2` is selected by `read.planner_version`; `extract` conditions `session3` / `dates` by the consolidation options. |
| `memories.*.enabled` | `false` | Enable a memory type (`working`/`episodic`/`semantic`/…); C1b auto-enables prerequisites. |
| `memories.*.policies` | `{}` | Per-type policy overrides (conflict/dedup/trust/entity_extraction/page_size/…). `semantic.policies.extract_graph` (`{max_rounds, min_confidence}`) opts into C2 graphiti-style writes: with an `extract_edges` LLM role, the background `extract_graph` sleep stage writes edge facts + `asserted` links. `semantic.policies.write_pipeline: graph` opts into the C3 synchronous variant — edges extracted at write time and written through the M4/M5 ladder (ADR-026). `episodic.policies.sessions.passive_after` (seconds or `"30d"`; default off) opts into the #53 session lifecycle: idle sessions go PASSIVE, out of default reads (ADR-037). |
| `namespaces.*.policies` | `{}` | Per-namespace policy overrides (D-14). |
| `plugins` | `[]` | MOD-02 / ER-34: pinned third-party plug-ins. Entry points in group `memspine.<kind>` are discovered but never used unless listed here as `{name, slot: "<kind>:<name>", version: "1.2.3" (exact pin), lifetime: app\|job\|request, hash: "sha256:<RECORD hash>" (optional)}`. A wrong version or hash, two providers for one slot, a slot that shadows a built-in, or a lifetime that differs from the one the plug-in declares is a startup error. Kinds: `llm_providers`, `embedders`, `rerankers`, `vector_stores`, `lexical_indexes`, `graph_stores`, `read_legs`, `derive_stages`, `memory_types`, `prompt_sets`. Empty: no plug-in code is imported. |
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
| `memories.semantic.policies.extract_graph.close_ended` | `false` | G-1 (Graphiti edge end time): the edge extraction prompts (`extract_edges` v4, `extract_edges@session`) may return `valid_to` when the text says the relation stopped holding ("until 2022", "used to", "no longer"). With `close_ended: true` such a fact is written already closed (`valid_to` set), so current-state reads skip it and as-of reads still see it. |
| `memories.semantic.policies.extract_graph.contradictions` | `false` | G-2 (Graphiti cross-key contradiction): a new *state* fact is checked against the same subject's live facts on *other* keys (most shared words first, at most `CONTRADICTION_CANDIDATES` = 3) by the `invalidate_edge` role, one call each. An `update` / `invalidate` verdict retracts the old key through the semantic door, so the M4 ladder closes it, with its trust gate. Needs the `invalidate_edge` LLM role; the stage stats report `edges_invalidated`. |
| `memories.semantic.policies.extract_graph.cardinality` | `{}` | I17 (the integration operator): a `{relation: one or many}` map that overrides the extractor's `kind` for the relations it names. `one`: a newer value supersedes the older, which stays as history (`valid_to` set). `many`: the values coexist (two pets, two employers), never superseded. Unlisted relations keep the extractor's `state` / `event` choice. Contested writes (unresolved conflict, both kept) are `memories.semantic.policies.conflict.contest_ties` / `contest_lower_trust`. |
| `memories.semantic.policies.extract_graph.relation_types` | `[]` | G-6 (Graphiti edge types): a declared relation vocabulary, e.g. `[works_at, lives_in, married_to]`. The edge prompts are told to use only these `rel` values, and an extracted edge outside the list is dropped (counted as skipped). Empty: any relation. |
| `memories.semantic.policies.extract_graph.entity_types` | `[]` | G-5 (Graphiti entity types): declared kinds of entities, e.g. `[person, company, place]`, which the edge prompts are told to keep to. Empty: any entity. |
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
| `memories.semantic.policies.conflict.perspective_key` | `false` | I55: the conflict key also carries the subject and scope tags (`sub:` / `scope:`, written by `memories.episodic.policies.perspective` or passed as tags). A same-key statement about another subject, or of another scope (an event vs a standing preference), coexists instead of superseding; the incumbent it competes with is the newest active fact with the same subject and scope (a keyed listing per write, so opt-in). A polarity flip (`pol:neg` vs `pol:pos`) on one key and subject is a `contest` (both kept, tagged `disputed`). Untagged facts behave as before. |
| `memories.semantic.policies.conflict.inferred_defers` | `false` | I48: an engine-inferred fact (`src:inferred`) never changes a stated fact on the same key (NOOP), and a stated fact supersedes an inferred one whatever their event times (a `retract` still retracts). |
| `memories.episodic.policies.consolidation.miner` | `llm` | W5 (ADR-059): `rules` mines first-person personal facts without a model (`core/rule_miner.py`): home, origin, job, employer, relationship, age, education, diet, favourites, partner / parents supersede; likes, dislikes, pets, family, activities, plans coexist. With `mine_facts: true`. |
| `memories.episodic.policies.subject_tagging` | `false` | W8 (ADR-059): an episodic turn written as "Name: text" is tagged `speaker:<name>`. |
| `memories.episodic.policies.perspective` | `off` | I39-I46: `heuristic` (or `{"mode": "heuristic" or "decider", "axes": [...]}`, axes among `spk addr sub ask mod pol scope cert rep sens`) tags each episodic turn with its speaker, addressee, subject(s), asker, modality, polarity, scope, hedge, reported-speech source and sensitive category; content is untouched. `write_messages` turns may carry `speaker` (or `name`) and `addressee`, which beat the rules. `context` = `{"owner": id, "speakers": [ids]}` (I51) seeds the known participants and the default asker. `ack: true` (I47) tags the user turn that acknowledges the assistant turn before it (`ack:<assistant record id>`; cues "yes", "exactly", "that's right"; with `decider` the `acknowledges` task decides); without an acknowledgement an assistant statement stays proposed. Plan turns carry `due_from:` / `due_to:` dates (I49): an absolute date or month goes through the date resolver, a relative phrase is resolved against the turn date, no horizon means no window; past plans render `[past plan]` and are never deleted. Mined facts and graph edges (`extract_graph`, the graph write pipeline) inherit the `sub:`, `scope:` and `pol:` tags of their source turn (I55). `decider` lets the decider port (`read.decider: opendecider`) answer `is_fact`, `is_negated`, `is_standing`, `is_hedged` above `read.decider_min_confidence`. See [Perspective layer](#perspective-layer-i39-i46). |
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
| `memories.prospective.policies.v2` | `false` | O4-10 (LIB-28, O-23). `true` (with `memories.prospective.enabled`): stated plans become advisory intentions. A plan is a live record tagged `mod:plan` (without `mod:fact`); `due_from:` / `due_to:` day tags give its window; later statements about the same subject and content close it (done, cancelled by a negated plan or `pol:neg`); old ones expire. `Engine.reminders(namespace, now, cues)` returns the ones worth surfacing (reasons `in_window`, `overdue`, `deadline_near`, `upcoming`, `cue_match`), advisory only, with source ids and a trust at most the least trusted source. The sleep cycle's `check_watches` stage also reports `intentions_*` counts. Nothing is stored or delivered. Taps: `job.prospective.sweep`, `job.prospective.closed`, `job.prospective.read`. `false`: the due-time and invalidation watches only. |
| `memories.procedural.policies.v2` | `false` | O4-10 (LIB-28, O-24). `true` (with `memories.procedural.enabled`): skills and plans of one name that agree on their steps (at least 2 sources; steps are lines, a numbered list or `a -> b`; plain tags are the procedure's tags, `pre:<text>` tags its preconditions) become an advisory procedure with versions (a changed consensus opens a new version, the old counts move to history). Its counts move only on explicit `like` / `dislike` feedback on the source records; the engine's own outcome-receipt likes (actor `system`) are implicit and refused. `Engine.suggest_procedures(context_tags, namespace, limit)` ranks them (tag overlap x reliability x trust; an unmet precondition halves the score). Never executes anything; nothing is stored. The sleep cycle's `check_watches` stage reports `procedures_*` counts. Taps: `job.procedural.built`, `job.procedural.versioned`, `job.procedural.update_refused`. `false`: unchanged. |
| `memories.shared.policies.observer_views_v2` | `false` | O4-11 (LIB-44, O-26, M-24). `true` (with `memories.shared.enabled`): `shared_search` judges each foreign hit through the observer view over the grant (LIB-21: scope, horizon 1, kappa from `integrity.kappa` / `integrity.edge_kappa`; trust = home trust x kappa) instead of the per-edge attenuation. A hit that crosses keeps the E1 cap (`min(view, 0.3)`) unless `integrity.enabled`, which keeps its own admission threshold; the rest are dropped with a reason (`no_grant`, `scope_not_granted`, `horizon_exceeded`, `zero_trust`, `below_threshold`). Read-only; the library multiplies kappa (`integrity.attenuation: min` is not applied here). Tap: `read.observer_view`. `false`: unchanged. |
| `memories.episodic.policies.correction_detector` | `false` | W17d (ADR-060): a user turn correcting a fact ("no, I said Tuesday", "actually it's", "not X, Y", "that's wrong") is tagged `correction`; the best-matching live keyed fact is superseded (or retracted without a replacement). |
| `memories.semantic.policies.trust.source_types` | `{}` | N26 (ADR-060): document type (`doctype:<t>` tag, else channel) → authority tier 0–3; trust capped at 0.3 / 0.45 / 0.6 / 1.0. |
| `memories.semantic.policies.trust.hold_needs_evidence` | `false` | N26 (ADR-060): hold a non-privileged write below `authority_min_tier` (2) in quarantine (`pending_evidence`); `Engine.review_evidence` promotes it on authoritative support. |
| `memories.associative.policies.entity_nodes` | `false` | GP-2 (#13): the graph projector adds an `ent:<namespace>:<canonical>` node per entity a record names (its `entity` field and `dst:` tags; canonical = NFKC, casefolded, whitespace collapsed) and a `mentions` edge record -> entity weighted by the record's trust. `true` uses the default blocklist (pronouns, day words, "luck"); a map takes `blocklist` (replaces it) and `allowed` (only these names). Rebuild == incremental; forgetting a record removes its mentions and any entity left without one. `mentions` is a reserved rel. Change it, then `engine.rebuild()`. |
| `memories.associative.policies.entity_summaries` | `false` | GP-6 (#17): the `summarize_entities` sleep stage (after `extract_graph`, before `reorganize`) writes one summary record per entity node whose membership (the live records with a `mentions` edge to it) changed since the last sweep; unchanged entities cost nothing (watermark: `entity_summarized` MARKER events, erased with the summary). The dated fact lines are the summary for free while they fit `free_chars` (2000); longer ones go to the `summarize_entity` role (else `summarize`; prompt `summarize_entity.yaml`: facts only, dates kept, no meta-language), `batch_size` (30) entities per call; without a role, the newest lines that fit. The record is derived (channel `entity_summary`, tags `entity_summary` + `about:<Name>`): parents = the members (a member's hard forget cascades to it), trust = the least member trust, firewall-screened. Drift supersedes it (archived); a soft-forgotten member is dropped at the next sweep. `true` or a map with `free_chars`, `batch_size`. Needs `entity_nodes`. Read with `read.entity_summaries`. |
| `memories.semantic.policies.extract_graph.resolve` | `off` | GP-7 (#18): entity resolution in `extract_graph` before facts are written: `rules` = exact canonical name, then the alias table (earlier decisions), then candidates (embedding cosine top 15 among the namespace's entities), an entropy gate (short low-entropy names such as "Mel" skip string matching) and MinHash shingle Jaccard ≥ 0.9; `llm` adds one batched `resolve_entity@batch` call per sweep for the names still ambiguous (needs a `resolve_entity` role; without one it acts as `rules`). A merged name is written as the known entity's spelling ("Mel" → "Melanie"). A match whose source trust differs from the entity's (the most trusted record naming it) by more than 0.2 is `contested` and not merged. Decisions are `entity_resolved` MARKER events (keyed by the source record, erased with it), so rebuilds and later sweeps reuse them without a call. Names new in the same sweep are not resolved against each other; write-time records are not resolved. |

---

### Perspective layer (I39-I46)

A record belongs to one owner (its namespace: the hard isolation boundary), but its text has a speaker, an
addressee, one or more subjects and a stance. `memories.episodic.policies.perspective` writes those as tags
(`spk:`, `addr:`, `sub:`, `ask:`, `mod:`, `pol:`, `scope:`, `cert:hedged`, `rep:`, `sensitive:`), rules only and
language-light (English regexes, see I23); an unknown speaker is `user`. `sub:` ids are participant ids, or
`<relation>@<anchor>` for a related third party ("my cousin" said by Caroline is `sub:cousin@caroline`),
`3p` for an unresolved "he / she / they". At read time `read.perspective_mode` resolves the question the same
way and weights or filters candidates by subject match; `read.perspective_axes` adds modality, polarity, scope
and sensitivity. Subject is a soft signal; the namespace stays the only hard boundary. Latest-wins (`read.latest_wins`)
keys per subject and ignores questions, requests and hypotheticals once the tags exist.

```python
eng = Engine(
    memories={"episodic": {"policies": {"perspective": "heuristic"}}},
    read={"perspective_mode": "subject_weight", "perspective_axes": ["subject", "polarity"]},
)
await eng.write_messages(
    [{"role": "user", "speaker": "Caroline", "content": "Caroline: My cousin wants to volunteer."}]
)
```

### English-only features (I23)

These read-path features match English words with regexes; they were written and tuned on English data and
are NOT multilingual. Non-English text can misfire them (a month name that is also a common word, a modal
that is a different word) or simply never trigger.

| Feature | Module | English-dependent part |
|---|---|---|
| Question shapes: `is_temporal`, `is_ordering`, `is_inference`, `is_verbatim`, `is_set_question(_wide)`, `is_count`, `is_aggregation`, `is_duration`, `is_novelty`, `is_personal`, `statement_form`, `question_shape`, `rule_read_mode` | `core/query_shape.py` | wh-words, modals, set nouns, plural heads |
| Temporal query spans (`temporal_leg`, `query_interval`, N45 yearless dates) | `core/temporal_query.py` | English month names, "in/during/since <month>", "of" |
| Relative-date resolution and annotation (`resolve_relative_dates`, `temporal_relative`, `relative_dates_anchored`, `resolve_durations`) | `core/temporal_resolve.py` | "last week", "two days ago", weekday and month names |

`read.language_guard: on` makes these fail closed on text judged non-English (non-Latin script, or Latin script
with more French / German / Spanish / Italian / Portuguese / Dutch stopwords than English ones). An ISO date
(`2023-05-30`) still resolves. It is a context variable set around `read` / `assemble`; write-time mining
(`consolidation.mine_event_dates`) is not covered. Short or name-only text counts as English. Until a
multilingual trigger set exists, treat these features as English-only.

### Data-shape profiles (I25)

The `base` template's read defaults were tuned on one shape of data: named speakers, real timestamps, short
turns. `data_profile` (opt-in, default `off`) layers small presets from `src/memspine/config/presets/` between
the template and your own settings, chosen from facts about the data (`data_shape`). The mapping comes from the
gap analysis, not from benchmark scores; an undeclared fact selects nothing.

| Fact | Preset | What it sets |
|---|---|---|
| `has_timestamps: true` | `ts_dated` | dated rendering, relative-date anchoring, event-time leg, rerank date prefix |
| `has_timestamps: false` | `ts_none` | plain rendering, no date legs or date resolution, defaulted clocks skipped (I7) |
| `turn_length: long` | `long_turns` | token-unit replay window, budget scaling, chunk-and-max rerank (I6, I9, I20) |
| `history_size: large` | `large_history` | budget scaling (I20) |
| `speaker_kind: named` | `named_speakers` | name-keyed speaker vote (I5) |
| `speaker_kind: user_assistant` | `chat_roles` | perspective tags and vote (I39), assistant exempt from the prefix-repeat signal (I21) |
| `language` not `en*` | `non_english` | `read.language_guard: on` (I23) |

```python
eng = Engine(data_profile="auto", data_shape={"has_timestamps": False, "speaker_kind": "user_assistant"})
eng = Engine(data_profile="chat_roles,ts_none")   # or name the presets
```

In the eval harness the adapter declares the shape (`DatasetInfo.shape`, or `EvalItem.meta["shape"]`), the rest
is inferred from the history (`memspine_evals.shape`), and `MemspineSystem` passes it to the engine when the
arm config sets `data_profile`.

### Image assets and public knowledge (E04, E03; opt-in)

Both ports are off by default and leave a read byte-identical when off.

**E04 image assets.** `ingest.assets: on` keeps each attachment's identity at write time; `read.asset_evidence`
resolves it at read time, only for a question that needs the picture. The adapter downloads **only the URI the
turn itself carried**, once, caches the bytes by content hash under `ingest.asset_dir`, and runs the vision port
once per distinct image. A missing or expired URL, an oversized file, a type mismatch or an off-site redirect is
recorded on the registry entry (`availability: unavailable`, `error`) and shown to the reader as an explicit
"unavailable ... do not guess" line; nothing is substituted, and the data source's search phrase is stored for
audit but never used as evidence. Report the arm apart from text-only runs (`meta.network_calls`,
`meta.vision_calls` per question in the harness).

```python
eng = Engine(
    ingest={"assets": "on", "asset_dir": "evals/runs/_asset_cache",
            "asset_vision": "ollama", "asset_vision_model": "qwen2.5vl:3b"},
    read={"asset_evidence": "fetch"},
)
await eng.write_messages([{"role": "user", "content": "Look at this", "turn_id": "D1:2",
                           "attachments": [{"uri": "https://…/pic.jpg", "caption": "a photo of a book"}]}])
```

Local vision model (documented, not pulled): `qwen2.5vl:3b` (about 3.2 GB of weights, roughly 4 to 5 GB of VRAM
with image tokens) fits next to `qwen3.5:9b` (about 6.6 GB at Q4) on a 16 GB GPU with room for the KV cache;
`gemma3:4b` (about 3.3 GB) is the alternative. Run vision between reader batches or after the reader is unloaded
if both cannot stay resident.

**E03 public knowledge.** `read.external_evidence: cache|web` fires only for an invited inference or a
recommendation request. The query is built from the question's lowercase topic words after removing every name,
place, number, URL, e-mail, quoted span, relationship word and every capital word of the retrieved context
(`memspine.services.external.privacy`); private conversation text is never sent. Results are cached, the call
budget is enforced, and each failure (`cache_miss`, `budget_exhausted`, `provider_unavailable`,
`provider_error`, `filtered_empty`) leaves the context unchanged and is recorded in the forensics. The block is
labelled `[public knowledge]`, carries the clause that it cannot prove a private event, possession or
attribute, and is never persisted. Use the `http` provider with `MEMSPINE_EXTERNAL_SEARCH_URL` /
`MEMSPINE_EXTERNAL_SEARCH_KEY`; report web-enabled runs apart from memory-only runs.

**I61 premise-tolerant answering** is a harness reader clause, `--premise-tolerant` (no extra model call): when
a question carries a detail that differs slightly from memory, answer the supported part and correct the detail
instead of confirming or refusing. It trades against abstention, so screen it with the cat-5 guard slice.

## Where to go next

- [`FEATURES.md`](./FEATURES.md) — the feature catalog (types, firewall, E2–E9).
- [`examples/01_quickstart.py`](../examples/01_quickstart.py) → `04_prospective_shared_rest.py`.
- [`memspine-structure-plan.md`](./memspine-structure-plan.md) — the authoritative blueprint.
- [`adr/`](./adr/) — architecture decision records (ADR-001 … ADR-031); the newest cover
  the multi-call write pipeline (ADR-026), record group tags (ADR-027), Leiden community
  detection (ADR-028, amended by ADR-043), the trust-horizon invariant (ADR-029, proposed), relevance-first
  scoring (ADR-030, proposed), and the decision port with call accounting (ADR-031,
  proposed).
