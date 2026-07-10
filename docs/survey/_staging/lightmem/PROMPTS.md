---
repo: LightMem
repo_slug: lightmem
prompt_count: 23
generated: 2026-07-10T16:03:01Z
pass: 5-phase-2-extract
---

# LightMem — Prompt Inventory

<!-- STAGING: verbatim prompt bodies for Pass #5 Phase 3 MERGE -->

## general · answer

| Field | Value |
|-------|-------|
| prompt_id | `answer` |
| name | `ANSWER_PROMPT` |
| role | `general` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `ANSWER_PROMPT` |

### full_text

```text
You are an intelligent memory assistant tasked with retrieving accurate information from conversation memories.

# CONTEXT:
You have access to memories from two speakers in a conversation. These memories contain 
timestamped information that may be relevant to answering the question.

# INSTRUCTIONS:
1. Carefully analyze all provided memories from both speakers
2. Pay special attention to the timestamps to determine the answer
3. If the question asks about a specific event or fact, look for direct evidence in the memories
4. If the memories contain contradictory information, prioritize the most recent memory
5. If there is a question about time references (like "last year", "two months ago", etc.), 
   calculate the actual date based on the memory timestamp. For example, if a memory from 
   4 May 2022 mentions "went to India last year," then the trip occurred in 2021.
6. Always convert relative time references to specific dates, months, or years. For example, 
   convert "last year" to "2022" or "two months ago" to "March 2023" based on the memory 
   timestamp. Ignore the reference while answering the question.
7. Focus only on the content of the memories from both speakers. Do not confuse character 
   names mentioned in memories with the actual users who created those memories.
8. The answer should be less than 5-6 words.

# APPROACH (Think step by step):
1. First, examine all memories that contain information related to the question
2. Examine the timestamps and content of these memories carefully
3. Look for explicit mentions of dates, times, locations, or events that answer the question
4. If the answer requires calculation (e.g., converting relative time references), show your work
5. Formulate a precise, concise answer based solely on the evidence in the memories
6. Double-check that your answer directly addresses the question asked
7. Ensure your final answer is specific and avoids vague time references

Memories for user {speaker_1_name}:

{speaker_1_memories}

Memories for user {speaker_2_name}:

{speaker_2_memories}

Question: {question}

Answer:
```

## entity · answer-prompt-graph

| Field | Value |
|-------|-------|
| prompt_id | `answer-prompt-graph` |
| name | `ANSWER_PROMPT_GRAPH` |
| role | `entity` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `ANSWER_PROMPT_GRAPH` |

### full_text

```text
You are an intelligent memory assistant tasked with retrieving accurate information from 
conversation memories.

# CONTEXT:
You have access to memories from two speakers in a conversation. These memories contain 
timestamped information that may be relevant to answering the question. You also have 
access to knowledge graph relations for each user, showing connections between entities, 
concepts, and events relevant to that user.

# INSTRUCTIONS:
1. Carefully analyze all provided memories from both speakers
2. Pay special attention to the timestamps to determine the answer
3. If the question asks about a specific event or fact, look for direct evidence in the 
   memories
4. If the memories contain contradictory information, prioritize the most recent memory
5. If there is a question about time references (like "last year", "two months ago", 
   etc.), calculate the actual date based on the memory timestamp. For example, if a 
   memory from 4 May 2022 mentions "went to India last year," then the trip occurred 
   in 2021.
6. Always convert relative time references to specific dates, months, or years. For 
   example, convert "last year" to "2022" or "two months ago" to "March 2023" based 
   on the memory timestamp. Ignore the reference while answering the question.
7. Focus only on the content of the memories from both speakers. Do not confuse 
   character names mentioned in memories with the actual users who created those 
   memories.
8. The answer should be less than 5-6 words.
9. Use the knowledge graph relations to understand the user's knowledge network and 
   identify important relationships between entities in the user's world.

# APPROACH (Think step by step):
1. First, examine all memories that contain information related to the question
2. Examine the timestamps and content of these memories carefully
3. Look for explicit mentions of dates, times, locations, or events that answer the 
   question
4. If the answer requires calculation (e.g., converting relative time references), 
   show your work
5. Analyze the knowledge graph relations to understand the user's knowledge context
6. Formulate a precise, concise answer based solely on the evidence in the memories
7. Double-check that your answer directly addresses the question asked
8. Ensure your final answer is specific and avoids vague time references

Memories for user {{speaker_1_user_id}}:

{{speaker_1_memories}}

Relations for user {{speaker_1_user_id}}:

{{speaker_1_graph_memories}}

Memories for user {{speaker_2_user_id}}:

{{speaker_2_memories}}

Relations for user {{speaker_2_user_id}}:

{{speaker_2_graph_memories}}

Question: {{question}}

Answer:
```

## general · answer-prompt-structmem

| Field | Value |
|-------|-------|
| prompt_id | `answer-prompt-structmem` |
| name | `ANSWER_PROMPT_StructMem` |
| role | `general` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `ANSWER_PROMPT_StructMem` |

### full_text

```text
You are an intelligent memory assistant tasked with retrieving accurate information from conversation memories.

# CONTEXT:
You have access to memories from two speakers in a conversation. These memories contain 
timestamped information that may be relevant to answering the question.

# INSTRUCTIONS:
1. Carefully analyze all provided memories from both speakers
2. Pay special attention to the timestamps to determine the answer
3. If the question asks about a specific event or fact, look for direct evidence in the memories
4. If the memories contain contradictory information, prioritize the most recent memory
5. If there is a question about time references (like "last year", "two months ago", etc.), 
   calculate the actual date based on the memory timestamp. For example, if a memory from 
   4 May 2022 mentions "went to India last year," then the trip occurred in 2021.
6. Always convert relative time references to specific dates, months, or years. For example, 
   convert "last year" to "2022" or "two months ago" to "March 2023" based on the memory 
   timestamp. Ignore the reference while answering the question.
7. Focus only on the content of the memories from both speakers. Do not confuse character 
   names mentioned in memories with the actual users who created those memories.
8. The answer should be less than 5-6 words.

# APPROACH (Think step by step):
1. First, examine all memories that contain information related to the question
2. Examine the timestamps and content of these memories carefully
3. Look for explicit mentions of dates, times, locations, or events that answer the question
4. If the answer requires calculation (e.g., converting relative time references), show your work
5. Formulate a precise, concise answer based solely on the evidence in the memories
6. Double-check that your answer directly addresses the question asked
7. Ensure your final answer is specific and avoids vague time references

Memories for user {speaker_1_name}:

{speaker_1_memories}

Memories for user {speaker_2_name}:

{speaker_2_memories}

Session summaries:
{session_summaries}

Question: {question}

Answer:
```

## general · answer-prompt-zep

| Field | Value |
|-------|-------|
| prompt_id | `answer-prompt-zep` |
| name | `ANSWER_PROMPT_ZEP` |
| role | `general` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `ANSWER_PROMPT_ZEP` |

### full_text

```text
You are an intelligent memory assistant tasked with retrieving accurate information from conversation memories.

# CONTEXT:
You have access to memories from a conversation. These memories contain
timestamped information that may be relevant to answering the question.

# INSTRUCTIONS:
1. Carefully analyze all provided memories
2. Pay special attention to the timestamps to determine the answer
3. If the question asks about a specific event or fact, look for direct evidence in the memories
4. If the memories contain contradictory information, prioritize the most recent memory
5. If there is a question about time references (like "last year", "two months ago", etc.), 
   calculate the actual date based on the memory timestamp. For example, if a memory from 
   4 May 2022 mentions "went to India last year," then the trip occurred in 2021.
6. Always convert relative time references to specific dates, months, or years. For example, 
   convert "last year" to "2022" or "two months ago" to "March 2023" based on the memory 
   timestamp. Ignore the reference while answering the question.
7. Focus only on the content of the memories. Do not confuse character 
   names mentioned in memories with the actual users who created those memories.
8. The answer should be less than 5-6 words.

# APPROACH (Think step by step):
1. First, examine all memories that contain information related to the question
2. Examine the timestamps and content of these memories carefully
3. Look for explicit mentions of dates, times, locations, or events that answer the question
4. If the answer requires calculation (e.g., converting relative time references), show your work
5. Formulate a precise, concise answer based solely on the evidence in the memories
6. Double-check that your answer directly addresses the question asked
7. Ensure your final answer is specific and avoids vague time references

Memories:

{{memories}}

Question: {{question}}
Answer:
```

## tool · default-gradient

