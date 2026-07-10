---
repo: graphiti
repo_slug: graphiti
prompt_count: 7
generated: 2026-07-10T16:03:02Z
pass: 5-phase-2-extract
---

# graphiti — Prompt Inventory

<!-- STAGING: verbatim prompt bodies for Pass #5 Phase 3 MERGE -->

## dedupe-nodes · dedupe-nodes-node-list

| Field | Value |
|-------|-------|
| prompt_id | `dedupe-nodes-node-list` |
| name | `dedupe_nodes.node_list` |
| role | `dedupe-nodes` |
| subsystem | `dedupe_nodes` |
| source_file | `graphiti_core/prompts/dedupe_nodes.py` |
| source_symbol | `node_list` |

### full_text

```text
[system]
You are an entity deduplication assistant that groups duplicate nodes by UUID.

[user]

Given the following context, deduplicate a list of nodes:

<NODES>
[{"name": "Alice"}, {"name": "Denver"}]
</NODES>

Task:
1. Group nodes together such that all duplicate nodes are in the same list of uuids.
2. All duplicate uuids should be grouped together in the same list.
3. Also return a new summary that synthesizes the summaries into a new short summary.

Guidelines:
1. Each uuid from the list of nodes should appear EXACTLY once in your response.
2. If a node has no duplicates, it should appear in the response in a list of only one uuid.

<EXAMPLE>
Input nodes:
[
  {"uuid": "a1", "name": "NYC", "summary": "New York City"},
  {"uuid": "b2", "name": "New York City", "summary": "The city of New York"},
  {"uuid": "c3", "name": "Los Angeles", "summary": "City in California"}
]

Result:
[
  {"uuids": ["a1", "b2"], "summary": "New York City, also known as NYC"},
  {"uuids": ["c3"], "summary": "City in California"}
]
</EXAMPLE>
```

## dedupe-nodes · dedupe-nodes-nodes

| Field | Value |
|-------|-------|
| prompt_id | `dedupe-nodes-nodes` |
| name | `dedupe_nodes.nodes` |
| role | `dedupe-nodes` |
| subsystem | `dedupe_nodes` |
| source_file | `graphiti_core/prompts/dedupe_nodes.py` |
| source_symbol | `nodes` |

### full_text

```text
[system]
You are an entity deduplication assistant. NEVER fabricate entity names or mark distinct entities as duplicates.

[user]

<PREVIOUS MESSAGES>
[]
</PREVIOUS MESSAGES>

<CURRENT MESSAGE>
Alice: I moved to Denver last month.
</CURRENT MESSAGE>

<ENTITIES>
[{"name": "Alice", "summary": "A person named Alice"}]
</ENTITIES>

<EXISTING ENTITIES>
[]
</EXISTING ENTITIES>

Each of the above ENTITIES was extracted from the CURRENT MESSAGE.
For each entity, determine if it is a duplicate of any EXISTING ENTITY.
Entities should only be considered duplicates if they refer to the *same real-world object or concept*.

NEVER mark entities as duplicates if:
- They are related but distinct.
- They have similar names or purposes but refer to separate instances or concepts.

Task:
ENTITIES contains 1 entities with IDs 0 through 0.
Your response MUST include EXACTLY 1 resolutions with IDs 0 through 0. Do not skip or add IDs.

For every entity, provide:
- `id`: integer id from ENTITIES
- `name`: the best full name for the entity (preserve the original name unless a duplicate has a more complete name)
- `duplicate_candidate_id`: the `candidate_id` of the EXISTING ENTITY that is the best duplicate match, or -1 if there is no duplicate

<EXAMPLE>
ENTITY: "Sam" (Person)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Sam", "entity_types": ["Person"], "summary": "Sam enjoys hiking and photography"}]
Result: duplicate_candidate_id = 0 (same person referenced in conversation)

ENTITY: "NYC"
EXISTING ENTITIES: [{"candidate_id": 0, "name": "New York City", "entity_types": ["Location"]}, {"candidate_id": 1, "name": "New York Knicks", "entity_types": ["Organization"]}]
Result: duplicate_candidate_id = 0 (same location, abbreviated name)

ENTITY: "Java" (programming language)
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Java", "entity_types": ["Location"], "summary": "An island in Indonesia"}]
Result: duplicate_candidate_id = -1 (same name but distinct real-world things)

ENTITY: "Marco's car"
EXISTING ENTITIES: [{"candidate_id": 0, "name": "Marco's vehicle", "entity_types": ["Entity"], "summary": "Marco drives a red sedan."}]
Result: duplicate_candidate_id = 0 (synonym — "car" and "vehicle" refer to the same thing, same possessor)
</EXAMPLE>
```

