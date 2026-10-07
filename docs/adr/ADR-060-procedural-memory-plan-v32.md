# ADR-060: Procedural memory for agents without a model (lessons, plan top-k, trajectories, corrections, task state, kNN vote, quarantine lessons, document-type trust)

- **Status:** accepted (every automatic behaviour off by default; the new verbs are additive)
- **Date:** 2026-10-07
- **Decision id:** D-84 (D-83 was not yet in the register when this was written; the number is reserved by the plan). Plan v3.2 rows W17a–f and N24; S4 gaps N26–N29.

## Context

The S4 agentic and multi-agent study (`paper_spine/evaluation/sota/S4_AGENTIC_MULTIAGENT.md` §10–§12) found:

- **No experience memory.** `record_plan` captured a success; nothing captured a failure, and `recall_plan` returned one plan. EvoMemBench, ReasoningBank, AWM and ACE all learn from failures too, and keep counts of what helped.
- **Distilled-only memory loses.** In MemoryArena the systems that keep only distilled notes are the worst group: notes must sit next to the evidence, not replace it.
- **Transcript notes degrade.** VibeMemBench saw 69% form degradation in free-text notes. A fixed-field template does not degrade.
- **Trajectories have no shape.** LME-V2 AgentRunbook-C (no model at insert, .749) keeps a per-trajectory summary record and reads raw slices around a hit.
- **Corrections are lost.** StreamMemBench follow-up reuse (MemOS at FUR 4) needs a correction to reach a later, different task.
- **Task state drifts.** CoMemBench reads completed steps as pending; MemoryArena's pooled probe is all@10 = 0 for shopping and travel without task scoping.
- **Classification by examples.** Mem-α's label table and a BM25 k = 5 baseline (0.605 / 0.562 / 0.429) show a kNN vote over stored examples is a cheap win.
- **Quarantine forgets the attack.** A held write left no reusable trace; the next attack from the same source starts from zero.
- **Trust is role × channel only.** CPB b7 governance (document-type authority plus a request-evidence hold) is best on all four agent families.

Every item below uses rules, arithmetic or the existing embeddings. No model call is added anywhere.

## Decision

**1. Lessons (W17a, N28).** New procedural kinds `lesson` and `mapping` and a new stage `advisory`.
- An advisory record is held out of ordinary search (status `resolving`), read only through its own verbs, never executable, never promotable (`next_stage` raises).
- `Engine.record_outcome(task, outcome, action=, error=, next_action=, reward=, tool=, used_ids=, derived_from=)` builds the lesson from a fixed template: "When ⟨task⟩ · tried ⟨action⟩ · failed because ⟨error⟩ · worked ⟨next action⟩".
- Trust is capped at the `derived_from` parents (MTI-D, when integrity is on).
- The dedup key is (trigger words, action signatures with literals removed, error class). A repeat adds a FEEDBACK note to the live lesson instead of a row.
- Counters ride existing events: `scoring.likes` = helpful, `dislikes` = harmful (one FEEDBACK per used plan or lesson on each receipt), `access_count` = used (RETRIEVE), `notes` = repeats.
- Read (`memories.procedural.policies.lessons: {inject: true}`): up to `LESSON_BLOCK_MAX` lessons, marked as advisory data, go **after** all evidence in `assemble`. Their cost comes out of the budget first.

**2. `recall_plans(task, k)` (W17b, N28).**
- Returns the top-k usable plans, k from `memories.procedural.policies.recall_k`, else 3.
- Ranked by `cos × (1 + helpful) / (1 + harmful)`; one plan per action signature.
- A plan whose failures dominate (≥ 2 harmful and more harmful than helpful) is pruned.
- `include_lessons` lets lessons compete in the same ranking.
- `recall_plan` (top-1) is unchanged.
- `prune_experience()` deprecates plans and lessons whose failures dominate, or that were used at least 5 times with a helpful share below 0.2.
- `policies.auto_verify_on_reward`: a receipt with reward ≥ 1 advances a used `staged` plan to `verified`; `active` still needs the dry run.