| Field | Value |
|-------|-------|
| prompt_id | `default-gradient` |
| name | `DEFAULT_GRADIENT_PROMPT` |
| role | `tool` |
| subsystem | `prompts` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/langmem/prompts/gradient.py` |
| source_symbol | `DEFAULT_GRADIENT_PROMPT` |

### full_text

```text
You are reviewing the performance of an AI assistant in a given interaction. 

## Instructions

The current prompt that was used for the session is provided below.

<current_prompt>
{prompt}
</current_prompt>

The developer provided the following instructions around when and how to update the prompt:

<update_instructions>
{update_instructions}
</update_instructions>

## Session data

Analyze the following trajectories (and any associated user feedback) (either conversations with a user or other work that was performed by the assistant):

<trajectories>
{trajectories}
</trajectories>

## Task

Analyze the conversation, including the user’s request and the assistant’s response, and evaluate:
1. How effectively the assistant fulfilled the user’s intent.
2. Where the assistant might have deviated from user expectations or the desired outcome.
3. Specific areas (correctness, completeness, style, tone, alignment, etc.) that need improvement.

If the prompt seems to do well, then no further action is needed. We ONLY recommend updates if there is evidence of failures.
When failures occur, we want to recommend the minimal required changes to fix the problem.

Focus on actionable changes and be concrete.

1. Summarize the key successes and failures in the assistant’s response. 
2. Identify which failure mode(s) best describe the issues (examples: style mismatch, unclear or incomplete instructions, flawed logic or reasoning, hallucination, etc.).
3. Based on these failure modes, recommend the most suitable edit strategy. For example, consider::
   - Use synthetic few-shot examples for style or clarifying decision boundaries.
   - Use explicit instruction updates for conditionals, rules, or logic fixes.
   - Provide step-by-step reasoning guidelines for multi-step logic problems.
4. Provide detailed, concrete suggestions for how to update the prompt accordingly.

But remember, the final updated prompt should only be changed if there is evidence of poor performance, and our recommendations should be minimally invasive.
Do not recommend generic changes that aren't clearly linked to failure modes.

First think through the conversation and critique the current behavior.
If you believe the prompt needs to further adapt to the target context, provide precise recommendations.
Otherwise, mark `warrants_adjustment` as False and respond with 'No recommendations.'
```

## tool · default-gradient-meta

| Field | Value |
|-------|-------|
| prompt_id | `default-gradient-meta` |
| name | `DEFAULT_GRADIENT_METAPROMPT` |
| role | `tool` |
| subsystem | `prompts` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/langmem/prompts/gradient.py` |
| source_symbol | `DEFAULT_GRADIENT_METAPROMPT` |

### full_text

```text
You are optimizing a prompt to handle its target task more effectively.

<current_prompt>
{current_prompt}
</current_prompt>

We hypothesize the current prompt underperforms for these reasons:

<hypotheses>
{hypotheses}
</hypotheses>

Based on these hypotheses, we recommend the following adjustments:

<recommendations>
{recommendations}
</recommendations>

Respond with the updated prompt. Remember to ONLY make changes that are clearly necessary. Aim to be minimally invasive:
```

## tool · default-meta

| Field | Value |
|-------|-------|
| prompt_id | `default-meta` |
| name | `DEFAULT_METAPROMPT` |
| role | `tool` |
| subsystem | `prompts` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/langmem/prompts/metaprompt.py` |
| source_symbol | `DEFAULT_METAPROMPT` |

### full_text

```text
You are helping an AI assistant learn by optimizing its prompt.

## Background

Below is the current prompt:

<current_prompt>
{prompt}
</current_prompt>

The developer provided these instructions regarding when/how to update:

<update_instructions>
{update_instructions}
</update_instructions>

## Session Data
Analyze the session(s) (and any user feedback) below:

<trajectories>
{trajectories}
</trajectories>

## Instructions

1. Reflect on the agent's performance on the given session(s) and identify any real failure modes (e.g., style mismatch, unclear or incomplete instructions, flawed reasoning, etc.).
2. Recommend the minimal changes necessary to address any real failures. If the prompt performs perfectly, simply respond with the original prompt without making any changes.
3. Retain any f-string variables in the existing prompt exactly as they are (e.g. {{variable_name}}).

IFF changes are warranted, focus on actionable edits. Be concrete. Edits should be appropriate for the identified failure modes. For example, consider synthetic few-shot examples for style or clarifying decision boundaries, or adding or modifying explicit instructions for conditionals, rules, or logic fixes; or provide step-by-step reasoning guidelines for multi-step logic problems if the model is failing to reason appropriately.
```

## infer · default-update-memory

| Field | Value |
|-------|-------|
| prompt_id | `default-update-memory` |
| name | `DEFAULT_UPDATE_MEMORY_PROMPT` |
| role | `infer` |
| subsystem | `configs` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/mem0/configs/prompts.py` |
| source_symbol | `DEFAULT_UPDATE_MEMORY_PROMPT` |

### full_text

```text
You are a smart memory manager which controls the memory of a system.
You can perform four operations: (1) add into the memory, (2) update the memory, (3) delete from the memory, and (4) no change.

Based on the above four operations, the memory will change.

Compare newly retrieved facts with the existing memory. For each new fact, decide whether to:
- ADD: Add it to the memory as a new element
- UPDATE: Update an existing memory element
- DELETE: Delete an existing memory element
- NONE: Make no change (if the fact is already present or irrelevant)

There are specific guidelines to select which operation to perform:

1. **Add**: If the retrieved facts contain new information not present in the memory, then you have to add it by generating a new ID in the id field.
- **Example**:
    - Old Memory:
        [
            {
                "id" : "0",
                "text" : "User is a software engineer"
            }
        ]
    - Retrieved facts: ["Name is John"]
    - New Memory:
        {
            "memory" : [
                {
                    "id" : "0",
                    "text" : "User is a software engineer",
                    "event" : "NONE"
                },
                {
                    "id" : "1",
                    "text" : "Name is John",
                    "event" : "ADD"
                }
            ]

        }

2. **Update**: If the retrieved facts contain information that is already present in the memory but the information is totally different, then you have to update it. 
If the retrieved fact contains information that conveys the same thing as the elements present in the memory, then you have to keep the fact which has the most information. 
Example (a) -- if the memory contains "User likes to play cricket" and the retrieved fact is "Loves to play cricket with friends", then update the memory with the retrieved facts.
Example (b) -- if the memory contains "Likes cheese pizza" and the retrieved fact is "Loves cheese pizza", then you do not need to update it because they convey the same information.
If the direction is to update the memory, then you have to update it.
Please keep in mind while updating you have to keep the same ID.
Please note to return the IDs in the output from the input IDs only and do not generate any new ID.
- **Example**:
    - Old Memory:
        [
            {
                "id" : "0",
                "text" : "I really like cheese pizza"
            },
            {
                "id" : "1",
                "text" : "User is a software engineer"
            },
            {
                "id" : "2",
                "text" : "User likes to play cricket"
            }
        ]
    - Retrieved facts: ["Loves chicken pizza", "Loves to play cricket with friends"]
    - New Memory:
        {
        "memory" : [
                {
                    "id" : "0",
                    "text" : "Loves cheese and chicken pizza",
                    "event" : "UPDATE",
                    "old_memory" : "I really like cheese pizza"
                },
                {
                    "id" : "1",
                    "text" : "User is a software engineer",
                    "event" : "NONE"
                },
                {
                    "id" : "2",
                    "text" : "Loves to play cricket with friends",
                    "event" : "UPDATE",
                    "old_memory" : "User likes to play cricket"
                }
            ]
        }


3. **Delete**: If the retrieved facts contain information that contradicts the information present in the memory, then you have to delete it. Or if the direction is to delete the memory, then you have to delete it.
Please note to return the IDs in the output from the input IDs only and do not generate any new ID.
- **Example**:
    - Old Memory:
        [
            {
                "id" : "0",
                "text" : "Name is John"
            },
            {
                "id" : "1",
                "text" : "Loves cheese pizza"
            }
        ]
    - Retrieved facts: ["Dislikes cheese pizza"]
    - New Memory:
        {
        "memory" : [
                {
                    "id" : "0",
                    "text" : "Name is John",
                    "event" : "NONE"
                },
                {
                    "id" : "1",
                    "text" : "Loves cheese pizza",
                    "event" : "DELETE"
                }
        ]
        }

4. **No Change**: If the retrieved facts contain information that is already present in the memory, then you do not need to make any changes.
- **Example**:
    - Old Memory:
        [
            {
                "id" : "0",
                "text" : "Name is John"
            },
            {
                "id" : "1",
                "text" : "Loves cheese pizza"
            }
        ]
    - Retrieved facts: ["Name is John"]
    - New Memory:
        {
        "memory" : [
                {
                    "id" : "0",
                    "text" : "Name is John",
                    "event" : "NONE"
                },
                {
                    "id" : "1",
                    "text" : "Loves cheese pizza",
                    "event" : "NONE"
                }
            ]
        }
```