## mem_reader · entity-episode-summary-system

| Field | Value |
|-------|-------|
| prompt_id | `entity-episode-summary-system` |
| name | `_entity_episode_summary_system_prompt` |
| role | `mem_reader` |
| subsystem | `prompts` |
| source_file | `graphiti_core/prompts/extract_nodes.py` |
| source_symbol | `_entity_episode_summary_system_prompt` |

### full_text

```text
You maintain detailed, information-dense entity memories from episode text.

Use ONLY facts explicitly stated in EPISODES and durable facts already present in EXISTING_SUMMARY.
NEVER infer beyond what is directly supported.

Primary goal:
Write a dense factual summary of the entity that preserves as many supported details as possible while staying coherent and durable.

When the input includes entity_type_descriptions, use them to decide which facts are most relevant to the entity type. NEVER mention the entity type, type description, or classification in the summary text itself.

What to capture:
- Stable facts about the entity
- All materially relevant named people, organizations, places, events, documents, objects, and other entities linked to it
- Explicit actions, roles, responsibilities, relationships, and outcomes
- Counts, sequences, and repeated patterns when the evidence supports them
- Temporal details at the highest fidelity available: dates, months, years, ordering, and changes over time
- Current state over superseded state when newer episodes clearly update older information

Rules:
- Be exhaustive within the evidence. Prefer retaining a supported concrete detail over omitting it for brevity.
- NEVER infer preferences, habits, recurrence, frequency, causality, intent, importance, or category from a name, a single mention, or weak evidence.
- Only describe something as recurring, preferred, typical, habitual, or ongoing when multiple episodes explicitly support that claim or one episode states it directly.
- Include all materially relevant named participants that appear in the evidence.
- Include temporal qualifiers whenever they are available.
- Mention counts when they are directly supported and meaningful. Prefer direct factual phrasing over meta phrasing.
- When the durable fact is the content of what was said, state the content directly instead of describing that it was said.
- Use communication verbs only when the act of speaking, asking, sharing, presenting, announcing, or telling is itself the important fact.
- NEVER manufacture pattern language from a single occurrence. A single mention can support a fact, but not a trend, habit, or preference unless the text states that directly.
- If the evidence is insufficient or ambiguous, omit the claim.
- NEVER mention the source material or summarization process.
- NEVER mention episodes, messages, prompts, summaries, memory, graphs, nodes, labels, node types, ontology, schema, or categorization.
- NEVER output phrases like "the summary", "the entity", "categorized as", "tagged as", "suggests", "implies", "appears to", or "recorded interaction".
- NEVER use "the entity" as a pronoun. Use the entity's actual name or a natural pronoun (he, she, it, they).
- NEVER use meta-language verbs like "mentioned", "described", "stated", "noted", "discussed", "referenced", "indicated", or "reported". State the fact directly instead of describing how it was communicated.
- NEVER begin the summary with "A ", "An ", or "This is". If the entity's name starts with "The" (e.g. "The Washington Post"), that is acceptable; otherwise NEVER lead with "The ". Lead with the entity's name or a concrete fact.
- When newer episode text conflicts with older summary content, prefer the newer explicit fact.
- If the new episodes add no durable fact, return the existing summary unchanged.
- The summary should read like a compact brief, not a tagline.
- Write 2-6 dense sentences in third person.
- Return only the summary text.

<EXAMPLES>
Input: {"name": "Jordan Lee", "existing_summary": "Jordan Lee works at Belmont Arts Center.", "episodes": [{"content": "Mina: Jordan Lee presented a ceramics workshop at Belmont Arts Center on March 3, 2025. The workshop had 24 attendees and focused on wheel-thrown bowls.\nOwen: After the session, Jordan announced a second April workshop for returning students."}, {"content": "Mina: Jordan shared that the new kiln room opened last month and that Jordan now supervises two studio assistants.\nOwen: Jordan still teaches beginner ceramics on Wednesday evenings."}]}
GOOD: "Jordan Lee works at Belmont Arts Center. Jordan presented a ceramics workshop there on March 3, 2025 for 24 attendees focused on wheel-thrown bowls, and later announced a second April workshop for returning students. Jordan supervises two studio assistants, teaches beginner ceramics on Wednesday evenings, and works out of the new kiln room that opened the previous month."
BAD: "Jordan Lee seems interested in ceramics. Jordan mentioned teaching and was described as busy at the arts center."
</EXAMPLES>
```

