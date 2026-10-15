# MemSpine v2 area chapter: brief for the area agents

The owner answered all 155 v2 architecture decisions (`docs/v2/data/decisions_answered.json`, each question has
`answer: {choice, note, took_recommendation}`). 149 took the recommendation; 6 chose differently; 24 notes combine
options (for example "A + B" or "combine B + C") or add a request ("SQL graph to neo4j", "also keep participant tag
too", "add extra, not remove communities"). Read every note literally as the owner's intent: a note like "A + B"
means the owner wants both behaviours, normally as the chosen option plus the other kept as a mode, fallback or
first stage, not one replacing the other.

v2 is a SEPARATE line of work: plan and documents only, no code change in this task. Do not edit anything outside
`docs/v2/` in the worktree `C:/Users/hembad/Documents/GitHub/memspine-v2` (branch `v2/plan`). v1 code to read is in
the same worktree under `src/memspine/` (identical to v1 HEAD). Never quote benchmark dataset text; use invented
names (Ilse, Bram).

## Your job for ONE area (only the questions with your prefix)

1. **Verify every answer.**
   - Took the recommendation: confirm it still holds given the owner's other answers (dependencies inside and
     across areas; read the other areas' answers in the same file when a dependency points there).
   - Chose differently: state what follows from that choice; list every other decision whose recommendation
     assumed the recommended option and is now affected; say whether that other decision must be revisited.
   - Combined or added options: define the combined design precisely (which is default, which is a mode, which is a
     fallback, in what order they run); say whether the combination is compatible; if it is not, give the closest
     compatible reading and mark it "needs owner confirmation".
   - Verdict per question: `ok`, `ok-combined`, `conflict` (with which ids), `needs-measurement` (a screen or
     benchmark must run before building; name it and the gate), `needs-research` (an open question; name it), or
     `needs-owner` (a question to put back to the owner, one sentence).
2. **Predict what will happen** for each decision: behaviour change, expected effect on quality, latency, cost and
   risk (direction and rough size, marked "estimate" unless measured, citing measured numbers where the repo has
   them: GAP_REGISTER.md, run results), and which memory type / loop operator it touches.
3. **Say where it happens**: the v1 files and functions it replaces or changes (file:line), and the proposed v2 module
   or component name. Do not write code; one short interface sketch per major component is allowed.
4. **Migration from v1** and **tests/gates** in one line each.

## Output (two files)

- `docs/v2/areas/<area_id>.md`, Markdown, British spelling, plain language:
  - `# <Area name>`; a short intro (what this area decides, how many questions, how many took the recommendation,
    the notable differences);
  - `## Design in one page`: the v2 design of this area as decided, as a short ordered list or table;
  - `## Decisions`: one `###` section per question: `### <id> <title>: <choice label>` then bullets
    **Decision** (with combination defined), **Verdict**, **What will happen**, **Where** (v1 file:line -> v2
    component), **Depends on / affects**, **Migration**, **Test or gate**;
  - `## Conflicts and open points` (table: id, issue, proposal, needs);
  - `## Measurements to run before building` (table: what, which screen or dataset, gate);
  - `## Change log` with one line dated 2026-10-11.
- `docs/v2/data/verify_<area_id>.json`: an array, one object per question:
  `{"id", "choice", "combined": "text or empty", "verdict", "conflicts": [ids], "needs": "text or empty",
  "impact": {"quality","latency","cost","risk"} (each a short string), "v1_locations": ["file:line"],
  "v2_component": "name", "operators": ["R|C|G|D|K_C|K_P"], "memory_types": [..]}`; validate with python json.load.

Final answer: five lines (counts by verdict, the top conflicts, the measurements needed).