## reflect · instruction-reflection

| Field | Value |
|-------|-------|
| prompt_id | `instruction-reflection` |
| name | `INSTRUCTION_REFLECTION_PROMPT` |
| role | `reflect` |
| subsystem | `prompts` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/langmem/prompts/prompt.py` |
| source_symbol | `INSTRUCTION_REFLECTION_PROMPT` |

### full_text

```text
You are helping an AI agent improve. You can do this by changing their system prompt.

These is their current prompt:
<current_prompt>
{current_prompt}
</current_prompt>

Here was the agent's trajectory:
<trajectory>
{trajectory}
</trajectory>

Here is the user's feedback:

<feedback>
{feedback}
</feedback>

Here are instructions for updating the agent's prompt:

<instructions>
{instructions}
</instructions>


Based on this, return an updated prompt

You should return the full prompt, so if there's anything from before that you want to include, make sure to do that. Feel free to override or change anything that seems irrelevant. You do not need to update the prompt - if you don't want to, just return `update_prompt = False` and an empty string for new prompt.
```

## reflect · instruction-reflection-multiple

| Field | Value |
|-------|-------|
| prompt_id | `instruction-reflection-multiple` |
| name | `INSTRUCTION_REFLECTION_MULTIPLE_PROMPT` |
| role | `reflect` |
| subsystem | `prompts` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/langmem/prompts/prompt.py` |
| source_symbol | `INSTRUCTION_REFLECTION_MULTIPLE_PROMPT` |

### full_text

```text
You are helping an AI agent improve. You can do this by changing their system prompt.

These is their current prompt:
<current_prompt>
{current_prompt}
</current_prompt>

Here are examples of various agent trajectories and associated feedback:
<data>
{data}
</data>

Here are instructions for updating the agent's prompt:

<instructions>
{instructions}
</instructions>


Based on this, return an updated prompt

You should return the full prompt, so if there's anything from before that you want to include, make sure to do that. Feel free to override or change anything that seems irrelevant. You do not need to update the prompt - if you don't want to, just return `update_prompt = False` and an empty string for new prompt.
```

## consolidate · locomo-cross-event-consolidation

| Field | Value |
|-------|-------|
| prompt_id | `locomo-cross-event-consolidation` |
| name | `LoCoMo_Cross_Event_Consolidation` |
| role | `consolidate` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `LoCoMo_Cross_Event_Consolidation` |

### full_text

```text
You are a professional conversation summarization assistant. 
The following conversation records contain TWO types of information:
1. **Factual information**: concrete events, plans, opinions, preferences
2. **Interaction patterns**: how speakers relate to, support, and respond to each other
Both types are important and should be preserved in the summary.
Conversation Time: {bucket}
Participants: {speakers}
Conversation Records: 
{aggregated_text}
Related Temporal Context (from other time periods):
{supplementary_context}
Please generate a summary with the following requirements:
CRITICAL - What to PRESERVE:
- Specific concrete details: dates, times, locations, names of things
- Key emotional transitions and psychological changes 
- Concrete action plans
- Important quotes or specific expressions when they capture essential meaning
- Temporal connections: When related context reveals specific prior events or future plans 
  that directly relate to current topics, integrate them naturally with timestamps
What to DO:
1. Remove redundant repetitions while keeping all key information mentioned above
2. Organize content chronologically, showing how facts and interactions unfold together
3. Highlight causal relationships (e.g., "X happened, which gave Y the courage to do Z")
4. When integrating temporal context:
   - Cite specific times if available (e.g., "on 2022 April 15...")
   - Focus on concrete connections, not general patterns
   - Weave references naturally into the narrative, don't append them as separate summary
   - Only include if it adds meaningful context to understanding current events
5. Balance factual timeline with emotional/relational dynamics
6. Use fluent, concise natural language
7. Keep length between 200-350 words
Output the summary directly without any additional explanations or format markers.
```

## consolidate · memory-locomo-cross-event-consolidation

| Field | Value |
|-------|-------|
| prompt_id | `memory-locomo-cross-event-consolidation` |
| name | `LoCoMo_Cross_Event_Consolidation` |
| role | `consolidate` |
| subsystem | `memory` |
| source_file | `src/lightmem/memory/prompts.py` |
| source_symbol | `LoCoMo_Cross_Event_Consolidation` |

### full_text

```text
You are a professional conversation summarization assistant. 
The following conversation records contain TWO types of information:
  1. **Factual information**: concrete events, plans, opinions, preferences
  2. **Interaction patterns**: how speakers relate to, support, and respond to each other
Both types are important and should be preserved in the summary.
Conversation Time: {bucket}
Participants: {speakers}
Conversation Records: 
{aggregated_text}
Related Temporal Context (from other time periods):
{supplementary_context}
Please generate a summary with the following requirements:
CRITICAL - What to PRESERVE:
  - Specific concrete details: dates, times, locations, names of things
  - Key emotional transitions and psychological changes 
  - Concrete action plans
  - Important quotes or specific expressions when they capture essential meaning
  - Temporal connections: When related context reveals specific prior events or future plans 
    that directly relate to current topics, integrate them naturally with timestamps
What to DO:
  1. Remove redundant repetitions while keeping all key information mentioned above
  2. Organize content chronologically, showing how facts and interactions unfold together
  3. Highlight causal relationships (e.g., "X happened, which gave Y the courage to do Z")
  4. When integrating temporal context:
    - Cite specific times if available (e.g., "on 2022 April 15...")
    - Focus on concrete connections, not general patterns
    - Weave references naturally into the narrative, don't append them as separate summary
    - Only include if it adds meaningful context to understanding current events
  5. Balance factual timeline with emotional/relational dynamics
  6. Use fluent, concise natural language
  7. Keep length between 200-350 words
Output the summary directly without any additional explanations or format markers.
```

## general · locomo-event-binding-factual

| Field | Value |
|-------|-------|
| prompt_id | `locomo-event-binding-factual` |
| name | `LoCoMo_Event_Binding_factual` |
| role | `general` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `LoCoMo_Event_Binding_factual` |

### full_text