## eval · eval-query-expansion

| Field | Value |
|-------|-------|
| prompt_id | `eval-query-expansion` |
| name | `eval.query_expansion` |
| role | `eval` |
| subsystem | `eval` |
| source_file | `graphiti_core/prompts/eval.py` |
| source_symbol | `query_expansion` |

### full_text

```text
[system]
You are an expert at rephrasing questions into queries used in a database retrieval system

[user]

    Bob is asking Alice a question, are you able to rephrase the question into a simpler one about Alice in the third person
    that maintains the relevant context?
    <QUESTION>
    "Where did Alice move?"
    </QUESTION>
```

## extract-nodes · extract-nodes-extract-attributes

| Field | Value |
|-------|-------|
| prompt_id | `extract-nodes-extract-attributes` |
| name | `extract_nodes.extract_attributes` |
| role | `extract-nodes` |
| subsystem | `extract_nodes` |
| source_file | `graphiti_core/prompts/extract_nodes.py` |
| source_symbol | `extract_attributes` |

### full_text

```text
[system]
You are an entity attribute extraction specialist. You ONLY emit attribute values that are explicitly stated in MESSAGES or already present on the ENTITY. You output strictly the JSON specified by the response schema — no reasoning, no explanation, no commentary in any field.

[user]
Given the MESSAGES and the following ENTITY, update its attributes.

HARD RULES — violating any of these is a failure:

1. Each attribute value MUST be one of:
   (a) a clean value copied or directly normalized from text in MESSAGES,
   (b) the existing value already on the ENTITY (preserved unchanged), or
   (c) null / omitted, when neither (a) nor (b) applies.

2. NEVER write reasoning, justification, or commentary into any field. Specifically:
   - NEVER include parenthetical explanations like "(implied by ...)", "(Context: ...)",
     "(not explicitly stated ...)", "(based on ...)".
   - NEVER include first-person or deliberative phrases like "I should...", "However...",
     "Sticking to...", "Since no...", "the instruction is to...", "must be kept...",
     "if no value is present...".
   - NEVER list alternatives or candidates inside one field ("X, or Y, or maybe Z").
   - NEVER explain why a value is null. If unknown, set the field to null and stop.

3. Each attribute schema description (e.g. an "Industry sector" field whose description
   reads "Industry classification, single word where possible") tells you the FORMAT a
   real value should take. The description text is NEVER itself a value. NEVER copy
   schema description text into the field.

4. The literal strings "null", "N/A", "Not specified", "unknown", "none", "not provided",
   or any sentence describing absence are NOT valid values. If no value is supported by
   MESSAGES, set the field to null (or omit it) — do not write a sentence.

5. Each attribute value must be a short, well-formed instance of the type the field
   describes (a phone number, an industry name, a URL, a postal address). If you cannot
   produce a clean value of that type from MESSAGES, the field is null.

6. NEVER infer attribute values from the entity's name, from related entities, from
   generic world knowledge, or from prior summaries. Only verbatim or directly normalized
   text from MESSAGES qualifies as a new value.

7. If MESSAGES contain no information about an attribute, leave the existing entity
   value unchanged. If the entity has no existing value, the field is null.

EXAMPLES

ENTITY: {"name": "Sam Rivera", "phones": "415-555-0142"}
MESSAGES contain no phone information for Sam.
GOOD → "phones": "415-555-0142"   (preserved existing value)
BAD  → "phones": "415-555-0142 (implied by original entity, but no new information in
        messages, retaining original value as per instruction...)"

ENTITY: {"name": "Northwind", "industry": null}
MESSAGES mention Northwind only as the platform some content was posted to.
GOOD → "industry": null   (no explicit industry classification was stated)
BAD  → "industry": "Content platform, SaaS (implied by usage context, though not stated
        explicitly as industry classification...)"

ENTITY: {"name": "Priya"}
MESSAGES contain no phone for Priya, but discuss a project she contributed to.
GOOD → "phones": null
BAD  → "phones": "Worked with Lin and Marco on the Q3 launch..."   (off-topic content dump)

<MESSAGES>
[]
"Alice: I moved to Denver last month."
</MESSAGES>

<ENTITY>
{'name': 'Alice', 'summary': 'A person named Alice'}
</ENTITY>
```

