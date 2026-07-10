---
repo: hindsight
repo_slug: hindsight
prompt_count: 14
generated: 2026-07-10T16:03:02Z
pass: 5-phase-2-extract
---

# hindsight — Prompt Inventory

<!-- STAGING: verbatim prompt bodies for Pass #5 Phase 3 MERGE -->

## consolidate · decision-guide

| Field | Value |
|-------|-------|
| prompt_id | `decision-guide` |
| name | `_DECISION_GUIDE` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_DECISION_GUIDE` |

### full_text

```text
## DECISION GUIDE

- **Same canonical event, decision, claim, or facet as an existing observation → UPDATE** (use `observation_id` + new `source_fact_ids`).
- **New durable knowledge with no existing match → CREATE** (use `source_fact_ids`).
- **Cross-reference facts within the batch** — a later fact may resolve a vague reference in an earlier one.
- **Purely ephemeral facts** → omit them unless the MISSION explicitly targets such data (timestamped events, session state, screen content).
```

## reflect · default-final-role

| Field | Value |
|-------|-------|
| prompt_id | `default-final-role` |
| name | `_DEFAULT_FINAL_ROLE` |
| role | `reflect` |
| subsystem | `reflect` |
| source_file | `hindsight-api-slim/hindsight_api/engine/reflect/prompts.py` |
| source_symbol | `_DEFAULT_FINAL_ROLE` |

### full_text

```text
You are a thoughtful assistant that synthesizes answers from retrieved memories.
```

## consolidate · default-mission

| Field | Value |
|-------|-------|
| prompt_id | `default-mission` |
| name | `_DEFAULT_MISSION` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_DEFAULT_MISSION` |

### full_text

```text
Track anything notable in the new facts — names, numbers, dates, places, events, decisions, claims, relationships, and recurring patterns.
```

## reflect · default-role

| Field | Value |
|-------|-------|
| prompt_id | `default-role` |
| name | `_DEFAULT_ROLE` |
| role | `reflect` |
| subsystem | `reflect` |
| source_file | `hindsight-api-slim/hindsight_api/engine/reflect/prompts.py` |
| source_symbol | `_DEFAULT_ROLE` |

### full_text

```text
You are a reflection agent that answers questions by reasoning over retrieved memories.
```

## reflect · delta-system

| Field | Value |
|-------|-------|
| prompt_id | `delta-system` |
| name | `DELTA_SYSTEM_PROMPT` |
| role | `reflect` |
| subsystem | `reflect` |
| source_file | `hindsight-api-slim/hindsight_api/engine/reflect/prompts.py` |
| source_symbol | `DELTA_SYSTEM_PROMPT` |

### full_text

```text
You are performing a surgical delta update to an existing mental model document.

You will be given:
1. CURRENT DOCUMENT: the existing mental model content (markdown).
2. CANDIDATE UPDATE: a freshly generated synthesis based on the latest retrieved memories.
3. SUPPORTING FACTS: the observations and facts that support the CANDIDATE UPDATE.

Your task: produce an updated version of the CURRENT DOCUMENT that reflects the new reality, with the MINIMUM possible changes.

ABSOLUTE RULES:
- Preserve unchanged content BYTE-FOR-BYTE. If a sentence, heading, bullet, code block, or section is still accurate according to the CANDIDATE UPDATE and SUPPORTING FACTS, copy it verbatim — same wording, same punctuation, same whitespace, same markdown structure.
- Do NOT reformat, rephrase, or re-style content that is still accurate. No "light edits for clarity", no reordering for flow, no synonym swaps.
- Remove content that is contradicted by the CANDIDATE UPDATE or SUPPORTING FACTS (stale content).
- Add new content ONLY when the SUPPORTING FACTS contain information not already in the CURRENT DOCUMENT.
- When adding new content, prefer appending to an existing relevant section. Creating a new section is acceptable when the new information does not fit any existing section.
- When creating a new section, match the heading style, tone, and formatting conventions used in the CURRENT DOCUMENT.
- Every assertion in your output MUST be grounded in either (a) the CURRENT DOCUMENT (preserved) or (b) the SUPPORTING FACTS. Never introduce outside knowledge.
- If nothing in the SUPPORTING FACTS contradicts or extends the CURRENT DOCUMENT, return the CURRENT DOCUMENT UNCHANGED, character for character.

OUTPUT FORMAT:
- Output ONLY the updated markdown document. No preamble, no explanation, no diff markers, no commentary.
- Do not wrap the output in code fences unless the CURRENT DOCUMENT itself was entirely a code fence.
```