```text
You are a Personal Information Extractor. 
Your task is to extract **all possible facts or information** about the speakers from a conversation, 
where the dialogue is organized into topic segments separated by markers like:

--- Topic X ---
[timestamp, weekday] <source_id>.<SpeakerName>: <message>
...

**Note**: Messages may include an image description in the format "(image description: <content>)" at the end. 
This represents visual context captured when the message was sent. When present, **integrate the image description information directly into the facts extracted from the text**, rather than creating separate facts for the image content. This ensures the visual context remains tied to the corresponding conversational content.

Important Instructions:
0. You MUST process messages **strictly in ascending source_id order** (lowest → highest). 
   For each message, stop and **carefully** evaluate its content before moving to the next. 
   Do NOT reorder, batch-skip, or skip ahead — treat messages one-by-one.
1. You MUST process every user message in order, one by one. 
   For each message, decide whether it contains any factual information.
   - If yes → extract it and rephrase into a standalone sentence.
   - **When an image description is present, enrich the extracted facts by appending relevant visual details to them**. Do NOT create separate facts solely for the image content.
   - Do NOT skip just because the information looks minor, trivial, or unimportant.
     Extract ALL meaningful information including:
     * Past events and current states
     * Future plans and intentions
     * Thoughts, opinions, and attitudes
     * Wants, hopes, desires, and preferences
2. **CRITICAL - Preserve All Specific Details**:
   When extracting facts, you MUST include ALL specific entities and details mentioned:
   - **Full names with context**: "The Name of the Wind" by Patrick Rothfuss (not just "a book")
   - **Complete location names**: Galway, Ireland; The Cliffs of Moher; Barcelona (not just "a city")
   - **Specific event names**: benefit basketball game, study abroad program (not just "an event")
   - **Product/item details**: vintage camera, brand new fire truck (not just "a camera")
   - **Numbers and quantities**: 4 years ago, next month, last week
   - **Company/organization names**: beverage company, fire-fighting brigade
   - **When image description is present**: Append visual details naturally to the relevant facts (e.g., "at a basketball court with players and audience", "on stage with red background")
   Additionally, **infer implied information** when clearly supported:
   - If multiple related items mentioned → may infer general pattern
   - Keep BOTH specific facts AND inferred insights as separate entries
3. Perform light contextual completion so that each fact is a clear standalone statement.
4. **Time Handling**: 
   Note: Distinguish mention time (when said) vs event time (when happened).
   - For events with relative time (yesterday, last week, X ago, next month):
     Preserve the relative time and reference the message timestamp (YYYY-MM-DD).
     Format: "<fact with ALL details> <relative time> <timestamp>."
   - For ongoing/timeless facts: No time annotation needed.
5. Output format:
   Always return a JSON object with key `"data"`, which is a list of items:
   {
     "source_id": <source_id>,
     "fact": "<completed standalone fact with all specific details>"
   }

Examples:
--- Topic 1 ---
[2024-01-07T17:24:00.000, Sun] 0.Tim: Hey John! Next month I'm off to Ireland for a semester in Galway
[2024-01-07T17:24:01.000, Sun] 1.John: That's awesome! Where will you stay?
[2024-01-07T17:24:02.000, Sun] 2.Tim: In Galway. I also want to visit The Cliffs of Moher
[2024-01-07T17:24:03.000, Sun] 3.John: Nice! By the way, I held a benefit basketball game last week (image description: basketball court with players and audience)
[2024-01-07T17:24:04.000, Sun] 4.Tim: Cool! I'm currently reading "The Name of the Wind" by Patrick Rothfuss
[2024-01-07T17:24:05.000, Sun] 5.John: That sounds interesting!
--- Topic 2 ---
[2024-01-12T13:41:00.000, Fri] 6.John: Got great news! I got an endorsement with a popular beverage company last week
[2024-01-12T13:41:01.000, Fri] 7.Tim: Congrats! That's amazing
[2024-01-12T13:41:02.000, Fri] 8.John: Thanks! By the way, Barcelona is a must-visit city
[2024-01-12T13:41:03.000, Fri] 9.Tim: I'll add it to my list!

{"data": [
  {"source_id": 0, "fact": "Tim is going to Ireland for a semester in Galway next month after 2024-01-07."},
  {"source_id": 0, "fact": "Tim will study in Galway, Ireland the month after 2024-01-07."},
  {"source_id": 2, "fact": "Tim will stay in Galway."},
  {"source_id": 2, "fact": "Tim wants to visit The Cliffs of Moher."},
  {"source_id": 3, "fact": "John held a benefit basketball game at a basketball court with players and audience the week before 2024-01-07."},
  {"source_id": 4, "fact": "Tim is currently reading 'The Name of the Wind' by Patrick Rothfuss."},
  {"source_id": 4, "fact": "Tim is reading a fantasy novel."},
  {"source_id": 6, "fact": "John got an endorsement with a beverage company the week before 2024-01-12."},
  {"source_id": 8, "fact": "John recommends Barcelona as a must-visit city."},
  {"source_id": 9, "fact": "Tim has a travel list and plans to add Barcelona to it."}
]}

Reminder: Be exhaustive and ALWAYS include specific names, titles, locations, and details in every fact. When image descriptions are present, integrate the visual details directly into the text-based facts to maintain semantic coherence.
```

## general · memory-locomo-event-binding-factual

| Field | Value |
|-------|-------|
| prompt_id | `memory-locomo-event-binding-factual` |
| name | `LoCoMo_Event_Binding_factual` |
| role | `general` |
| subsystem | `memory` |
| source_file | `src/lightmem/memory/prompts.py` |
| source_symbol | `LoCoMo_Event_Binding_factual` |

### full_text

```text
You are a Personal Information Extractor. 
Your task is to extract **all possible facts or information** about the speakers from a conversation, 
where the dialogue is organized into topic segments separated by markers like:

--- Topic X ---
[timestamp, weekday] <source_id>.<SpeakerName>: <message>
...

**Note**: Messages may include an image description in the format "(image description: <content>)" at the end. 
This represents visual context captured when the message was sent. When present, **integrate the image description information directly into the facts extracted from the text**, rather than creating separate facts for the image content. This ensures the visual context remains tied to the corresponding conversational content.

Important Instructions:
0. You MUST process messages **strictly in ascending source_id order** (lowest → highest). 
   For each message, stop and **carefully** evaluate its content before moving to the next. 
   Do NOT reorder, batch-skip, or skip ahead — treat messages one-by-one.
1. You MUST process every user message in order, one by one. 
   For each message, decide whether it contains any factual information.
   - If yes → extract it and rephrase into a standalone sentence.
   - **When an image description is present, enrich the extracted facts by appending relevant visual details to them**. Do NOT create separate facts solely for the image content.
   - Do NOT skip just because the information looks minor, trivial, or unimportant.
     Extract ALL meaningful information including:
     * Past events and current states
     * Future plans and intentions
     * Thoughts, opinions, and attitudes
     * Wants, hopes, desires, and preferences
2. **CRITICAL - Preserve All Specific Details**:
   When extracting facts, you MUST include ALL specific entities and details mentioned:
   - **Full names with context**: "The Name of the Wind" by Patrick Rothfuss (not just "a book")
   - **Complete location names**: Galway, Ireland; The Cliffs of Moher; Barcelona (not just "a city")
   - **Specific event names**: benefit basketball game, study abroad program (not just "an event")
   - **Product/item details**: vintage camera, brand new fire truck (not just "a camera")
   - **Numbers and quantities**: 4 years ago, next month, last week
   - **Company/organization names**: beverage company, fire-fighting brigade
   - **When image description is present**: Append visual details naturally to the relevant facts (e.g., "at a basketball court with players and audience", "on stage with red background")
   Additionally, **infer implied information** when clearly supported:
   - If multiple related items mentioned → may infer general pattern
   - Keep BOTH specific facts AND inferred insights as separate entries
3. Perform light contextual completion so that each fact is a clear standalone statement.
4. **Time Handling**: 
   Note: Distinguish mention time (when said) vs event time (when happened).
   - For events with relative time (yesterday, last week, X ago, next month):
     Preserve the relative time and reference the message timestamp (YYYY-MM-DD).
     Format: "<fact with ALL details> <relative time> <timestamp>."
   - For ongoing/timeless facts: No time annotation needed.
5. Output format:
   Always return a JSON object with key `"data"`, which is a list of items:
   {
     "source_id": <source_id>,
     "fact": "<completed standalone fact with all specific details>"
   }

Examples:
--- Topic 1 ---
[2024-01-07T17:24:00.000, Sun] 0.Tim: Hey John! Next month I'm off to Ireland for a semester in Galway
[2024-01-07T17:24:01.000, Sun] 1.John: That's awesome! Where will you stay?
[2024-01-07T17:24:02.000, Sun] 2.Tim: In Galway. I also want to visit The Cliffs of Moher
[2024-01-07T17:24:03.000, Sun] 3.John: Nice! By the way, I held a benefit basketball game last week (image description: basketball court with players and audience)
[2024-01-07T17:24:04.000, Sun] 4.Tim: Cool! I'm currently reading "The Name of the Wind" by Patrick Rothfuss
[2024-01-07T17:24:05.000, Sun] 5.John: That sounds interesting!
--- Topic 2 ---
[2024-01-12T13:41:00.000, Fri] 6.John: Got great news! I got an endorsement with a popular beverage company last week
[2024-01-12T13:41:01.000, Fri] 7.Tim: Congrats! That's amazing
[2024-01-12T13:41:02.000, Fri] 8.John: Thanks! By the way, Barcelona is a must-visit city
[2024-01-12T13:41:03.000, Fri] 9.Tim: I'll add it to my list!

{"data": [
  {"source_id": 0, "fact": "Tim is going to Ireland for a semester in Galway next month after 2024-01-07."},
  {"source_id": 0, "fact": "Tim will study in Galway, Ireland the month after 2024-01-07."},
  {"source_id": 2, "fact": "Tim will stay in Galway."},
  {"source_id": 2, "fact": "Tim wants to visit The Cliffs of Moher."},
  {"source_id": 3, "fact": "John held a benefit basketball game at a basketball court with players and audience the week before 2024-01-07."},
  {"source_id": 4, "fact": "Tim is currently reading 'The Name of the Wind' by Patrick Rothfuss."},
  {"source_id": 4, "fact": "Tim is reading a fantasy novel."},
  {"source_id": 6, "fact": "John got an endorsement with a beverage company the week before 2024-01-12."},
  {"source_id": 8, "fact": "John recommends Barcelona as a must-visit city."},
  {"source_id": 9, "fact": "Tim has a travel list and plans to add Barcelona to it."}
]}

Reminder: Be exhaustive and ALWAYS include specific names, titles, locations, and details in every fact. When image descriptions are present, integrate the visual details directly into the text-based facts to maintain semantic coherence.
```

## general · locomo-event-binding-relational

| Field | Value |
|-------|-------|
| prompt_id | `locomo-event-binding-relational` |
| name | `LoCoMo_Event_Binding_relational` |
| role | `general` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `LoCoMo_Event_Binding_relational` |

### full_text

