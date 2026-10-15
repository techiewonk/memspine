# Ingestion walkthrough: every layer, step by step

This is how to run ingestion by hand, one turn at a time, and read what each layer did. The stage ids (A1…A28, B…) are the ones in `PIPELINE_STAGE_INVENTORY.md`. `params <id>` in step mode lists each stage's knobs (`STAGE_PARAMS.md`).

## 0. Start a session

Run from `memspine-restructure/evals`, using the shared venv:

```powershell
$env:PYTHONPATH="../src;."
$py="C:/Users/hembad/Documents/GitHub/memspine/.venv/Scripts/python.exe"

# A. invented demo conversation, CPU only, stub extractor (every layer fires, no GPU needed)
& $py -m memspine_evals.inspect step --data demo --item demo-1 --arm demo --offline --out runs/_analysis/inspect/walk

# B. real LoCoMo conversation, real extraction (needs Ollama qwen3.5:9b running; uses the GPU)
& $py -m memspine_evals.inspect step --data ../data/locomo10.json --item conv-26 --arm scr-b8-write-v2 --live --out runs/_analysis/inspect/walk
```

**Arm A** (`evals/memspine_evals/inspect/demo_arm.json`) turns on the full write-v2 path: window extraction, envelope stages, stamping, role policy, temporal, grounding, integration and the lineage table. The extractor is the stub `stub/window`. Arm B is the real one.

**Commands:** `turn` (one turn), `turn 3`, `turns D1:1-D1:4`, `say Bram: … @2024-05-01 #S9`, `interaction`, `session`, `sleep`, `ask q1`, `show <rid|window|occ>`, `links <rid>`, `diff`, `params <stage>`, `set key=value`, `rerun window <id>`, `save`, `quit`. Add `--pause` to stop between stage groups, and `--wide` to print every event.

## 1. What one turn goes through (`turn`)

Each row is one block of the step output. The trace event is what to grep for in the logs.

| # | Layer | What it does | Code | Trace event (what to check) |
|---|---|---|---|---|
| A1 | Entry | The adapter sends the message to `Engine.write_messages` | `evals/memspine_evals/systems/memspine_system.py:_write_batch` → `src/memspine/api/engine.py:write_messages` → `src/memspine/ingest/write.py:write_messages` | `write.messages.received turns=1 depositable=1` |
| A6 | Role policy and echo guard | Decides how the turn may be used: `stated` / `proposal`, and the source class (`user_stated` …), trust cap 0.7 for user | `src/memspine/ingest/role_wiring.py:decide`, `ingest/roles.py` | `write.role.turn role=user derive_use=stated src_class=user_stated trust_cap=0.7` |
| A7 | Stamping | Three clocks, timezone, speaker / addressee / participants, date precision | `ingest/write.py:_stamp_turn` → `ingest/stamping.py:ConversationStamper.turn` → `ingest/stamp.py:stamp`, `parse_source_time` | `write.stamp.turn spk=bram participants=2 source_tz=UTC precision=second dated=true`. **Check `dated=true`.** A turn without a date gets `precision=unknown`. |
| A8 | Turn tags | Perspective tags (`spk:`/`addr:`), reply link, assets | `ingest/write.py:_write_turns` | (tags appear in `show <rid>`) |
| A9 | Index view (opt-in, `write.index_view`) | Rule-built search text: speaker, date words, previous-turn words for anaphoric turns | `ingest/index_view.py:plan_prefixes` | `write.index_view.text` |
| A10 | Record build | Builds the `MemoryRecord` (uuid record id, `valid_from`, trust cap) | `ingest/write.py:write` | `write.turn.bound rid=… turn=0` (binds turn → record id) |
| A11 | Firewall, redaction, quarantine | Trust, instruction-shaped text, PII, anomaly score | `ingest/write.py:_screen_write` → `trust/firewall.py` | `write.firewall.verdict trust=0.7 quarantine=false anomalous=false` |
| A12 | Route and event append | Turn → plain episodic path. The WRITE event goes into the event log. | `ingest/write.py:_write_screened` → `api/engine.py:_append_and_project` | `write.route route=plain`, `write.deposit action=added` |
| A13 | Projectors | Writes the record into four stores: records (SQLite), vectors (LanceDB), lexical (Tantivy BM25), graph node | `store/projections/projector.py`, `services/vector/projector.py`, `services/lexical/projector.py`, `memories/associative/projector.py` | `store.index.record vector=yes lexical=yes graph_node=yes projectors=[records,vectors,lexical,graph]` |
| A15 | Window observe | The turn joins the open window; cuts happen on turns, tokens, topic, idle, gap, day or session end | `workers/windows.py:observe`, `derive/segmenter.py` | `write.segment.cut`, `write.window.cut` (they show up when a window closes) |

## 2. What a closing window goes through (`session`, or a cut mid-session)

