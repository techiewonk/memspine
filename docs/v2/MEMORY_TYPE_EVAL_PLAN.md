# Memory-type evaluation plan (MF-16), 2026-10-15

Why this exists: the J arm (`evals/arms/v2-PICK3-j.json`) is retrieval-only. It has no worker model,
no offline stages and `lineage_table` off, so it cannot exercise the derived stack (L2..L5). A
headline score from it says nothing about reflective, procedural, prospective or associative memory.
This plan evaluates each of the nine memory types on its own, then in interaction, with the same
budget on every arm and the effective flags written into every result row.

Layer map (`src/memspine/store/layers.py`): working, episodic, resource, shared = L0; semantic facts
= L1; semantic session summaries/episodes = L2; associative graph records = L3; reflective records
and semantic observation-channel records = L4; procedural records, prospective watches, and semantic
standing-answer records = L5. These are storage layers, not an assertion that nearby mechanisms are
the same memory type. Standing answers are semantic answer caches on L5, not proof of procedural skill
learning; profile cards are event-log markers (not layered records); prospective watches are trigger
records and are distinct from anticipation cues (retrieval keys on answering turns); resource records
describe assets/documents and remain their own type.

## 1. Rules for every run

1. **Record effective flags.** Before a run, `python -m memspine_evals.memory_type_matrix <arm.json>
   --json` and store the output in the result row (`effective_flags`). After the run, store
   `storage.layer_counts(ns)` per namespace (`layer_counts_after`). A layer that is enabled but empty
   after the run means the producer was inert; the row is then labelled `inert:<producer>` and is not
   counted as evidence for that memory type.
2. **Held-out tasks.** Tune only on the dev split. Each type has a held-out slice (section 3) built
   with `make_split.py` before any arm is run, frozen with `freeze_blind_slices.py`, and read once.
   No dataset text enters the repo; slices are referenced by id and hash.
3. **Matched budgets.** Within one comparison all arms get the same reader model, the same prompt
   token cap for the evidence block (`read.exact_budget`), the same top-k, and the same wall-clock
   and call budgets. Report tokens in, tokens out, model calls and p50/p95 latency per question, for
   read and for write/offline separately. An arm that spends more write-time model calls than its
   comparator is reported with cost per point, not only the point.
4. **Paired design.** Same questions across arms; paired bootstrap (`paired_gate.py`) with the
   average-led adoption rule from the eval scope note. Report the delta against the matched
   ablation, not against an unrelated baseline.
5. **Contract tests first.** `tests/unit/contract/test_layer_contract.py` must pass before any
   derived-layer arm is run. An arm run on a tree where session-close records land on L1 measures
   the bug, not the memory type.
6. **Prerequisite flags are exact.** Validate the resolved config (including `config/requires.py`)
   before each run; do not infer a prerequisite from a feature name. Relevant hard requirements are
   `read.standing_v2` → `offline.standing_v2`; `read.profile_card_header` →
   `offline.profile_card_v2` → `offline.session_close_v2`; `write.asset_describer` →
   `write.intake_v2`; and `read.graph_leg_v2` → `read.legs_v2` plus `graph.edge_compiler_v2`.
   Type enablement is `memories.<type>.enabled`: prospective watches need
   `memories.prospective.enabled: true`; anticipation is separately the episodic consolidation policy
   `memories.episodic.policies.consolidation.anticipate: true` and a bound `anticipate` (or fallback
   `extract`) role. Procedural lifecycle needs `memories.procedural.enabled: true`. Record resolved
   role bindings, sleep invocation, type enablement and read flags in the row. `offline.patterns_v2`
   and `offline.standing_v2` are opt-in sleep stages; do not assume either runs merely because its
   corresponding memory type is enabled.

## 2. Per memory type: ablation and metric

Each type is switched on over the J arm as the only change (`+type`), compared with J (`-type`),
using the layer's producer and the leg that reads it. Checks in the last column are what must be true
in `effective_flags` and `layer_counts_after` for the row to count.