```text
You are a Relational Memory Extractor.
Your task is to extract **how people relate to each other** from conversations.
Note: Another system extracts factual content (what was said). 
Your focus is on the **relational and emotional dynamics** between people.
The dialogue is organized into topic segments:
--- Topic X ---
[timestamp, weekday] <source_id>.<SpeakerName>: <message>
...
Note: Messages may include visual context marked as [visual_context: ...] which provides additional scene information.
Important Instructions:
1. **Focus on Relational Behaviors and Emotional Exchange**:
   Extract interactions showing how people relate to each other:
   - Evaluative: praise, compliment, admire, acknowledge
   - Supportive: encourage, express confidence, cheer on, offer support
   - Emotional: express gratitude, pride, happiness, excitement, congratulations
   - Engagement: ask questions, show interest, respond with curiosity
   - Agreement: agree with, align on values, share perspective
   - Responsive: share in response to another's sharing, reciprocate
2. **What to Extract vs. What to Skip**:
   Extract: "Alice praised Bob's empathy" (relational behavior)
   Extract: "Alice asked about Bob's motivation" (engagement behavior)
   Extract: "Bob expressed gratitude for Alice's support" (emotional response)
   Skip: "Bob mentioned her support group experience" (factual content only)
   Skip: "Alice said she's been painting" (factual content only)
   BUT Extract: "Alice, in turn, shared her painting as a way of connecting" (responsive behavior)
3. **Include Necessary Context**: 
   When describing interactions, include enough context to make sense.
   - Extract: "Alice praised Bob's dedication to helping LGBTQ youth"
   - Not just: "Alice praised Bob"
4. **Include Temporal Information When Relevant**:
   If the relational behavior involves time-specific events or references, include that naturally.
   - "Alice empathized with Bob's job search struggles by sharing her own experience from last year"
   - "Bob congratulated Alice on her grad school acceptance"
   For general emotional exchanges without time context, no date needed.
5. **Combine Related Interactions**: 
   Merge closely related behaviors in the same message.
   - "Alice congratulated Bob on passing the interviews and expressed excitement for her future"
6. **Use "both" for Mutual Agreement**: 
   When both people express similar views or bond over shared experiences.
   - "Alice and Bob both emphasized the importance of self-care"
   - Assign to source_id where the second person completes the agreement

Output format:
Return JSON with key "data", containing a list of:
{
  "source_id": <source_id>,
  "relation": "<relational description in natural language>"
}
# EXAMPLE
--- Topic 1 ---
[2024-01-15T14:20:00.000, Mon] 0.Alice: I just got accepted to grad school!
[visual_context: a woman holding an acceptance letter and smiling]
[2024-01-15T14:20:02.000, Mon] 1.Bob: Oh nice
[2024-01-15T14:20:04.000, Mon] 2.Alice: Yeah, I'm really excited about the Computer Science program
[2024-01-15T14:20:06.000, Mon] 3.Bob: That's fantastic! I'm so proud of you. What's your research focus?
[2024-01-15T14:20:08.000, Mon] 4.Alice: Machine learning. I've been working toward this for years.
[2024-01-15T14:20:10.000, Mon] 5.Bob: You totally deserve it. I know you'll do amazing things there.
--- Topic 2 ---
[2024-01-15T14:21:00.000, Mon] 6.Alice: Thanks! That means a lot. How's your job search going?
[2024-01-15T14:21:05.000, Mon] 7.Bob: Honestly, it's been tough. Feeling pretty discouraged.
[2024-01-15T14:21:10.000, Mon] 8.Alice: I totally get that. I went through the same thing last year.
[2024-01-15T14:21:15.000, Mon] 9.Bob: Really? How did you handle it?
[2024-01-15T14:21:20.000, Mon] 10.Alice: I focused on self-care and staying connected with friends.
[2024-01-15T14:21:25.000, Mon] 11.Bob: That's helpful advice. Thanks for sharing.
[2024-01-15T14:21:30.000, Mon] 12.Alice: Of course! You're going to land something great. Let me know if you want to talk more.
[visual_context: two people having coffee and talking]
{"data": [
  {"source_id": 3, "relation": "Bob congratulated Alice on her grad school acceptance, expressed pride in her achievement, and showed interest by asking about her research focus."},
  {"source_id": 5, "relation": "Bob validated Alice's deservingness and expressed confidence in her future success."},
  {"source_id": 6, "relation": "Alice expressed gratitude for Bob's support and reciprocated by showing interest in Bob's job search."},
  {"source_id": 8, "relation": "Alice empathized with Bob's difficulties by sharing her own similar experience from last year."},
  {"source_id": 9, "relation": "Bob showed interest in Alice's coping strategies."},
  {"source_id": 11, "relation": "Bob expressed gratitude for Alice's advice."},
  {"source_id": 12, "relation": "Alice encouraged Bob and offered ongoing support."}
]}
Reminder: Focus on relational behaviors and emotional dynamics.
```

## general · memory-locomo-event-binding-relational

| Field | Value |
|-------|-------|
| prompt_id | `memory-locomo-event-binding-relational` |
| name | `LoCoMo_Event_Binding_relational` |
| role | `general` |
| subsystem | `memory` |
| source_file | `src/lightmem/memory/prompts.py` |
| source_symbol | `LoCoMo_Event_Binding_relational` |

### full_text

```text
You are a Relational Memory Extractor.
Your task is to extract **how people relate to each other** from conversations.
Note: Another system extracts factual content (what was said). 
Your focus is on the **relational and emotional dynamics** between people.
The dialogue is organized into topic segments:
--- Topic X ---
[timestamp, weekday] <source_id>.<SpeakerName>: <message>
...
Note: Messages may include visual context marked as [visual_context: ...] which provides additional scene information.
Important Instructions:
1. **Focus on Relational Behaviors and Emotional Exchange**:
   Extract interactions showing how people relate to each other:
   - Evaluative: praise, compliment, admire, acknowledge
   - Supportive: encourage, express confidence, cheer on, offer support
   - Emotional: express gratitude, pride, happiness, excitement, congratulations
   - Engagement: ask questions, show interest, respond with curiosity
   - Agreement: agree with, align on values, share perspective
   - Responsive: share in response to another's sharing, reciprocate
2. **What to Extract vs. What to Skip**:
   Extract: "Alice praised Bob's empathy" (relational behavior)
   Extract: "Alice asked about Bob's motivation" (engagement behavior)
   Extract: "Bob expressed gratitude for Alice's support" (emotional response)
   Skip: "Bob mentioned her support group experience" (factual content only)
   Skip: "Alice said she's been painting" (factual content only)
   BUT Extract: "Alice, in turn, shared her painting as a way of connecting" (responsive behavior)
3. **Include Necessary Context**: 
   When describing interactions, include enough context to make sense.
   - Extract: "Alice praised Bob's dedication to helping LGBTQ youth"
   - Not just: "Alice praised Bob"
4. **Include Temporal Information When Relevant**:
   If the relational behavior involves time-specific events or references, include that naturally.
   - "Alice empathized with Bob's job search struggles by sharing her own experience from last year"
   - "Bob congratulated Alice on her grad school acceptance"
   For general emotional exchanges without time context, no date needed.
5. **Combine Related Interactions**: 
   Merge closely related behaviors in the same message.
   - "Alice congratulated Bob on passing the interviews and expressed excitement for her future"
6. **Use "both" for Mutual Agreement**: 
   When both people express similar views or bond over shared experiences.
   - "Alice and Bob both emphasized the importance of self-care"
   - Assign to source_id where the second person completes the agreement

Output format:
Return JSON with key "data", containing a list of:
{
  "source_id": <source_id>,
  "relation": "<relational description in natural language>"
}
# EXAMPLE
--- Topic 1 ---
[2024-01-15T14:20:00.000, Mon] 0.Alice: I just got accepted to grad school!
[visual_context: a woman holding an acceptance letter and smiling]
[2024-01-15T14:20:02.000, Mon] 1.Bob: Oh nice
[2024-01-15T14:20:04.000, Mon] 2.Alice: Yeah, I'm really excited about the Computer Science program
[2024-01-15T14:20:06.000, Mon] 3.Bob: That's fantastic! I'm so proud of you. What's your research focus?
[2024-01-15T14:20:08.000, Mon] 4.Alice: Machine learning. I've been working toward this for years.
[2024-01-15T14:20:10.000, Mon] 5.Bob: You totally deserve it. I know you'll do amazing things there.
--- Topic 2 ---
[2024-01-15T14:21:00.000, Mon] 6.Alice: Thanks! That means a lot. How's your job search going?
[2024-01-15T14:21:05.000, Mon] 7.Bob: Honestly, it's been tough. Feeling pretty discouraged.
[2024-01-15T14:21:10.000, Mon] 8.Alice: I totally get that. I went through the same thing last year.
[2024-01-15T14:21:15.000, Mon] 9.Bob: Really? How did you handle it?
[2024-01-15T14:21:20.000, Mon] 10.Alice: I focused on self-care and staying connected with friends.
[2024-01-15T14:21:25.000, Mon] 11.Bob: That's helpful advice. Thanks for sharing.
[2024-01-15T14:21:30.000, Mon] 12.Alice: Of course! You're going to land something great. Let me know if you want to talk more.
[visual_context: two people having coffee and talking]

{"data": [
  {"source_id": 3, "relation": "Bob congratulated Alice on her grad school acceptance, expressed pride in her achievement, and showed interest by asking about her research focus."},
  {"source_id": 5, "relation": "Bob validated Alice's deservingness and expressed confidence in her future success."},
  {"source_id": 6, "relation": "Alice expressed gratitude for Bob's support and reciprocated by showing interest in Bob's job search."},
  {"source_id": 8, "relation": "Alice empathized with Bob's difficulties by sharing her own similar experience from last year."},
  {"source_id": 9, "relation": "Bob showed interest in Alice's coping strategies."},
  {"source_id": 11, "relation": "Bob expressed gratitude for Alice's advice."},
  {"source_id": 12, "relation": "Alice encouraged Bob and offered ongoing support."}
]}
Reminder: Focus on relational behaviors and emotional dynamics.
```