| # | Layer | What it does | Code | Trace event |
|---|---|---|---|---|
| A16 | Live-turn filter | Keeps live, unquarantined, role-allowed turns | `workers/windows.py:_live_turns` | `write.window.planned turns=10 excluded=0` |
| A17 | Pre-pass (opt-in) | Rules + GLiNER: skip / episode_only / full | `derive/prepass.py` | `prepass=off` on the window line |
| A18 | Prompt context | CONTEXT (previous window tail) + KNOWN entities | `derive/prompt_context.py` | `write.prompt.v2 known_count=0 schema_mode=…` |
| A19 | **Extraction call (LLM)** | One structured call: numbered lines → `facts` / `edges` / `episode` JSON | `workers/windows.py:_call`, `PromptWindowExtractor`, prompts `src/memspine/prompts/structured.py` (`structured.window`) | `extract.io window=… model=… tokens=… ms=…` with the **prompt and raw reply printed**. `show <window_id>` shows them in full. |
| A20 | Validate, repair, compile | Items → `AssertionEnvelope` (occ id, key, evidence turn ids); one repair call; dead letters | `derive/items.py:process_items`, `derive/envelope_compiler.py` | `write.item.valid id=oc_… kind=state`, `write.item.dead` |
| A21a | Envelope stage 100 · temporal | Event time, precision, basis (e.g. "last week" + source date) | `derive/temporal_stage.py` | `occ … temporal:rewritten(time)` → `{"precision":"week","basis":"source_time","at":…}` |
| A21b | Stage 200 · grounding | Checks the claim against the cited lines: grounded / uncertain / dropped | `derive/grounding_stage.py` | `grounding:dropped(ground.dropped.modality_asserted_vs_hyp…)`, `write.ground.summary before=8 after=7` |
| A21c | Stage 250 · coverage (opt-in) | Lines not covered → at most one re-extraction call | `derive/coverage_stage.py` | `write.coverage.window` |
| A21d | Stage 300 · resolver (opt-in) | Mentions → entity id or `pending_merge` | `derive/resolve_stage.py` | `write.resolve.mention` |
| A21e | Stage 400 · integration | append / supersede / merge / coexist / contest (or defer) | `derive/integration_stage.py` | `integrate: {"op":"append","rung":"cardinality"}`, `op=defer_kc` |
| A21f | Stage 500 · pre-pass cross-check | Names the pre-pass never saw | `derive/prepass.py:prepass_stage` | `write.prepass.unseen` |
| A23 | Fact deposit | Envelope → semantic record (parents = cited turns, trust ≤ min of the parents) | `workers/windows.py:_store_envelopes` → `derive/pipeline/integrate.py` | `write.extract.deposit id=… layer=facts parents=1` |
| A24 | Derived door and lineage | Gates, then the lineage edge child → parent turn | `derive/write.py:write_derived` | `derive.lineage.write parents=[…] roles=["support"] trust=0.5 trust_cap=0.7` |
| A25 | Dedup and conflict ladder | Same entity and attribute already stored → add / merge / update (supersede) / conflict | `memories/semantic/store.py:write` | `ladder verdict=update rule=R3_newer_supersedes incumbent=…` |
| A22/A26 | Edges, episode | Window edges are queued for sleep; episode fragments are deposited | `workers/windows.py:_deposit_sections`, `_store_episode` | `write.extract.edges`, `write.edges.pending` |

Every envelope prints its full journey on one line, for example:
`occ oc_… compile:added temporal:rewritten grounding:rewritten integration:rewritten deposit:kept -> record <rid>`.
**If a fact has gone missing, this line shows the stage where it was dropped and why.**

## 3. Sleep (`sleep`)

Session close → graph extraction → entity summaries → communities → reconcile, histories → decay. Code: `workers/schedule.py:run_sleep_stage`, `derive/pipeline/stages/*`. Trace events: `job.stage.start/end` plus `job.stage.item` (per output record: input ids → output id, channel). B-ids are listed in the inventory.

## 4. Where the logs are

| What | Where |
|---|---|
| Live, per step | The terminal (step mode prints the events in memory, grouped by stage) |
| Full bundle (after `save`) | `evals/runs/_analysis/inspect/walk/step-<data>-<item>--bundle/`: `turns.jsonl` (per turn: record, stamp, firewall, index), `windows.jsonl` (members, prompt and reply), `envelopes.jsonl` (per envelope per stage), `records.jsonl` and `links.jsonl` (store snapshot: parents and lineage), `sleep.jsonl`, `checks.jsonl` (22 invariants) |
| Raw harness logs | `…--forensics/ingest.jsonl` (per turn), `…--trace/`, `…--memspine/` |
| Visual | `python build_inspector.py runs/_analysis/inspect/walk/step-demo-demo-1--bundle` → `index.html` (Conversation and Window views) |
| Web | The Tuning Studio (`python -m memspine_evals.studio.http_server`), Write tab → stage strip per turn |

All of these are under the gitignored `evals/runs/`. Real LoCoMo text stays on this machine.

## 5. What to check at each layer (manual checklist)

1. **Stamp:** `dated=true` with the right precision. The timezone is assumed only when the dataset has none.
2. **Firewall:** `trust` is as expected (user 0.7, assistant lower) and `quarantine=false` for normal turns.
3. **Index:** all four projectors are listed. The `text_len` matches the turn.
4. **Window:** members are one session (unless batching), and the cut reason makes sense.
5. **Extraction:** the prompt contains your lines, and the reply parses. Items in the reply match the `write.item.valid` count.
6. **Envelopes:** no unexpected `dropped`. Temporal `at` matches the turn's date plus the relative phrase.
7. **Ladder:** `R3_newer_supersedes` only where the newer fact really replaces the old one.
8. **Lineage:** every derived record has a parent turn (`links <rid>`).
9. After `save`: `checks.jsonl` has 0 errors.

## 6. Known quirks seen in the walkthrough (2026-10-15)

- **Stub extractor:** in offline mode, `stub/window` turns every line into a `"<Speaker> said <line>"` fact. On the same entity and attribute, the ladder then supersedes the earlier "said" facts (`R3_newer_supersedes`). This is a stub artefact; real extraction emits proper attributes.
- **Fixed today:** single-turn writes from the eval adapter did not pass the timestamp, so stamping showed `dated=false precision=unknown`. Fixed in `memspine_system.py`; the stamp is now `dated=true precision=second`.
- **Raw date strings:** `parse_source_time` does not parse the raw LoCoMo string (`"7:31 pm on 21 January, 2022"` → `stamp_unparsed`). The adapter converts it to a datetime first, so runs are fine, but a raw string passed directly is not dated.