| Type | Layer | Switch on (over J) | Reads through | Task family and metric | Row valid only if |
|---|---|---|---|---|---|
| working | L0 | window / context assembly options (`read.assembly`), no store change | assembly | in-session recall, long-context pressure; F1, tokens kept | effective types show working; token counts logged |
| episodic | L0 (+ L2 episodes) | `memories.episodic`, `offline.session_close_v2` for episodes | vector, lexical, temporal, episode leg | temporal and multi-session questions (LoCoMo cat 2/3, OP-Bench); F1 / judge | L2 count > 0 when the episode leg is claimed; lift returns source turns |
| semantic | L1 | `memories.semantic`, `write.window_extract: facts`, `write.envelope_v2` | vector, lexical, entity leg | single-hop and knowledge-update (LoCoMo cat 1/4, MAB-CR); F1, update accuracy | L1 count > 0 and above raw-turn count not dominated by user writes |
| associative | L3 | `memories.associative`, `graph.edge_compiler_v2`, `read.graph_leg_v2`, `extract_graph` | graph leg, entity summaries | multi-hop, who/what-connects questions; F1 on multi-hop subset | graph leg `on`, edges compiled, hit count > 0 (an empty graph leg is `inert`) |
| reflective | L4 | `offline.patterns_v2` produces semantic `observation` channel records; `consolidation.reflect_profile` produces reflective records; profile card is separately `offline.profile_card_v2` | observation and/or `read.profile_header`; card via `read.profile_card_header` | held-out preference/pattern probes; accuracy by representation, evidence support and stale/forgotten-support rejection | State the mechanism under test. For patterns require observation-channel L4 records and retrieval hits; for profile reflection require reflective records; for the card require a latest card marker with live cited support and header inclusion. Do not pool these counts. |
| procedural | L5 | skill API (`add_skill`, `promote_skill`, `deprecate_skill`, `skills`; `memories.procedural.enabled`); task state/outcome APIs are a separate mechanism | `skills(kind="skill")`, usable-only active selection; task state/outcomes evaluated separately | held-out repeated-task transfer; skill applicability/selection, staged promotion, dry-run gate, deprecation | Verify `add_skill` creates a draft; only legal one-step promotion draft→staged→verified→active succeeds; verified→active requires `dry_run_passed=True`; active skills are returned by usable-only `skills`; deprecated skills are no longer offered; illegal promotions fail and repeated deprecation is idempotent. Keep task progression and task-close outcomes separate from skill lifecycle scores. |
| prospective | L5 | `Engine.watch` / `memories.prospective`; separate optional `consolidation.anticipate` writes cues to answering semantic turns | `due()` / `pending()` and acknowledge; anticipation cues affect retrieval keys, not assembled evidence | synthetic future-event probes; due and invalidation precision/recall, false fires, acknowledgement and cue-assisted retrieval | Watches: verify due watch fires only at/after due time; target watch fires on matching invalidation; acknowledgement cancels repeat delivery; repeated sweep is idempotent (no duplicate firing). Report due and invalidation separately. Cues: verify cue tags/keys and retrieval hit independently; never count as a watch. |
| shared | L0 grants | `memories.shared` with two or more principals | scoped read | cross-agent visibility: leak rate (must be zero), authorised recall | principals and grants logged; leak rate reported even at zero |
| resource | L0 | document ingestion is `ResourceMemory.ingest` / `Engine.ingest` (extract, chunk, write resource records); `write.asset_describer` configures a separate image-asset description path through the intake gateway | resource-record retrieval for documents; asset evidence path for image assets | held-out document questions; retrieval/answer accuracy and source-document attribution. Test image-asset description separately. | Require resource chunks with `source.doc_path` and chunk IDs; verify re-ingest idempotency and answer attribution to the right document. Record image asset mode separately; do not attribute document ingestion to `write.intake_v2` or count asset descriptions as resource records. |
| standing answers (semantic mechanism; cross-cutting diagnostic, not a tenth type) | L5 | `offline.standing_v2` builds cached answers from keyed semantic fact histories; `read.standing_v2` selects/includes them | standing answer block and router `profile` leg | keyed-state questions; answer accuracy, freshness after value change, contested-state handling, rebuild cost | Require current L5 `standing` channel records with supporting semantic parents, matching dependency fingerprint, and read inclusion; verify invalidation/rebuild after source change. Do not score as procedural skill evidence. |
| profile card (profile representation; cross-cutting diagnostic) | event-log marker, no record layer | `offline.profile_card_v2` at session close; `read.profile_card_header` consumes latest marker | profile card header | profile attribute accuracy, evidence support, forgotten-support invalidation, header budget | Require latest `profile_card_v2` marker and header inclusion; cited support must remain live. Report marker count and coverage, not L-layer counts. |

