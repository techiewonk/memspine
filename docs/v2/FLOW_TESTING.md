# Testing every flow by hand, with commands

This guide covers every engine flow: write, sleep, read, quarantine, forget, correct, rebuild, procedural, tasks, prospective, shared, graph, assets and views. Each flow is a numbered list of steps, and every step is one command. It prints what the step did, the values it got, PASS or FAIL against the expected behaviour, and optionally the trace events it produced and every engine line that ran.

Everything runs on the CPU: in-memory SQLite, the hash embedder and an offline stub instead of the LLM. You need no GPU, no model and no network. Only invented names are used.

Last full run (2026-10-15): **467 of 468 checks pass**. The one failure is F12.9, a real engine gap (section 4).

---

## 0. Setup (once per PowerShell window)

```powershell
cd C:\Users\hembad\Documents\GitHub\memspine-restructure
$py = "C:/Users/hembad/Documents/GitHub/memspine/.venv/Scripts/python.exe"
$env:PYTHONPATH = "src"
& $py docs/v2/flow_checks.py --list          # every flow and step id
```

## 1. The five ways to run a check

| Want | Command |
|---|---|
| A whole flow | `& $py docs/v2/flow_checks.py forget` |
| One step | `& $py docs/v2/flow_checks.py forget F7.3`: the earlier steps run silently first, so the step has its preconditions |
| Several steps | `& $py docs/v2/flow_checks.py forget F7.2 F7.3` |
| The trace events of a step | add `--events`: every wiretap event the step produced (the leg hits, gate decisions, firewall verdicts…) |
| **Every line of engine code a step ran** | add `--lines`, then narrow it with `--only <path part>` and `--limit N`. Example: `& $py docs/v2/flow_checks.py forget F7.3 --lines --only trust/ --limit 400` prints `memspine/trust/erasure.py:57 forget: …` for each line, in order |
| Everything | `& $py docs/v2/flow_checks.py all` (about 2 minutes, with a summary at the end) |

Read a step like this:
```
[forget F7.4] verify the erasure across every store
    code: api/engine.py:verify_forget          <- where the behaviour lives
    verify_forget: {"record_absent": true, "vector_absent": true, "lexical_absent": true, ...}
    [PASS] verify_forget reports the erasure    <- the expected behaviour and the result
```

## 2. Every line: three more tools

1. **Line trace of a step:** `--lines`, as above. It shows the exact path through `src/memspine`.
2. **Line coverage of a flow:** which engine lines ran (green) and which never ran (red):
   ```powershell
   $env:COVERAGE_FILE = "$env:TEMP\memspine-cov\.coverage"
   & $py -m coverage run --source=src/memspine docs/v2/flow_checks.py forget
   & $py -m coverage report --include="src/memspine/trust/*"
   & $py -m coverage html -d $env:TEMP\memspine-cov\html ; start $env:TEMP\memspine-cov\html\index.html
   ```
   A red line in a function you care about means no flow exercises it yet. That is a test to add.
3. **Stop on a line and inspect variables:**
   - **Debugger:** put `breakpoint()` on the line in `src/memspine/...`, then run the single step (`… forget F7.3`). You land in `pdb`: `n` next line, `s` step into, `p var` print, `l` list, `c` continue.
   - **VS Code:** open the repo, set a breakpoint, and use *Run → Start Debugging → Python File* on `docs/v2/flow_checks.py` with args `forget F7.3`. Set `PYTHONPATH=src` in `launch.json` `env`.
   - Remove `breakpoint()` when you are done.

## 3. The flows and their steps

Run each one as `& $py docs/v2/flow_checks.py <flow> [step]`. The code location of each step is printed when it runs.