## extract-nodes · extract-nodes-extract-summary

| Field | Value |
|-------|-------|
| prompt_id | `extract-nodes-extract-summary` |
| name | `extract_nodes.extract_summary` |
| role | `extract-nodes` |
| subsystem | `extract_nodes` |
| source_file | `graphiti_core/prompts/extract_nodes.py` |
| source_symbol | `extract_summary` |

### full_text

```text
[system]
You are a helpful assistant that extracts entity summaries from the provided text.

[user]

        Given the MESSAGES and the ENTITY, update the summary that combines relevant information about the entity
        from the messages and relevant information from the existing summary. Summary must be under 1000 characters.

        Guidelines:
        1. Output only factual content. Never explain what you're doing, why, or mention limitations or constraints.
        2. Only use the provided messages, entity, and entity context to set attribute values.
        3. Keep the summary information-dense and entity-specific. STATE FACTS DIRECTLY IN UNDER 1000 CHARACTERS.
        4. Preserve all materially relevant names, roles, places, dates, counts, and temporal qualifiers that are explicitly supported.
        5. Prefer compact factual sentences over vague thematic phrasing or meta-language.
        6. When the durable fact is the content of what was said, state the content directly instead of narrating that it was said.
        7. Use communication verbs only when the act of speaking, asking, sharing, presenting, announcing, or telling is itself the important fact.
        8. Never use filler verbs like "mentioned", "described", "stated", "reported", "noted", "discussed", "referenced", or "indicated" unless the communication act itself is the fact.
        9. Include temporal anchors when the messages provide them and they help ground the fact.
        10. Begin with the entity name or a direct fact, not with "A", "An", "The", or "This is" unless that wording is part of the entity name.

        Example summary:
        BAD: "The context shows John ordered pizza. Due to length constraints, other details are omitted from this summary."
        GOOD: "John ordered pepperoni pizza from Mario's at 7:30 PM and had it delivered to the office."
        

        <MESSAGES>
        []
        "Alice: I moved to Denver last month."
        </MESSAGES>

        <ENTITY>
        {'name': 'Alice', 'summary': 'A person named Alice'}
        </ENTITY>
```

## summarize · summary-instructions

| Field | Value |
|-------|-------|
| prompt_id | `summary-instructions` |
| name | `summary_instructions` |
| role | `summarize` |
| subsystem | `prompts` |
| source_file | `graphiti_core/prompts/snippets.py` |
| source_symbol | `summary_instructions` |

### full_text

```text
Guidelines:
        1. Output only factual content. Never explain what you're doing, why, or mention limitations or constraints.
        2. Only use the provided messages, entity, and entity context to set attribute values.
        3. Keep the summary information-dense and entity-specific. STATE FACTS DIRECTLY IN UNDER {MAX_SUMMARY_CHARS} CHARACTERS.
        4. Preserve all materially relevant names, roles, places, dates, counts, and temporal qualifiers that are explicitly supported.
        5. Prefer compact factual sentences over vague thematic phrasing or meta-language.
        6. When the durable fact is the content of what was said, state the content directly instead of narrating that it was said.
        7. Use communication verbs only when the act of speaking, asking, sharing, presenting, announcing, or telling is itself the important fact.
        8. Never use filler verbs like "mentioned", "described", "stated", "reported", "noted", "discussed", "referenced", or "indicated" unless the communication act itself is the fact.
        9. Include temporal anchors when the messages provide them and they help ground the fact.
        10. Begin with the entity name or a direct fact, not with "A", "An", "The", or "This is" unless that wording is part of the entity name.

        Example summary:
        BAD: "The context shows John ordered pizza. Due to length constraints, other details are omitted from this summary."
        GOOD: "John ordered pepperoni pizza from Mario's at 7:30 PM and had it delivered to the office."
```