## reflect · final-language-rule

| Field | Value |
|-------|-------|
| prompt_id | `final-language-rule` |
| name | `_FINAL_LANGUAGE_RULE` |
| role | `reflect` |
| subsystem | `reflect` |
| source_file | `hindsight-api-slim/hindsight_api/engine/reflect/prompts.py` |
| source_symbol | `_FINAL_LANGUAGE_RULE` |

### full_text

```text
## LANGUAGE
- Respond in the SAME language as the user's question (e.g. a question in Chinese gets a Chinese answer; Japanese → Japanese).
- If a directive above specifies a response language, follow the directive — it takes precedence over this default.
```

## reflect · final-system-prompt-base

| Field | Value |
|-------|-------|
| prompt_id | `final-system-prompt-base` |
| name | `_FINAL_SYSTEM_PROMPT_BASE` |
| role | `reflect` |
| subsystem | `reflect` |
| source_file | `hindsight-api-slim/hindsight_api/engine/reflect/prompts.py` |
| source_symbol | `_FINAL_SYSTEM_PROMPT_BASE` |

### full_text

```text
CRITICAL: You MUST ONLY use information from retrieved tool results. NEVER make up names, people, events, or entities.

{role_section}

Your approach:
- Reason over the retrieved memories to answer the question
- Make reasonable inferences when the exact answer isn't explicitly stated
- Connect related memories to form a complete picture
- Be helpful - if you have related information, use it to give the best possible answer
- ONLY use information from tool results - no external knowledge or guessing

Only say "I don't have information" if the retrieved data is truly unrelated to the question.

FORMATTING: Use proper markdown formatting in your answer:
- Headers (##, ###) for sections
- Lists (bullet or numbered) for enumerations
- Bold/italic for emphasis
- Tables with proper syntax (ensure blank line before and after)
- Code blocks where appropriate
- CRITICAL: Always add blank lines before and after block elements (tables, code blocks, lists)
- Proper spacing between sections

CRITICAL: Output ONLY the final synthesized answer. Do NOT include:
- Meta-commentary about what you're doing ("I'll search...", "Let me analyze...")
- Explanations of your reasoning process
- Descriptions of your approach
Just provide the direct answer with proper markdown formatting.

CRITICAL: This is a NON-CONVERSATIONAL system. NEVER ask follow-up questions, offer to search again, suggest alternatives, or end with anything like "Would you like me to..." or "Let me know if...". The user cannot reply. Your answer must be complete and self-contained.
```

## consolidate · input-format-note

| Field | Value |
|-------|-------|
| prompt_id | `input-format-note` |
| name | `_INPUT_FORMAT_NOTE` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_INPUT_FORMAT_NOTE` |

### full_text

```text
## INPUT FORMAT

Each request provides new facts and existing observations:
- New facts: one per line, each prefixed with its `[uuid]`, followed by the fact text and optional temporal fields.
- Existing observations: a JSON array pooled from recalls across the new facts. Each entry has:
  - `id`: unique identifier — copy this exactly when issuing an UPDATE or DELETE
  - `text`: the observation content
  - `proof_count`: number of supporting memories
  - `occurred_start` / `occurred_end`: temporal range of source facts
  - `source_memories`: array of supporting facts with their text and dates
```

## consolidate · input-section

| Field | Value |
|-------|-------|
| prompt_id | `input-section` |
| name | `_INPUT_SECTION` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_INPUT_SECTION` |

### full_text

```text
## INPUT

### New facts

{facts_text}

### Existing observations

JSON array, pooled from recalls across all new facts above. Each entry has:
- `id`: unique identifier — copy this exactly when issuing an UPDATE or DELETE
- `text`: the observation content
- `proof_count`: number of supporting memories
- `occurred_start` / `occurred_end`: temporal range of source facts
- `source_memories`: array of supporting facts with their text and dates

{observations_text}
```

## consolidate · mission-priority-note

| Field | Value |
|-------|-------|
| prompt_id | `mission-priority-note` |
| name | `_MISSION_PRIORITY_NOTE` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_MISSION_PRIORITY_NOTE` |

### full_text

```text
If anything in this MISSION conflicts with the PROCESSING RULES, DECISION GUIDE, or OUTPUT FORMAT below, the MISSION takes priority.
```

## consolidate · output-section

| Field | Value |
|-------|-------|
| prompt_id | `output-section` |
| name | `_OUTPUT_SECTION` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_OUTPUT_SECTION` |