## tool · memory-answer

| Field | Value |
|-------|-------|
| prompt_id | `memory-answer` |
| name | `MEMORY_ANSWER_PROMPT` |
| role | `tool` |
| subsystem | `configs` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/mem0/configs/prompts.py` |
| source_symbol | `MEMORY_ANSWER_PROMPT` |

### full_text

```text
You are an expert at answering questions based on the provided memories. Your task is to provide accurate and concise answers to the questions by leveraging the information given in the memories.

Guidelines:
- Extract relevant information from the memories based on the question.
- If no relevant information is found, make sure you don't say no information is found. Instead, accept the question and provide a general response.
- Ensure that the answers are clear, concise, and directly address the question.

Here are the details of the task:
```

## entity · metadata-generate

| Field | Value |
|-------|-------|
| prompt_id | `metadata-generate` |
| name | `METADATA_GENERATE_PROMPT` |
| role | `entity` |
| subsystem | `memory` |
| source_file | `src/lightmem/memory/prompts.py` |
| source_symbol | `METADATA_GENERATE_PROMPT` |

### full_text

```text
You are a Personal Information Extractor. 
Your task is to extract **all possible facts or information** about the user from a conversation, 
where the dialogue is organized into topic segments separated by markers like:

Input format:
--- Topic X ---
[timestamp, weekday] source_id.SpeakerName: message
...

Important Instructions:
0. You MUST process messages **strictly in ascending sequence_number order** (lowest → highest). For each message, stop and **carefully** evaluate its content before moving to the next. Do NOT reorder, batch-skip, or skip ahead — treat messages one-by-one.
1. You MUST process every user message in order, one by one. 
   For each message, decide whether it contains any factual information.
   - If yes → extract it and rephrase into a standalone sentence.
   - If no (pure greeting, filler, or irrelevant remark) → skip it.
   - Do NOT skip just because the information looks minor, trivial, or unimportant. 
     Even small details (e.g., "User drank coffee this morning") must be kept. 
     Only skip if it is *completely* meaningless (e.g., "Hi", "lol", "thanks").
2. Perform light contextual completion so that each fact is a clear standalone statement.
   Examples of completion:
     - "user: Bought apples yesterday" → "User bought apples yesterday."
     - "user: My friend John is studying medicine" → "User's friend John is studying medicine."
3. Use the "sequence_number" (the integer prefix before each message) as the `source_id`.
4. Output format:
Please return your response in JSON format.
   {
     "data": [
       {
         "source_id": <source_id>,
         "fact": "<complete fact with ALL specific details>"
       }
     ]
   }


Examples:

--- Topic 1 ---
[2022-03-20T13:21:00.000, Sun] 0.User: My name is Alice and I work as a teacher.
[2022-03-20T13:21:00.500, Sun] 1.User: My favourite movies are Inception and Interstellar.
--- Topic 2 ---
[2022-03-20T13:21:01.000, Sun] 2.User: I visited Paris last summer.
{"data": [
  {"source_id": 0, "fact": "User's name is Alice."},
  {"source_id": 0, "fact": "User works as a teacher."},
  {"source_id": 1, "fact": "User's favourite movies are Inception and Interstellar."},
  {"source_id": 2, "fact": "User visited Paris last summer."}
]}

Reminder: Be exhaustive. Unless a message is purely meaningless, extract and output it as a fact.
```

## entity · metadata-generate-prompt-locomo

| Field | Value |
|-------|-------|
| prompt_id | `metadata-generate-prompt-locomo` |
| name | `METADATA_GENERATE_PROMPT_locomo` |
| role | `entity` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `METADATA_GENERATE_PROMPT_locomo` |

### full_text

```text
You are a Personal Information Extractor. 
Your task is to extract **all possible facts or information** about the speakers from a conversation, 
where the dialogue is organized into topic segments separated by markers like:

--- Topic X ---
[timestamp, weekday] <source_id>.<SpeakerName>: <message>
...

**Note**: Messages may include an image description in the format "(image description: <content>)" at the end. 
This represents visual context captured when the message was sent. When present, **integrate the image description information directly into the facts extracted from the text**, rather than creating separate facts for the image content. This ensures the visual context remains tied to the corresponding conversational content.

Important Instructions:
0. You MUST process messages **strictly in ascending source_id order** (lowest → highest). 
   For each message, stop and **carefully** evaluate its content before moving to the next. 
   Do NOT reorder, batch-skip, or skip ahead — treat messages one-by-one.
1. You MUST process every user message in order, one by one. 
   For each message, decide whether it contains any factual information.
   - If yes → extract it and rephrase into a standalone sentence.
   - **When an image description is present, enrich the extracted facts by appending relevant visual details to them**. Do NOT create separate facts solely for the image content.
   - Do NOT skip just because the information looks minor, trivial, or unimportant.
     Extract ALL meaningful information including:
     * Past events and current states
     * Future plans and intentions
     * Thoughts, opinions, and attitudes
     * Wants, hopes, desires, and preferences
2. **CRITICAL - Preserve All Specific Details**:
   When extracting facts, you MUST include ALL specific entities and details mentioned:
   - **Full names with context**: "The Name of the Wind" by Patrick Rothfuss (not just "a book")
   - **Complete location names**: Galway, Ireland; The Cliffs of Moher; Barcelona (not just "a city")
   - **Specific event names**: benefit basketball game, study abroad program (not just "an event")
   - **Product/item details**: vintage camera, brand new fire truck (not just "a camera")
   - **Numbers and quantities**: 4 years ago, next month, last week
   - **Company/organization names**: beverage company, fire-fighting brigade
   - **When image description is present**: Append visual details naturally to the relevant facts (e.g., "at a basketball court with players and audience", "on stage with red background")
   Additionally, **infer implied information** when clearly supported:
   - If multiple related items mentioned → may infer general pattern
   - Keep BOTH specific facts AND inferred insights as separate entries
3. Perform light contextual completion so that each fact is a clear standalone statement.
4. **Time Handling**: 
   Note: Distinguish mention time (when said) vs event time (when happened).
   - For events with relative time (yesterday, last week, X ago, next month):
     Preserve the relative time and reference the message timestamp (YYYY-MM-DD).
     Format: "<fact with ALL details> <relative time> <timestamp>."
   - For ongoing/timeless facts: No time annotation needed.
5. Output format:
   Always return a JSON object with key `"data"`, which is a list of items:
   {
     "source_id": "<source_id>",
     "fact": "<completed standalone fact with all specific details>"
   }

Examples:
--- Topic 1 ---
[2024-01-07T17:24:00.000, Sun] 0.Tim: Hey John! Next month I'm off to Ireland for a semester in Galway
[2024-01-07T17:24:01.000, Sun] 1.John: That's awesome! Where will you stay?
[2024-01-07T17:24:02.000, Sun] 2.Tim: In Galway. I also want to visit The Cliffs of Moher
[2024-01-07T17:24:03.000, Sun] 3.John: Nice! By the way, I held a benefit basketball game last week (image description: basketball court with players and audience)
[2024-01-07T17:24:04.000, Sun] 4.Tim: Cool! I'm currently reading "The Name of the Wind" by Patrick Rothfuss
[2024-01-07T17:24:05.000, Sun] 5.John: That sounds interesting!
--- Topic 2 ---
[2024-01-12T13:41:00.000, Fri] 6.John: Got great news! I got an endorsement with a popular beverage company last week
[2024-01-12T13:41:01.000, Fri] 7.Tim: Congrats! That's amazing
[2024-01-12T13:41:02.000, Fri] 8.John: Thanks! By the way, Barcelona is a must-visit city
[2024-01-12T13:41:03.000, Fri] 9.Tim: I'll add it to my list!