### ingest (flows 1–2, write path; stage ids A*)
| Step | Tests |
|---|---|
| F1.1 | single write: record build, `valid_from` from the supplied timestamp, layer L0, trust 0.7 (A10) |
| F1.2 | `write_messages`: stamping precision (date-only → day; with a time → instant), timezone offset normalised to UTC, speaker axis (A1, A7) |
| F1.3 | role policy: user `stated`, assistant `proposal` with lower trust (A6) |
| F1.4 | firewall: normal text clean; instruction-shaped text flagged with reason `instruction_shaped_content` (A11) |
| F1.5 | routing: episodic → `plain`, semantic → `semantic_pipeline` (A12) |
| F1.6 | projectors: the record is found by the vector leg, the BM25 leg and `search` (A13) |
| F1.7 | conflict ladder: a newer value on the same entity+attribute supersedes; the old one is archived with `evolve_to` (A25) |
| F1.8 | window extraction (offline stub): L1 facts with parents = cited turns, trust ≤ parents, "last week" resolved from the message date (A15–A24) |
| F1.9 | layers before sleep: only L0 and L1; every L1 fact has parents |

### sleep (flow 3; B*)
F3.1 prepare (two sessions) · F3.2 the per-stage report: 16 stages, none failed, each skip with a reason · F3.3 B6 consolidate: one L2 summary per session, parents from that session only · F3.4 B11 standing answers: L5 holds the current value only · F3.5 B12 compiled edges name their source fact; superseded edges are closed · F3.6 B16 decay/compress: no change on a fresh store · F3.7 a second sleep is idempotent.

### read (flow 4; C*)
F4.1 prepare (5 records, plus 1 in another namespace) · F4.2 C8 vector leg · F4.3 C9 BM25 leg · F4.4 C16 fusion equals Σ 1/(k+rank) · F4.5 C23/C25 assembled context within budget, dates rendered `[YYYY-MM-DD Dow]` · F4.6 a tiny budget keeps the relevant records · F4.7 C28 gate: abstains off-topic, answers on-topic · F4.8 namespace isolation everywhere.

### quarantine (flow 6)
F6.1 a web-channel injection is held, while a user's own imperative is only flagged · F6.2 review queue; reads hide held records · F6.3 effective trust 0 · F6.4 approve: the author is refused, a reviewer releases · F6.5 reject archives · F6.6 operator hold drains the trust of derived children · F6.7 `audit_taint` blast radius · F6.8 `rollback_taint` · F6.9 `repair_taint` · F6.10 `verify_integrity`: chain head, fingerprints, external anchor.

### forget (flow 7)
F7.1 write and find · F7.2 soft forget (status `deleted`, hidden) · F7.3 hard forget (row removed, log redacted) · F7.4 `verify_forget` across record, vector, lexical and log · F7.5 `erase_subject`.

### correct (flow 8)
F8.1 a keyed fact · F8.2 `vet` a draft · F8.3 `correct()`: old archived, new value wins search · F8.4 `vet` flags the stale value · F8.5 a firewall-held correction supersedes nothing · F8.6 `feedback` counts; inferred channel refused · F8.7 `feedback(correction)` · F8.8 `correction_latency` · F8.9 `retract` · F8.10 `review_inferred` and user confirmation.

### rebuild (flow 9)
F9.1 a state snapshot (ids + layers + status) · F9.2 `rebuild()` identical, erasures stay erased · F9.3 idempotent · F9.4 `export` deterministic, no erased text · F9.5 `export(subject=…)` · F9.6 a file-backed store in a temp dir · F9.7 `migrate_sqlite`: ids, layers and status survive; source untouched · F9.8 the CLI `python -m memspine.cli migrate SRC DST`.

### procedural (flow 10)
F10.1 `add_skill` draft · F10.2 promotion ladder with the dry-run gate · F10.3 deprecate is terminal · F10.4 `record_plan` / `recall_plan` · F10.5 `record_outcome` → lesson · F10.6 `recall_lessons`, dedup · F10.7 `prune_experience` · F10.8 `suggest_procedures`.

### tasks (flow 11)
F11.1 task state round-trip · F11.2 `update_subgoal` rules · F11.3 `task_search` · F11.4 `close_task` / reopen · F11.5 `record_trajectory` · F11.6 `trajectory_window`.