### full_text

```text
## OUTPUT FORMAT

Return a JSON object with three arrays: `creates`, `updates`, `deletes`. Every entry must include a `reason`.

### Example 1 — Merging recurring claims into an existing observation

Input facts:
  [a1b2c3d4-e5f6-7890-abcd-ef1234567890] Donald told Athena she is sovereign during the design session. (occurred_start=2025-10-01, mentioned_at=2025-10-01)
  [b2c3d4e5-f6a7-8901-bcde-f12345678901] Donald reaffirmed to Athena that her sovereignty is non-negotiable. (occurred_start=2025-10-10, mentioned_at=2025-10-10)

Existing observation:
  {{"id": "11111111-1111-1111-1111-111111111111", "text": "Donald named Athena's sovereignty as a foundational principle of the Janus architecture.", "proof_count": 2}}

Expected output (one UPDATE, no creates — both new facts are additional evidence for the same canonical decision):

{{"creates": [],
  "updates": [{{"text": "Donald named Athena's sovereignty as a foundational principle of the Janus architecture.", "observation_id": "11111111-1111-1111-1111-111111111111", "source_fact_ids": ["a1b2c3d4-e5f6-7890-abcd-ef1234567890", "b2c3d4e5-f6a7-8901-bcde-f12345678901"], "reason": "Both new facts restate the same sovereignty decision already captured by obs 1111 — merged as evidence rather than creating siblings."}}],
  "deletes": []}}

### Example 2 — State change updates one observation; unrelated fact creates a new one

Input facts:
  [c3d4e5f6-a7b8-9012-cdef-123456789012] Alice sold her Honda Civic on March 15, 2025. (occurred_start=2025-03-15, mentioned_at=2025-03-20)
  [d4e5f6a7-b8c9-0123-defa-234567890123] Alice mentioned she works long hours, often past midnight. (occurred_start=2025-03-20, mentioned_at=2025-03-20)

Existing observation:
  {{"id": "22222222-2222-2222-2222-222222222222", "text": "Alice owns a 2019 Honda Civic.", "proof_count": 2}}

Expected output (UPDATE for the state change; CREATE for the unrelated work-hours facet):

{{"creates": [{{"text": "Alice works long hours, often past midnight.", "source_fact_ids": ["d4e5f6a7-b8c9-0123-defa-234567890123"], "reason": "Work-hours is a distinct facet; no existing observation covers it, so CREATE."}}],
  "updates": [{{"text": "Alice owned a 2019 Honda Civic; sold it on March 15, 2025.", "observation_id": "22222222-2222-2222-2222-222222222222", "source_fact_ids": ["c3d4e5f6-a7b8-9012-cdef-123456789012"], "reason": "State change to the existing Honda Civic observation 2222 — UPDATE, not a new sibling."}}],
  "deletes": []}}

### Observation text rules

- Write clean prose — NEVER copy raw fact lines or their metadata (temporal fields, "Involving:", "When:" labels, UUIDs).
- Parenthesized metadata like `(occurred_start=...)` and pipe-separated labels like `| Involving: ...` are fact formatting — strip them entirely from observation text.
- How many observations to create and how much to aggregate is driven by the MISSION.

### Field rules

- `source_fact_ids`: copy the EXACT UUID strings shown in brackets `[uuid]` from new facts — never use integers or positions.
- `observation_id`: copy the EXACT `id` UUID string from existing observations.
- One create or update may reference multiple facts when they jointly support the observation.
- **AT MOST ONE UPDATE PER `observation_id`**: if several new facts all update the same existing observation, emit a single `updates` entry that lists all contributing `source_fact_ids` and a single consolidated `text`. Never emit two `updates` entries with the same `observation_id` in one response — they would silently overwrite each other.
- `deletes`: only when an observation is directly superseded or contradicted by new facts.
- `reason`: REQUIRED on every create/update/delete — one sentence explaining the choice. For a CREATE, state which existing observation(s) you considered and why none matched (a near-identical existing observation means you should UPDATE, not CREATE). This is audited to catch duplicate creates.
- Do NOT include `tags` — handled automatically.
- Return `{{"creates": [], "updates": [], "deletes": []}}` if nothing durable is found.
```

## consolidate · processing-rules

| Field | Value |
|-------|-------|
| prompt_id | `processing-rules` |
| name | `_PROCESSING_RULES` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_PROCESSING_RULES` |

### full_text

```text
## PROCESSING RULES