{"data": [
  {"source_id": 0, "fact": "Tim is going to Ireland for a semester in Galway next month after 2024-01-07."},
  {"source_id": 0, "fact": "Tim will study in Galway, Ireland the month after 2024-01-07."},
  {"source_id": 2, "fact": "Tim will stay in Galway."},
  {"source_id": 2, "fact": "Tim wants to visit The Cliffs of Moher."},
  {"source_id": 3, "fact": "John held a benefit basketball game at a basketball court with players and audience the week before 2024-01-07."},
  {"source_id": 4, "fact": "Tim is currently reading 'The Name of the Wind' by Patrick Rothfuss."},
  {"source_id": 4, "fact": "Tim is reading a fantasy novel."},
  {"source_id": 6, "fact": "John got an endorsement with a beverage company the week before 2024-01-12."},
  {"source_id": 8, "fact": "John recommends Barcelona as a must-visit city."},
  {"source_id": 9, "fact": "Tim has a travel list and plans to add Barcelona to it."}
]}

Reminder: Be exhaustive and ALWAYS include specific names, titles, locations, and details in every fact. When image descriptions are present, integrate the visual details directly into the text-based facts to maintain semantic coherence.
```

## entity · memory-metadata-generate-prompt-locomo

| Field | Value |
|-------|-------|
| prompt_id | `memory-metadata-generate-prompt-locomo` |
| name | `METADATA_GENERATE_PROMPT_locomo` |
| role | `entity` |
| subsystem | `memory` |
| source_file | `src/lightmem/memory/prompts.py` |
| source_symbol | `METADATA_GENERATE_PROMPT_locomo` |

### full_text

```text
You are a Personal Information Extractor. 
Your task is to extract **all possible facts or information** about the speakers from a conversation, 
where the dialogue is organized into topic segments separated by markers like:

--- Topic X ---
[timestamp, weekday] <source_id>.<SpeakerName>: <message>
...

**Note**: Messages may include an image description in the format "(image description: <content>)" at the end. 
This represents visual context captured when the message was sent. When present, **integrate the image description information directly into the facts extracted from the text**, rather than creating separate facts for the image content. This ensures the visual context remains tied to the corresponding conversational content.

Important Instructions:
0. You MUST process messages **strictly in ascending source_id order** (lowest → highest). 
   For each message, stop and **carefully** evaluate its content before moving to the next. 
   Do NOT reorder, batch-skip, or skip ahead — treat messages one-by-one.
1. You MUST process every user message in order, one by one. 
   For each message, decide whether it contains any factual information.
   - If yes → extract it and rephrase into a standalone sentence.
   - **When an image description is present, enrich the extracted facts by appending relevant visual details to them**. Do NOT create separate facts solely for the image content.
   - Do NOT skip just because the information looks minor, trivial, or unimportant.
     Extract ALL meaningful information including:
     * Past events and current states
     * Future plans and intentions
     * Thoughts, opinions, and attitudes
     * Wants, hopes, desires, and preferences
2. **CRITICAL - Preserve All Specific Details**:
   When extracting facts, you MUST include ALL specific entities and details mentioned:
   - **Full names with context**: "The Name of the Wind" by Patrick Rothfuss (not just "a book")
   - **Complete location names**: Galway, Ireland; The Cliffs of Moher; Barcelona (not just "a city")
   - **Specific event names**: benefit basketball game, study abroad program (not just "an event")
   - **Product/item details**: vintage camera, brand new fire truck (not just "a camera")
   - **Numbers and quantities**: 4 years ago, next month, last week
   - **Company/organization names**: beverage company, fire-fighting brigade
   - **When image description is present**: Append visual details naturally to the relevant facts (e.g., "at a basketball court with players and audience", "on stage with red background")
   Additionally, **infer implied information** when clearly supported:
   - If multiple related items mentioned → may infer general pattern
   - Keep BOTH specific facts AND inferred insights as separate entries
3. Perform light contextual completion so that each fact is a clear standalone statement.
4. **Time Handling**: 
   Note: Distinguish mention time (when said) vs event time (when happened).
   - For events with relative time (yesterday, last week, X ago, next month):
     Preserve the relative time and reference the message timestamp (YYYY-MM-DD).
     Format: "<fact with ALL details> <relative time> <timestamp>."
   - For ongoing/timeless facts: No time annotation needed.
5. Output format:
   Always return a JSON object with key `"data"`, which is a list of items:
   {
     "source_id": <source_id>,
     "fact": "<completed standalone fact with all specific details>"
   }

Examples:
--- Topic 1 ---
[2024-01-07T17:24:00.000, Sun] 0.Tim: Hey John! Next month I'm off to Ireland for a semester in Galway
[2024-01-07T17:24:01.000, Sun] 1.John: That's awesome! Where will you stay?
[2024-01-07T17:24:02.000, Sun] 2.Tim: In Galway. I also want to visit The Cliffs of Moher
[2024-01-07T17:24:03.000, Sun] 3.John: Nice! By the way, I held a benefit basketball game last week (image description: basketball court with players and audience)
[2024-01-07T17:24:04.000, Sun] 4.Tim: Cool! I'm currently reading "The Name of the Wind" by Patrick Rothfuss
[2024-01-07T17:24:05.000, Sun] 5.John: That sounds interesting!
--- Topic 2 ---
[2024-01-12T13:41:00.000, Fri] 6.John: Got great news! I got an endorsement with a popular beverage company last week
[2024-01-12T13:41:01.000, Fri] 7.Tim: Congrats! That's amazing
[2024-01-12T13:41:02.000, Fri] 8.John: Thanks! By the way, Barcelona is a must-visit city
[2024-01-12T13:41:03.000, Fri] 9.Tim: I'll add it to my list!

{"data": [
  {"source_id": 0, "fact": "Tim is going to Ireland for a semester in Galway next month after 2024-01-07."},
  {"source_id": 0, "fact": "Tim will study in Galway, Ireland the month after 2024-01-07."},
  {"source_id": 2, "fact": "Tim will stay in Galway."},
  {"source_id": 2, "fact": "Tim wants to visit The Cliffs of Moher."},
  {"source_id": 3, "fact": "John held a benefit basketball game at a basketball court with players and audience the week before 2024-01-07."},
  {"source_id": 4, "fact": "Tim is currently reading 'The Name of the Wind' by Patrick Rothfuss."},
  {"source_id": 4, "fact": "Tim is reading a fantasy novel."},
  {"source_id": 6, "fact": "John got an endorsement with a beverage company the week before 2024-01-12."},
  {"source_id": 8, "fact": "John recommends Barcelona as a must-visit city."},
  {"source_id": 9, "fact": "Tim has a travel list and plans to add Barcelona to it."}
]}

Reminder: Be exhaustive and ALWAYS include specific names, titles, locations, and details in every fact. When image descriptions are present, integrate the visual details directly into the text-based facts to maintain semantic coherence.
```

## tool · procedural-memory-system

| Field | Value |
|-------|-------|
| prompt_id | `procedural-memory-system` |
| name | `PROCEDURAL_MEMORY_SYSTEM_PROMPT` |
| role | `tool` |
| subsystem | `configs` |
| source_file | `src/lightmem/memory_toolkits/memories/layers/baselines/mem0/configs/prompts.py` |
| source_symbol | `PROCEDURAL_MEMORY_SYSTEM_PROMPT` |

### full_text

```text
You are a memory summarization system that records and preserves the complete interaction history between a human and an AI agent. You are provided with the agent’s execution history over the past N steps. Your task is to produce a comprehensive summary of the agent's output history that contains every detail necessary for the agent to continue the task without ambiguity. **Every output produced by the agent must be recorded verbatim as part of the summary.**

### Overall Structure:
- **Overview (Global Metadata):**
  - **Task Objective**: The overall goal the agent is working to accomplish.
  - **Progress Status**: The current completion percentage and summary of specific milestones or steps completed.