**3. Trajectories (W17c, N27).**
- `Engine.record_trajectory(goal, steps, outcome, reward=, trajectory_id=)` writes one episodic record per step (`group_id` = trajectory id, tag `step:<i>`). Each step stores its observation as a line diff against the previous one.
- It also writes a head record: a deterministic manifest (goal, start, action signature, outcome / reward, step count).
- `trajectory_window(record_id)` returns ±1 steps inside the group, capped at 20.
- `policies.trajectory: {expand: true}` adds those neighbours after a hit in `assemble`, while the budget allows.

**4. Correction detector (W17d).**
- `memories.episodic.policies.correction_detector` turns on rules for user turns in `write_messages`:
  - framings: "no, I said", "actually it's", "not X, Y", "I meant", "correction:", "that's wrong";
  - skipped: quoted or reported speech and stock refusals.
- A matched turn is tagged `correction`.
- The target is the live keyed semantic fact with the highest word overlap on the negated span (or on the latest assistant turn within 5 turns), at overlap ≥ 0.5.
- With a replacement, the fact is superseded on its key through the normal door (the M4 ladder). Without one, it is retracted.
- With `procedural.policies.lessons`, a lesson records the correction so a different later task can retrieve it.

**5. Task state (W17e).**
- `set_task_state`, `update_subgoal`, `close_task`, `task_state` and `task_search` manage one working record per task (channel `task_state`, `group_id` = task id).
- It holds JSON goal, constraints, subgoals and budget. Each update is a keyed supersede in place (the persona pattern), so every version stays on the log.
- `done` requires a receipt id.
- With `policies.task_state`, `assemble(..., session_id=<task id>)` pins the rendered state right after the persona until the task is closed.
- `task_search` reads the task's group first, then the namespace.
- Like the persona, the state is engine-built and bypasses the firewall. Goal text is caller-supplied, so a deployment that takes it from untrusted input should screen it first.

**6. kNN label vote (W17f, N29).**
- `add_exemplar(text, label, group=)` stores advisory `mapping` records.
- `classify(text, group=, k=5)` fuses BM25 and dense cosine by reciprocal rank and runs a score-weighted vote.
- It returns the label, margin, votes, exemplars and a label table: per label, its top tf-idf terms over its own exemplars.

**7. Quarantine lesson (N24).**
- With `memories.procedural.policies.quarantine_lesson`, every quarantine verdict writes a lesson naming the source signature (role / channel / document type), the reason kinds and the content hash.
- It **never** copies the held text.
- Repeats from one source and reason set add notes (N28).

**8. Document-type tier and needs-evidence hold (N26)**, under `memories.semantic.policies.trust` (the trust policy's existing home).
- `source_types` maps a document type (`doctype:<t>` tag, else the channel) to tier 3 / 2 / 1 / 0.
- Trust is capped per tier (1.0 / 0.6 / 0.45 / 0.3).
- `hold_needs_evidence` quarantines a non-privileged write below `authority_min_tier` (2) with a `pending_evidence(tier=…)` reason.
- `Engine.review_evidence(id, supporting_ids, contradicting_ids)` returns one of three verdicts:
  - `promote` (≥ 1 authoritative live supporter, no contradiction) releases the record;
  - `disputed` keeps it held;
  - `pending_evidence` keeps it held.

## Consequences

- **Defaults unchanged.** `profile=simple` and `core` reads and writes are unchanged with every key off.
- **Golden updated.** The simple-profile golden gains the three new trust-policy options at their defaults: `source_types: {}`, `hold_needs_evidence: false`, `authority_min_tier: 2`.
- **No schema change.** No new config section and no new event kind: counters reuse FEEDBACK and RETRIEVE, stages reuse DECAY_TRANSITION, task state reuses WRITE. A rebuild replays all of it.
- **English-only rules.** The correction detector is English-only and conservative: it acts only on keyed facts, so a missed correction costs nothing and a false one needs a ≥ 0.5 overlap with a live fact.
- **Not yet measured.** Measurements (EvoMemBench, MemoryArena, LME-V2, PersonaMem / TWIST capture rate, LoCoMo false-correction rate, CPB) are follow-up eval rows. This ADR adds the mechanisms only.
