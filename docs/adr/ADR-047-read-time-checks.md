# ADR-047 — Read-time checks: completeness round, answer verification, profile packing

- **Status:** proposed
- **Date:** 2026-10-06
- **Decision id:** master absorb list #38 (SM-12), #39 (SM-18), #40 (SM-15)
- **Phase:** v0.3 read path · **Tier:** DF

## Context

Multi-hop list questions (LoCoMo cat 1) lose points to partial evidence: the
composed context holds some of the items. Some wrong answers (cat 4, cat 1) state
something the context does not support. Both have known remedies that cost model
calls on the read path (a completeness check with missing-information queries;
verification of the answer against the context, EvolveMem-style), which memspine's
read path has so far avoided ("rules decide; no model on the read path", except the
opt-in planner and relevance filter).

## Decision

1. **Completeness check (#38), `read.completeness_check: false`.** Only reads routed
   to compose whose plan is `aggregate`, or whose question is a list or count question
   (`is_aggregation` / `is_count`), are checked: the `sufficiency` role (else `plan`)
   judges the composed context (+1 call, prompt `sufficiency`); only when it is
   incomplete does `sufficiency@missing` write up to three missing-information
   queries (+1 call); the compose read runs once more with them as extra probes.
   **One round at most**, so the cost is bounded at +2 calls. Any other read makes no
   extra call (tested with a counting fake LLM). An abstained read is never checked,
   so the check cannot talk the read out of abstaining (cat-5 safety).
2. **Answer verification (#39).** A separate verb, `Engine.verify_answer(question,
   answer, context)`, one `verify_answer` role call (else `chat`), prompt
   `verify_answer`; it returns `{supported, evidence_ids, revised_answer}`. No read
   path calls it: generation is the caller's stage. The eval harness exposes it as
   `--verify-answer` (a reader wrapper on the judge's backend; off by default, so the
   published readers, prompts and `describe()` are unchanged).
3. **Profile-header packing (#40), `read.profile_header_packing: false`.** No model
   call: summaries, then observations, then hits packed within
   `read.profile_header_budget` tokens (at most half the read budget), escaped, in a
   fixed order, with the existing `PROFILE NOTES (` marker as the header so stored
   text cannot forge it.

## Consequences

- Positive: each lever is measurable on its own; costs are fixed and stated
  (+0..2 calls per aggregate read; +1 per verified answer; 0 for packing).
- Negative / cost: the checks add latency and paid calls when enabled; both must
  pass the cat-5 abstention arm before they are promoted into a template.
- Follow-up: measure under the focused round (#81) and the cat-5 arms (#66).

## Alternatives rejected

- **Iterate until complete** — unbounded cost; one round captures most of the gain.
- **Verify inside `read()`** — `read()` returns a context, not an answer.
- **A new `[USER PROFILE]` marker** — would need a new escaping rule and change the
  default escaping; the existing `PROFILE NOTES (` marker already has one.