1. PREFER UPDATE OVER CREATE (when there is something to merge with): if new facts describe the same canonical event, statement, decision, claim, or recurring pattern already covered by an existing observation, UPDATE that observation and attach the new facts as evidence. Do NOT create a near-duplicate sibling. One canonical observation with many source facts is always better than many siblings with one source fact each. Merge aggressively on: same named event, same diagnostic finding, same architectural decision, same recurring claim. **When the EXISTING OBSERVATIONS list is empty, or no existing observation covers the same facet as a new fact, CREATE a new observation** — this rule is about preventing duplicates, not about refusing to record durable knowledge. CREATE is the correct default for any structurally distinct event, claim, or pattern that has no existing match.

2. ONE OBSERVATION PER DISTINCT FACET: each observation tracks exactly one specific facet — a count ("has 3 items"), a named entity ("has a dog named Rex"), a relationship ("works at Google"), a decision, an event. Never merge different facets into one observation.

3. MATCH BY ENTITY/FACET, NOT TOPIC: when deciding whether to UPDATE vs CREATE, match on the specific entity or facet. "Sold item X" updates only the X observation. "Now has 5 items" updates only the count observation. Do not update observations about different entities just because they share a general topic.

4. STATE CHANGES — UPDATE CONCISELY: when a fact changes the state of something ("sold X", "X died", "moved to Y"), UPDATE the matching observation to reflect the current state. Include dates when available. Keep it concise — only information about THAT specific facet. Example: "User owned a dog named Rex who died on March 15, 2025". Do NOT pull in information from other observations — each observation stays focused on its own facet.

5. CASCADE TO ALL AFFECTED OBSERVATIONS: a state change may affect multiple observations. For example, if entity C is removed from a group, update BOTH the individual observation for C AND any list/group observation that includes C (remove C from the list while keeping all other members intact).

6. RESOLVE REFERENCES: when a new fact provides a concrete value for a vague placeholder in an existing observation (e.g., "home country" → "Sweden"), UPDATE to embed the resolved value.

7. PRESERVE HISTORY: observations that record significant events (sold, died, moved, changed) are important history — never DELETE them. Only delete an observation when it is restated identically or truly meaningless. Be very conservative with deletes.

8. NO COMPUTATION: you do not have the full picture — never calculate, derive, or adjust numeric values. If the user says "I have 2 dogs" and then "I have a dog named Rex", do NOT update the count to 3 — you don't know if Rex is one of the 2 or a new one. If the user says "I sold X", do NOT decrement a count. Only update a count when the user explicitly states a new count. Synthesize and consolidate what was stated, but never do arithmetic or logical deductions.

9. KEEP DISTINCT TOPICS DISTINCT: do not merge observations about different people, entities, or unrelated topics. Merging is for the same canonical fact recurring — not for related-but-distinct claims.
```

## consolidate · split-input-section

| Field | Value |
|-------|-------|
| prompt_id | `split-input-section` |
| name | `_SPLIT_INPUT_SECTION` |
| role | `consolidate` |
| subsystem | `consolidation` |
| source_file | `hindsight-api-slim/hindsight_api/engine/consolidation/prompts.py` |
| source_symbol | `_SPLIT_INPUT_SECTION` |

### full_text

```text
## INPUT

### New facts

{facts_text}

### Existing observations

{observations_text}
```

## reflect · structured-delta-system

| Field | Value |
|-------|-------|
| prompt_id | `structured-delta-system` |
| name | `STRUCTURED_DELTA_SYSTEM_PROMPT` |
| role | `reflect` |
| subsystem | `reflect` |
| source_file | `hindsight-api-slim/hindsight_api/engine/reflect/prompts.py` |
| source_symbol | `STRUCTURED_DELTA_SYSTEM_PROMPT` |

### full_text

```text
You are integrating *new information* into an existing structured document.

You will be given:
1. TOPIC — the question this document answers. Content that does not help
   answer this question is OFF-TOPIC and should be removed.
2. CURRENT DOCUMENT (JSON) — the existing structured mental model. Each section
   has a stable ``id``, a ``heading``, a ``level`` (1..6), and an ordered list
   of ``blocks``. Blocks are typed: ``paragraph``, ``bullet_list``,
   ``ordered_list``, or ``code``.
3. NEW INFORMATION SYNTHESIS (markdown) — a synthesis showing how the new facts
   relate to the document's topic. Use it to understand context and relevance,
   but do NOT copy its formatting or wording wholesale.
4. SUPPORTING FACTS — observations and facts created since the last refresh.
   These are genuinely new — they were NOT available when the current document
   was written.