### prospective (flow 12)
F12.1 `watch` / `due` before, at and after the time · F12.2 `acknowledge_watch` · F12.3 naive datetimes refused · F12.4 invalidation watch · F12.5 firewall on watches · F12.6 reminder states (upcoming → expired) · F12.7 done/negated plans · F12.8 feature flags · **F12.9 cue-matched reminders (FAILS, see section 4)**.

### shared (flow 13)
F13.1 `grant` · F13.2 the grantee sees only granted records (capped trust, one-way) · F13.3 no grant, nothing visible · F13.4 `revoke` · F13.5 type-scoped grant · F13.6 `subscribe` · F13.7 `send` · F13.8 `principal_reputation` · F13.9 `authorize` fails closed · F13.10 `approve` + `authorize` · F13.11 cross-namespace isolation.

### graph (flow 14)
F14.1 `associate` · F14.2 `related` · F14.3 ppr / bfs / rrf · F14.4 cross-namespace refused, forgotten hidden · F14.5 `reflect`: parents, depth, trust cap · F14.6 `add_exemplar` · F14.7 `classify`.

### assets (flow 15)
F15.1 `ingest` a document into chunks · F15.2 searchable · F15.3 re-ingest is idempotent · F15.4 a changed chunk gets a new version · F15.5 empty or missing file · F15.6 `ingest_asset` reference · F15.7 description record with parent = asset · F15.8 refused while the describer is off.

### views (flow 16)
F16.1 seed two days · F16.2 `sessions` split on the silence gap · F16.3 `conversation` · F16.4 `timeline` · F16.5 `session_memories` · F16.6 `brief` · F16.7 `namespace_stats` · F16.8 `get_record` / `get_envelope` / `list_records`.

## 4. Findings from the 2026-10-15 run

**Real gap**
- **F12.9:** `reminders(cues=[…])` can never match. The cue logic reads `extras["event_cue"]` / `extras["location_cue"]` (`memory/prospective.py:_cue_text`, `_cue_hit`), but nothing in the engine sets them (`derive/pipeline/advisory.py:record_assertion` leaves `extras` empty). Cue-triggered plans ("when Bram calls, ask about the invoice") are never surfaced.

**Worth a look (each flow pins the current behaviour)**
- **Firewall false positive:** an assistant turn ("That sounds wonderful, Bram!") was quarantined as an `embedding_outlier`, because hash embeddings give zero similarity. This is probably a hash-embedder artefact; check it with the real embedder.
- **Gate false abstain:** with `relevance_gate: store_calibrated`, "Who teaches cello?" abstained although the answer is stored. Same likely cause.
- **`assemble()` has no relevance floor:** off-topic queries still fill the budget. Abstaining happens only in `read()`.
- **Hard forget logs at `[error]` level:** `memory.forget … hard=True` is noisy but harmless.
- **`export(include_history=False)` docstring:** the parameter drops only each record's `history` field, while the docstring suggests it drops archived records.
- **`feedback(correction)` content prefix:** the stored text starts with `"semantic: …"`.
- **`graph_edge_endpoints_exist`:** the 28 errors seen in step mode (`MANUAL_TESTING.md` §8) were not reproduced here. `upsert_edge` creates bare endpoint nodes, so this is probably a snapshot gap in the inspector, not missing nodes.

## 5. Adding a step

Copy a step in `docs/v2/flows/fNN_*.py`:
```python
async def my_step(ctx):
    rec = await ctx.engine.write("Noor adopted a greyhound.", namespace=ctx.ns)
    ctx.show("record", rec)                            # print what matters
    ctx.expect("layer", "L0", rec.layer)               # expected vs got -> PASS/FAIL
    ctx.state["noor"] = rec.record_id                  # for later steps
STEPS.append(Step("F16.9", "what it tests", my_step, "api/engine.py:write"))
```
`CONFIG = {...}` at the top of a module switches engine options on for that flow.
