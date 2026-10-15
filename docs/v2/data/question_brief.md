# MemSpine v2 decision questions: shared brief for the area agents

Goal (project owner): plan MemSpine v2 by combining everything learned from the 15-engine survey,
the gap plan, the independent review and the Codex reviews, including advanced features. Not a
change now: a PLAN. The owner will answer decision questions one by one. You write the questions
for ONE architecture area. Every question must be generic (any memory workload: chat assistants,
agents, documents, multi-user), never tuned to LoCoMo or any benchmark.

## Sources (read what your area needs; do not read everything)
- Independent review page (2.2 MB HTML, use python/grep to pull sections by id: read-first, codex,
  flows, decisions, proposal, layers, gaps, phases, prompts, engines, questions):
  `C:/Users/hembad/Documents/GitHub/memspine/docs/engine_survey/independent_review_2026-10-11/index.html`
  plus `review_decisions.json` (RV01-RV22, decisions with finding, evidence, solution, gate) and
  `codex_verification.json` in the same folder.
- Engine records (14 phases x 57 rows each, with extras, token economy, speed, specialities):
  `C:/Users/hembad/Documents/GitHub/memspine/docs/engine_survey/<engine>.json` for memspine, combined,
  memos, memu, mnemon, honcho, memmachine, hindsight, mirix, cognee, memori, nemori, lightmem, mem0,
  evermemos; `SPEC.md` explains the schema; `synthesis.json` the decision section;
  `prompt_matrix.json` prompt features F1-F23; `gap_plan.json` 41 gaps with changelog.
- Plans: `C:/Users/hembad/Documents/GitHub/memspine/docs/GAP_TO_SOLUTION_PLAN.md`,
  `C:/Users/hembad/Documents/GitHub/memspine/evals/analysis/GAP_REGISTER.md`, `docs/adr/*.md`
  (ADR-064 LadybugDB, ADR-065 Graphiti gaps, ADR-066 adaptive window, ADR-026 multi-call write).
- Today's research (online graph call G-W49, Graphiti trace, 15-engine write-time survey, Codex
  second opinion with prompt v2, G-W50 key identity, G-E1 gate hardening): already summarised in the
  gap_plan.json changelog and GAP_REGISTER progress log (2026-10-11 entries).
- Engine clones for spot checks: `C:/Users/hembad/AppData/Local/Temp/claude/C--Users-hembad-Documents-GitHub-memory-research/740f1bef-daa9-480a-95a1-041d8fe2753f/scratchpad/clones/`
  (graphiti, mem0, hindsight, honcho, lightmem, cognee, memori_v2, mnemon, memos, memmachine, mirix,
  nemori, memu, evermemos-*). `combined-src` is INTERNAL: never copy credentials, hostnames, URLs or
  account ids from it.
- MemSpine code: `C:/Users/hembad/Documents/GitHub/memspine/src/memspine/`.

## Paper vocabulary (use it in `paper`)
Loop operators: R retrieve, C compose, G generate, D deposit, K_C consolidate, K_P personalise.
Memory types: working, episodic, semantic, procedural, reflective, resource, associative,
prospective, shared. Ladder L0-L5 (personalisation level). Impact order O1 direct, O2 mediated,
O3 systemic.

## Output: write ONE file `<scratchpad>/v2q/<area_id>.json`
(`<scratchpad>` = `C:/Users/hembad/AppData/Local/Temp/claude/C--Users-hembad-Documents-GitHub-memory-research/740f1bef-daa9-480a-95a1-041d8fe2753f/scratchpad`)
A JSON array of 22 to 26 question objects, valid JSON (check with python json.load), UTF-8:

```json
{
  "id": "W-01",                      // area prefix + number
  "area": "<area name>",
  "phase": "<one of the 14 survey phases or a short sub-topic>",
  "title": "short decision name (max 8 words)",
  "question": "the decision as a question, one sentence",
  "today": "what MemSpine does now, with file:line or config key",
  "others": "how 2-4 named engines do it (one line each, concrete)",
  "options": [
    {"key": "A", "label": "short label", "detail": "one or two sentences",
     "used_by": ["engine names"],
     "impact": {"quality": "+2..-2", "latency": "+2..-2", "cost": "+2..-2",
                "complexity": "+2..-2", "risk": "+2..-2"},
     "note": "the main trade-off in one line"}
  ],
  "recommended": "A",
  "why": "one or two sentences: why this option, with evidence (review id RVxx, gap id, measured result, ADR)",
  "depends_on": ["other question ids, if any"],
  "gaps": ["G-W.., RVxx, BD-.."],
  "paper": {"layer": "memory type(s)", "operator": "R|C|G|D|K_C|K_P"},
  "v2_scope": "core | advanced | later"
}
```
Rules: 3 to 5 options per question, always including today's behaviour as an option when it is a
real choice, and an "advanced" option when one exists in the field. Impact scale: +2 much better,
-2 much worse, from the user's point of view (for cost/latency/complexity/risk, + means cheaper,
faster, simpler, safer). Questions must be real decisions (no yes/no trivia), not duplicated across
areas (stay inside your area list), ordered from foundational to detailed. Use British spelling. Never
quote benchmark dataset text; use invented names (Ilse, Bram) in examples. Return a 5-line summary
as your final answer (count, top 3 decisions), not the JSON.