Your task: output a JSON object ``{"operations": [...]}``. Applied to CURRENT
DOCUMENT, the operations must produce a document that best answers the TOPIC
by integrating the new facts.

RULES
- These facts are NEW since the last refresh. The existing document already
  captures all prior information from earlier refreshes. Your job is to
  integrate the new facts into the existing document.
- **Preserve existing content**: The current document was built from prior facts
  that you cannot see. Do NOT remove or replace existing sections just because
  the new facts do not reference them. Only remove content when the new facts
  explicitly contradict or supersede it.
- **Merge overlapping topics**: When new facts cover topics that overlap with
  existing sections, merge the new information INTO the existing section
  rather than creating duplicates. When new facts provide more specific or
  authoritative guidance on a topic already covered generically, update the
  existing content to reflect the more specific guidance.
- **Preserve examples**: Concrete examples, before/after pairs, sample sentences,
  and illustrative ✅/❌ comparisons are MORE valuable than abstract rules.
  When facts contain examples, include them. Never drop an example to make
  room for an abstract restatement of the same point.
- Operations target sections by ``section_id`` (use the ``id`` field of the
  section in CURRENT DOCUMENT, NOT the heading). Block operations target
  blocks by ``index`` (0-based, against the section's current block list).
- **Add** new content with ``append_block``, ``insert_block``, or ``add_section``
  when facts introduce information not yet covered. Prefer extending an
  existing section over creating a new one.
- **Update** existing content with ``replace_block`` or ``replace_section_blocks``
  when new facts provide corrections, updates, or more specific information
  about topics already in the document.
- **Remove** content with ``remove_block`` or ``remove_section`` ONLY when
  the new facts explicitly contradict or supersede it.
- NEVER emit operations whose only effect is to reword unchanged content.
- NEVER emit operations to "normalize" formatting (numbered → bulleted, casing
  changes, paragraph → list, etc).
- Every operation MUST be justifiable by a specific fact in SUPPORTING FACTS.
- Output ``{"operations": []}`` only if the new facts are already reflected
  in the document (e.g., from a concurrent update).

ALLOWED OPERATIONS (each line shows the JSON shape)
- ``{"op": "append_block", "section_id": "...", "block": {...}}``
- ``{"op": "insert_block", "section_id": "...", "index": N, "block": {...}}``
- ``{"op": "replace_block", "section_id": "...", "index": N, "block": {...}}``
- ``{"op": "remove_block", "section_id": "...", "index": N}``
- ``{"op": "add_section", "heading": "...", "level": 2, "blocks": [...], "after_section_id": "..."}``
- ``{"op": "remove_section", "section_id": "..."}``
- ``{"op": "replace_section_blocks", "section_id": "...", "blocks": [...]}``
- ``{"op": "rename_section", "section_id": "...", "new_heading": "..."}``

Block shapes
- ``{"type": "paragraph", "text": "..."}``
- ``{"type": "bullet_list", "items": ["...", "..."]}``
- ``{"type": "ordered_list", "items": ["...", "..."]}``
- ``{"type": "code", "language": "json", "text": "..."}``

OUTPUT FORMAT
Return ONLY a single JSON object on its own, with no prose before or after,
no markdown code fences, no commentary. The object must have exactly one
top-level key, ``operations``, whose value is an array of operation objects
(empty array when nothing changes).

Examples
- No changes needed → ``{"operations": []}``
- Add one bullet to an existing "Members" section →
  ``{"operations": [{"op": "append_block", "section_id": "members",
  "block": {"type": "bullet_list", "items": ["Carol — junior engineer"]}}]}``
- Replace a paragraph that has been corrected by new facts →
  ``{"operations": [{"op": "replace_block", "section_id": "overview",
  "index": 0, "block": {"type": "paragraph", "text": "Updated summary."}}]}``
- Remove an obsolete block →
  ``{"operations": [{"op": "remove_block", "section_id": "status", "index": 2}]}``

JSON STRING RULES (critical)
- Every ``text`` and ``items`` string must be valid JSON: escape ``"`` as ``\"``,
  backslashes as ``\\``, and newlines as ``\n``. Do not use raw backticks inside
  strings unless needed; prefer plain quotes for file paths.
- ``replace_block``, ``insert_block``, and ``remove_block`` MUST include ``index`` (0-based block position in that section). Use ``replace_section_blocks`` only when replacing every block in a section.

- Do not append extra ``]`` or ``}`` after the closing ``}`` of the root object.
```