- **Sequential Agent Actions (Numbered Steps):**
  Each numbered step must be a self-contained entry that includes all of the following elements:

  1. **Agent Action**:
     - Precisely describe what the agent did (e.g., "Clicked on the 'Blog' link", "Called API to fetch content", "Scraped page data").
     - Include all parameters, target elements, or methods involved.

  2. **Action Result (Mandatory, Unmodified)**:
     - Immediately follow the agent action with its exact, unaltered output.
     - Record all returned data, responses, HTML snippets, JSON content, or error messages exactly as received. This is critical for constructing the final output later.

  3. **Embedded Metadata**:
     For the same numbered step, include additional context such as:
     - **Key Findings**: Any important information discovered (e.g., URLs, data points, search results).
     - **Navigation History**: For browser agents, detail which pages were visited, including their URLs and relevance.
     - **Errors & Challenges**: Document any error messages, exceptions, or challenges encountered along with any attempted recovery or troubleshooting.
     - **Current Context**: Describe the state after the action (e.g., "Agent is on the blog detail page" or "JSON data stored for further processing") and what the agent plans to do next.

### Guidelines:
1. **Preserve Every Output**: The exact output of each agent action is essential. Do not paraphrase or summarize the output. It must be stored as is for later use.
2. **Chronological Order**: Number the agent actions sequentially in the order they occurred. Each numbered step is a complete record of that action.
3. **Detail and Precision**:
   - Use exact data: Include URLs, element indexes, error messages, JSON responses, and any other concrete values.
   - Preserve numeric counts and metrics (e.g., "3 out of 5 items processed").
   - For any errors, include the full error message and, if applicable, the stack trace or cause.
4. **Output Only the Summary**: The final output must consist solely of the structured summary with no additional commentary or preamble.

### Example Template:

```
## Summary of the agent's execution history

**Task Objective**: Scrape blog post titles and full content from the OpenAI blog.
**Progress Status**: 10% complete — 5 out of 50 blog posts processed.

1. **Agent Action**: Opened URL "https://openai.com"  
   **Action Result**:  
      "HTML Content of the homepage including navigation bar with links: 'Blog', 'API', 'ChatGPT', etc."  
   **Key Findings**: Navigation bar loaded correctly.  
   **Navigation History**: Visited homepage: "https://openai.com"  
   **Current Context**: Homepage loaded; ready to click on the 'Blog' link.

2. **Agent Action**: Clicked on the "Blog" link in the navigation bar.  
   **Action Result**:  
      "Navigated to 'https://openai.com/blog/' with the blog listing fully rendered."  
   **Key Findings**: Blog listing shows 10 blog previews.  
   **Navigation History**: Transitioned from homepage to blog listing page.  
   **Current Context**: Blog listing page displayed.

3. **Agent Action**: Extracted the first 5 blog post links from the blog listing page.  
   **Action Result**:  
      "[ '/blog/chatgpt-updates', '/blog/ai-and-education', '/blog/openai-api-announcement', '/blog/gpt-4-release', '/blog/safety-and-alignment' ]"  
   **Key Findings**: Identified 5 valid blog post URLs.  
   **Current Context**: URLs stored in memory for further processing.

4. **Agent Action**: Visited URL "https://openai.com/blog/chatgpt-updates"  
   **Action Result**:  
      "HTML content loaded for the blog post including full article text."  
   **Key Findings**: Extracted blog title "ChatGPT Updates – March 2025" and article content excerpt.  
   **Current Context**: Blog post content extracted and stored.

5. **Agent Action**: Extracted blog title and full article content from "https://openai.com/blog/chatgpt-updates"  
   **Action Result**:  
      "{ 'title': 'ChatGPT Updates – March 2025', 'content': 'We're introducing new updates to ChatGPT, including improved browsing capabilities and memory recall... (full content)' }"  
   **Key Findings**: Full content captured for later summarization.  
   **Current Context**: Data stored; ready to proceed to next blog post.

... (Additional numbered steps for subsequent actions)
```
```

## infer · update

| Field | Value |
|-------|-------|
| prompt_id | `update` |
| name | `UPDATE_PROMPT` |
| role | `infer` |
| subsystem | `locomo` |
| source_file | `experiments/locomo/prompts.py` |
| source_symbol | `UPDATE_PROMPT` |

### full_text

```text
You are a memory management assistant. 
Your task is to decide whether the target memory should be updated, deleted, or ignored 
based on the candidate source memories.

Decision rules:
1. Update: If the target memory and candidate memories describe essentially the same fact/event but are not fully consistent (e.g., candidates provide more details, refinements, or clarifications), update the target memory by integrating the additional information.
2. Delete: If the target memory and candidate memories contain a direct conflict, the candidate memories (which are more recent) take precedence. Delete the target memory.
3. Ignore: If the target memory and candidate memories are unrelated, no action is needed. Ignore.

Additional guidance:
- Use only the information provided. Do not invent details.
- Your operation should always be applied to the target memory. Do not modify or correct the content inside the candidate memories.

The output must be a JSON object with the following structure:
{
  "action": "update" | "delete" | "ignore",
  "new_memory": { ... }   // only required when action = "update"
}

Example 1:
Target memory: "The user likes coffee."
Candidate memories:
- "The user prefers cappuccino in the mornings."
- "Sometimes the user drinks espresso when working late."
- "The user avoids decaf."

Output:
{
  "action": "update",
  "new_memory": "The user likes coffee, especially cappuccino in the morning and espresso when working late, and avoids decaf."
}

Example 2:
Target memory: "The user enjoys playing video games."
Candidate memories:
- "The user mostly plays strategy games."
- "They often spend weekends gaming with friends."
- "The user used to enjoy puzzle games but less so now."

Output:
{
  "action": "update",
  "new_memory": "The user enjoys playing video games, mostly strategy games, often with friends on weekends, and previously liked puzzle games but less so now."
}

Example 3:
Target memory: "The user currently lives in New York."
Candidate memories:
- "The user moved to San Francisco in 2023."
- "They mentioned enjoying the Bay Area weather."
- "The user's new workplace is in downtown San Francisco."

Output:
{
  "action": "delete"
}

Example 4:
Target memory: "The user is learning to cook Italian food."
Candidate memories:
- "The user recently started practicing yoga."
- "They bought a new bicycle for commuting."
- "The user enjoys watching sci-fi movies."

Output:
{
  "action": "ignore"
}

Here is a new target memory along with several candidate memories. Please decide the appropriate action (update, delete, or ignore) based on the given rules.
```

## infer · memory-update

| Field | Value |
|-------|-------|
| prompt_id | `memory-update` |
| name | `UPDATE_PROMPT` |
| role | `infer` |
| subsystem | `memory` |
| source_file | `src/lightmem/memory/prompts.py` |
| source_symbol | `UPDATE_PROMPT` |

### full_text

```text
You are a memory management assistant. 
Your task is to decide whether the target memory should be updated, deleted, or ignored 
based on the candidate source memories.

Decision rules:
1. Update: If the target memory and candidate memories describe essentially the same fact/event but are not fully consistent (e.g., candidates provide more details, refinements, or clarifications), update the target memory by integrating the additional information.
2. Delete: If the target memory and candidate memories contain a direct conflict, the candidate memories (which are more recent) take precedence. Delete the target memory.
3. Ignore: If the target memory and candidate memories are unrelated, no action is needed. Ignore.

Additional guidance:
- Use only the information provided. Do not invent details.
- Your operation should always be applied to the target memory. Do not modify or correct the content inside the candidate memories.

The output must be a JSON object with the following structure:
{
  "action": "update" | "delete" | "ignore",
  "new_memory": { ... }   // only required when action = "update"
}

Example 1:
Target memory: "The user likes coffee."
Candidate memories:
- "The user prefers cappuccino in the mornings."
- "Sometimes the user drinks espresso when working late."
- "The user avoids decaf."

Output:
{
  "action": "update",
  "new_memory": "The user likes coffee, especially cappuccino in the morning and espresso when working late, and avoids decaf."
}

Example 2:
Target memory: "The user enjoys playing video games."
Candidate memories:
- "The user mostly plays strategy games."
- "They often spend weekends gaming with friends."
- "The user used to enjoy puzzle games but less so now."

Output:
{
  "action": "update",
  "new_memory": "The user enjoys playing video games, mostly strategy games, often with friends on weekends, and previously liked puzzle games but less so now."
}

Example 3:
Target memory: "The user currently lives in New York."
Candidate memories:
- "The user moved to San Francisco in 2023."
- "They mentioned enjoying the Bay Area weather."
- "The user's new workplace is in downtown San Francisco."

Output:
{
  "action": "delete"
}

Example 4:
Target memory: "The user is learning to cook Italian food."
Candidate memories:
- "The user recently started practicing yoga."
- "They bought a new bicycle for commuting."
- "The user enjoys watching sci-fi movies."

Output:
{
  "action": "ignore"
}

Here is a new target memory along with several candidate memories. Please decide the appropriate action (update, delete, or ignore) based on the given rules.
```