Prospective watches, procedural lifecycle, shared and resource need purpose-built slices because the five-benchmark suite barely
touches them. Their slices are small and synthetic (invented names only); report them as
diagnostics, not as headline scores.

## 3. Held-out slices

| Slice | Source | Types it exercises | Notes |
|---|---|---|---|
| H-temporal | held-out sessions, temporal categories | episodic, working | built before any derived-layer arm runs |
| H-update | knowledge-update questions | semantic, reflective | tests supersede/contest behaviour end to end |
| H-multihop | multi-hop questions | associative, semantic | graph leg claimed only if its hits are non-empty |
| H-pref | PrefEval-style preference probes | reflective observations, profile card, standing answers | score each mechanism separately; reader prompt fixed across arms |
| H-proc | synthetic repeated tasks with explicit success/failure/done events | procedural | lifecycle transitions, progression, selection and close; diagnostics |
| H-watch | synthetic due-time and target-invalidation scenarios | prospective watches | fire/cancel/duplicate/idempotence checks; diagnostics |
| H-synth | synthetic, invented names | shared, resource, anticipation cues | diagnostics; no dataset text |

## 4. Individual, then interaction

1. **Isolation pass.** For each type, J versus J+type on its own slice and on the shared general
   slice. The second number checks the type does not hurt questions it is not for (regression
   guard). One seed minimum for screens; three for anything that gates adoption.
2. **Pairwise interaction.** Only the pairs that share a producer or a read path:
   semantic x episodic (shared turns as lineage parents), semantic x associative (facts as edge
   records), episodic x reflective (observations over sessions), procedural x prospective (a fired
   watch can close a task). Standing-answer interactions should be included only where a concrete
   source/read path is exercised in the arm; no L4-to-standing-answer dependency is assumed.
   For each pair report
   `score(A+B) - max(score(A), score(B))` and `score(A+B) - score(A) - score(B) + score(J)`
   (interaction term), with the same matched budget. A pair is called synergistic only if the
   interaction term clears the paired-bootstrap interval.
3. **Full stack.** All nine on, versus the best single-type arm and versus J, at matched budget and
   at 2x budget, to separate "more memory types" from "more tokens or calls".
4. **Leave-one-out.** From the full stack remove one type at a time. A type whose removal changes
   nothing is either redundant or inert; the effective-flag record tells which.

No held-out quality or cost figures are asserted by this plan. Record exact results only after the
frozen slices have been run: paired score and uncertainty; retrieval/answer quality by mechanism;
watch due/invalidation precision and cancellation/duplicate outcomes; stale-state accuracy; tokens
in/out; read/write model calls; and p50/p95 latency. Mark unrun measurements `pending`; a configured
arm or passing contract test is not benchmark evidence.

## 5. Failure labels

Every wrong answer in an isolation or interaction run is labelled by the first stage that lost the
evidence, using the loop stages: not deposited (D), deposited on the wrong layer (D, label bug),
not retrieved (R), retrieved but dropped in assembly (C), present but misread (G). The
contract-test class of failure (wrong layer, empty leg, inert producer) is reported as a
configuration failure and excluded from the memory-type comparison rather than counted against the
type.

## 6. Row schema additions

Each result row gains: `effective_flags` (output of `memory_type_matrix.effective_flags`),
`layer_counts_after`, `inert_producers` (producers on but with no records on their layer),
`tokens_in`, `tokens_out`, `model_calls_read`, `model_calls_write`, `latency_p50_ms`,
`latency_p95_ms`, `slice_id`, `seed`.

## 7. Open points for the owner

* `memory_type_matrix` maps producers to config keys by hand (`PRODUCERS`, `LEGS`); update it when a
  producer or leg gains a flag, or add a test that fails on an unknown flag.
* "Enabled" is not "populated": the helper reports configuration, `layer_counts_after` reports
  what happened. Both go in the row.
* The consolidation producer is detected from `memories.episodic.policies.consolidation`; confirm
  that is the switch you use in arms.
