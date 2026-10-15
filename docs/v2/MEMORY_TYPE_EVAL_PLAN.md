# Memory-type evaluation plan (MF-16), 2026-10-15

Why this exists: the J arm (`evals/arms/v2-PICK3-j.json`) is retrieval-only. It has no worker model,
no offline stages and `lineage_table` off, so it cannot exercise the derived stack (L2..L5). A
headline score from it says nothing about reflective, procedural, prospective or associative memory.
This plan evaluates each of the nine memory types on its own, then in interaction, with the same
budget on every arm and the effective flags written into every result row.

Layer map (`store/layers.py`): working, episodic, resource, shared = L0; semantic = L1 (summaries
and episodes L2); associative = L3; reflective = L4; procedural, prospective = L5.

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
| reflective | L4 | `offline.patterns_v2` (observations) | observation records in evidence block | preference and pattern questions (PrefEval); preference-follow accuracy | L4 count > 0 |
| procedural | L5 | `offline.standing_v2` plus `read.standing_v2`; profile card via `offline.profile_card_v2` | standing block | repeated-instruction and how-to tasks; success of the standing answer | L5 count > 0 and `read.standing_v2` on |
| prospective | L5 | prospective memory enabled, anticipation cues | cue records, time-triggered read | reminder and future-event questions; due-recall rate, false-trigger rate | L5 count > 0; cue count logged |
| shared | L0 grants | `memories.shared` with two or more principals | scoped read | cross-agent visibility: leak rate (must be zero), authorised recall | principals and grants logged; leak rate reported even at zero |
| resource | L0 | resource intake (`write.intake_v2`, asset describer) | resource records | document and asset questions; F1 on the doc subset | resource count > 0 |

Prospective, shared and resource need purpose-built slices because the five-benchmark suite barely
touches them. Their slices are small and synthetic (invented names only); report them as
diagnostics, not as headline scores.

## 3. Held-out slices

| Slice | Source | Types it exercises | Notes |
|---|---|---|---|
| H-temporal | held-out sessions, temporal categories | episodic, working | built before any derived-layer arm runs |
| H-update | knowledge-update questions | semantic, reflective | tests supersede/contest behaviour end to end |
| H-multihop | multi-hop questions | associative, semantic | graph leg claimed only if its hits are non-empty |
| H-pref | PrefEval-style preference probes | reflective, procedural | reader prompt fixed across arms |
| H-synth | synthetic, invented names | prospective, shared, resource | diagnostics; no dataset text |

## 4. Individual, then interaction

1. **Isolation pass.** For each type, J versus J+type on its own slice and on the shared general
   slice. The second number checks the type does not hurt questions it is not for (regression
   guard). One seed minimum for screens; three for anything that gates adoption.
2. **Pairwise interaction.** Only the pairs that share a producer or a read path:
   semantic x episodic (shared turns as lineage parents), semantic x associative (facts as edge
   records), episodic x reflective (observations over sessions), reflective x procedural (L4 feeding
   L5 standing answers), procedural x prospective (both L5). For each pair report
   `score(A+B) - max(score(A), score(B))` and `score(A+B) - score(A) - score(B) + score(J)`
   (interaction term), with the same matched budget. A pair is called synergistic only if the
   interaction term clears the paired-bootstrap interval.
3. **Full stack.** All nine on, versus the best single-type arm and versus J, at matched budget and
   at 2x budget, to separate "more memory types" from "more tokens or calls".
4. **Leave-one-out.** From the full stack remove one type at a time. A type whose removal changes
   nothing is either redundant or inert; the effective-flag record tells which.

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
