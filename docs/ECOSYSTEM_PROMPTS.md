# memspine — Ecosystem Prompts Catalog (Pass #5)

**Status:** Pass #5 · Full verbatim catalog · **Air-gap** · **Built:** 2026-07-10

This document is the **Pass #5 full prompt catalog** — every LLM-prompt surface surveyed across 24 open-source memory engines pasted **verbatim** below. There are no "see staging" stubs; every `## <repo>` section contains the full text of every prompt lifted from `docs/survey/_staging/<repo>/PROMPTS.md`.

**Text anchors.** Each prompt subsection is preceded by a stable HTML anchor of the form `<a id="{slug}"></a>`, where `{slug} = lowercase("<repo>-<prompt_id>")` with `@` → `-at-` and every remaining non-alnum → `-`. The [`ECOSYSTEM_PROMPTS.csv`](./exports/ECOSYSTEM_PROMPTS.csv) companion (~598 rows) carries the identical slug in its `text_anchor` column so any row in the CSV points at exactly one anchor in this file.

**Companions:**
- [`exports/ECOSYSTEM_PROMPTS.csv`](./exports/ECOSYSTEM_PROMPTS.csv) — one CSV row per `###` prompt (repo, prompt_id, packaging, path, purpose, call_sites, call_order, hot_path, output_schema, text_anchor).
- [`exports/ECOSYSTEM_ALGORITHMS.csv`](./exports/ECOSYSTEM_ALGORITHMS.csv) · [`exports/ECOSYSTEM_PACKAGE_GAPS.csv`](./exports/ECOSYSTEM_PACKAGE_GAPS.csv) · [`exports/ECOSYSTEM_PACKAGE_ADOPTION.csv`](./exports/ECOSYSTEM_PACKAGE_ADOPTION.csv).
- Per-repo staging files under [`docs/survey/_staging/<repo>/PROMPTS.md`](./survey/_staging/) remain the immutable source of truth; this hub is a rewritten copy with hub-scoped headings + anchors.

## Repo index

| repo | prompts | staging file |
|------|---------|--------------|
| [memspine](#memspine) | 18 | `_staging/memspine/PROMPTS.md` |
| [A-mem](#a-mem) | 3 | `_staging/A-mem/PROMPTS.md` |
| [cognee](#cognee) | 65 | `_staging/cognee/PROMPTS.md` |
| [EverMemOS](#evermemos) | 6 | `_staging/EverMemOS/PROMPTS.md` |
| [graphiti](#graphiti) | 26 | `_staging/graphiti/PROMPTS.md` |
| [hindsight](#hindsight) | 17 | `_staging/hindsight/PROMPTS.md` |
| [honcho](#honcho) | 8 | `_staging/honcho/PROMPTS.md` |
| [langmem](#langmem) | 24 | `_staging/langmem/PROMPTS.md` |
| [LightMem](#lightmem) | 22 | `_staging/LightMem/PROMPTS.md` |
| [mem0](#mem0) | 16 | `_staging/mem0/PROMPTS.md` |
| [MemMachine](#memmachine) | 26 | `_staging/MemMachine/PROMPTS.md` |
| [memobase](#memobase) | 16 | `_staging/memobase/PROMPTS.md` |
| [memonto](#memonto) | 7 | `_staging/memonto/PROMPTS.md` |
| [Memori](#memori) | 4 | `_staging/Memori/PROMPTS.md` |
| [MemoryBear](#memorybear) | 32 | `_staging/MemoryBear/PROMPTS.md` |
| [memory-opensource](#memory-opensource) | 0 (evidence-only) | `_staging/memory-opensource/PROMPTS.md` |
| [MemOS](#memos) | 118 | `_staging/MemOS/PROMPTS.md` |
| [memU](#memu) | 22 | `_staging/memU/PROMPTS.md` |
| [OpenMemory](#openmemory) | 0 (evidence-only) | `_staging/OpenMemory/PROMPTS.md` |
| [powermem](#powermem) | 32 | `_staging/powermem/PROMPTS.md` |
| [ReMe](#reme) | 30 | `_staging/ReMe/PROMPTS.md` |
| [Second-Me](#second-me) | 67 | `_staging/Second-Me/PROMPTS.md` |
| [SimpleMem](#simplemem) | 23 | `_staging/SimpleMem/PROMPTS.md` |
| [telemem](#telemem) | 14 | `_staging/telemem/PROMPTS.md` |

**Total repos:** 24 · **Total CSV rows:** 598 · **Total anchored prompts:** matches CSV rows one-to-one.

Section order matches the CSV row order. Each repo section begins with a mini-index (id · purpose · hot_path · path) parsed from its staging sections, followed by the full verbatim bodies.

## memspine

**Staging:** `docs/survey/_staging/memspine/PROMPTS.md`

<!--CONT:memspine:1-->


## A-mem

**Staging:** `docs/survey/_staging/A-mem/PROMPTS.md`

# A-mem — prompt inventory (Pass #5)

**SHA:** `ceffb860f0712bbae97b184d440df62bc910ca8d` · Air-gap · Full verbatim text below.

Pass #3 / ECOSYSTEM claimed **2 inline JSON prompts**. Confirmed: exactly two LLM prompt surfaces in `agentic_memory/`. System message in controllers is a fixed JSON-enforcement string (documented as packaging glue, not a third content prompt).

---

### am.analyze_content
- packaging: python_const|inline
- path: `agentic_memory/memory_system.py:AgenticMemorySystem.analyze_content` (L176–203)
- purpose: write_extract
- call_sites: []  *(no production callers; tests do not invoke; `add_note` never calls)*
- call_order: n/a (dead on write hot path)
- inputs: content appended after template (`Content for analysis:\n` + `content`)
- output_schema: JSON `{keywords: string[], context: string, tags: string[]}` via `response_format` json_schema
- hot_path: dead
- full_text: |
    Generate a structured analysis of the following content by:
                1. Identifying the most salient keywords (focus on nouns, verbs, and key concepts)
                2. Extracting core themes and contextual elements
                3. Creating relevant categorical tags

                Format the response as a JSON object:
                {
                    "keywords": [
                        // several specific, distinct keywords that capture key concepts and terminology
                        // Order from most to least important
                        // Don't include keywords that are the name of the speaker or time
                        // At least three keywords, but don't be too redundant.
                    ],
                    "context": 
                        // one sentence summarizing:
                        // - Main topic/domain
                        // - Key arguments/points
                        // - Intended audience/purpose
                    ,
                    "tags": [
                        // several broad categories/themes for classification
                        // Include domain, format, and type tags
                        // At least three tags, but don't be too redundant.
                    ]
                }

                Content for analysis:

---

### am.evolution
- packaging: python_const|inline
- path: `agentic_memory/memory_system.py:AgenticMemorySystem.__init__` → `self._evolution_system_prompt` (L127–157); formatted in `process_memory` (L612–618)
- purpose: evolve
- call_sites: [`agentic_memory/memory_system.py:AgenticMemorySystem.process_memory`]
- call_order: 1 in write flow (after Chroma neighbor fetch; skipped when `self.memories` empty)
- inputs: `{context}`, `{content}`, `{keywords}`, `{nearest_neighbors_memories}`, `{neighbor_number}`
- output_schema: JSON `{should_evolve: bool, actions: string[], suggested_connections: string[], tags_to_update: string[], new_context_neighborhood: string[], new_tags_neighborhood: string[][]}` (strict schema in `get_completion`)
- hot_path: yes
- full_text: |
                                    You are an AI memory evolution agent responsible for managing and evolving a knowledge base.
                                    Analyze the the new memory note according to keywords and context, also with their several nearest neighbors memory.
                                    Make decisions about its evolution.  

                                    The new memory context:
                                    {context}
                                    content: {content}
                                    keywords: {keywords}

                                    The nearest neighbors memories:
                                    {nearest_neighbors_memories}

                                    Based on this information, determine:
                                    1. Should this memory be evolved? Consider its relationships with other memories.
                                    2. What specific actions should be taken (strengthen, update_neighbor)?
                                       2.1 If choose to strengthen the connection, which memory should it be connected to? Can you give the updated tags of this memory?
                                       2.2 If choose to update_neighbor, you can update the context and tags of these memories based on the understanding of these memories. If the context and the tags are not updated, the new context and tags should be the same as the original ones. Generate the new context and tags in the sequential order of the input neighbors.
                                    Tags should be determined by the content of these characteristic of these memories, which can be used to retrieve them later and categorize them.
                                    Note that the length of new_tags_neighborhood must equal the number of input neighbors, and the length of new_context_neighborhood must equal the number of input neighbors.
                                    The number of neighbors is {neighbor_number}.
                                    Return your decision in JSON format with the following structure:
                                    {{
                                        "should_evolve": True or False,
                                        "actions": ["strengthen", "update_neighbor"],
                                        "suggested_connections": ["neighbor_memory_ids"],
                                        "tags_to_update": ["tag_1",..."tag_n"], 
                                        "new_context_neighborhood": ["new context",...,"new context"],
                                        "new_tags_neighborhood": [["tag_1",...,"tag_n"],...["tag_1",...,"tag_n"]],
                                    }}

---

### am.llm_json_system (controller glue)
- packaging: inline
- path: `agentic_memory/llm_controller.py:OpenAIController.get_completion` / `OllamaController.get_completion`
- purpose: other
- call_sites: [both controllers on every completion]
- call_order: wraps every LLM call
- inputs: none
- output_schema: n/a
- hot_path: yes
- full_text: |
    You must respond with a JSON object.

---

## Notes

- No YAML/Jinja prompt packs.
- Ollama path uses `litellm.completion` with `ollama_chat/{model}`; OpenAI path uses official `openai` SDK (litellm also imported at module top of `memory_system.py` but unused there).
- Neighbor string format for evolution comes from `find_related_memories` (tab-separated memory index / timestamp / content / context / keywords / tags) — not raw UUIDs in the prompt body; `suggested_connections` still asks for neighbor_memory_ids (schema/prompt mismatch risk).


## cognee

**Staging:** `docs/survey/_staging/cognee/PROMPTS.md`

<!--CONT:cognee:1-->


## EverMemOS

**Staging:** `docs/survey/_staging/EverMemOS/PROMPTS.md`

<!--CONT:EverMemOS:1-->


## graphiti

**Staging:** `docs/survey/_staging/graphiti/PROMPTS.md`

<!--CONT:graphiti:1-->


## hindsight

**Staging:** `docs/survey/_staging/hindsight/PROMPTS.md`

<!--CONT:hindsight:1-->


## honcho

**Staging:** `docs/survey/_staging/honcho/PROMPTS.md`

<!--CONT:honcho:1-->


## langmem

**Staging:** `docs/survey/_staging/langmem/PROMPTS.md`

<!--CONT:langmem:1-->


## LightMem

**Staging:** `docs/survey/_staging/LightMem/PROMPTS.md`

<!--CONT:LightMem:1-->


## mem0

**Staging:** `docs/survey/_staging/mem0/PROMPTS.md`

<!--CONT:mem0:1-->


## MemMachine

**Staging:** `docs/survey/_staging/MemMachine/PROMPTS.md`

<!--CONT:MemMachine:1-->


## memobase

**Staging:** `docs/survey/_staging/memobase/PROMPTS.md`

<!--CONT:memobase:1-->


## memonto

**Staging:** `docs/survey/_staging/memonto/PROMPTS.md`

# memonto — Pass #5 prompts (FULL)

**Repo:** `D:\mem\memonto` · **SHA:** `65e89eac12f5cefbb875b6c107470c9cf316cc95`  
**Packaging:** `string.Template` files under `memonto/prompts/*.prompt`, loaded by `utils/llm.py:load_prompt` → substituted in `llms/base_llm.py:LLMModel._fit_to_context_window`.  
**Count:** 7 files · **hot:** 5 · **dead:** 2

---

### commit_to_memory
- packaging: txt (`.prompt` + `string.Template`)
- path: `memonto/prompts/commit_to_memory.prompt`
- purpose: write_extract
- call_sites: [`memonto/core/retain.py:save_memory`]
- call_order: 3 in write flow (after optional expand/update; primary extract)
- inputs: `ontology`, `user_message`, `updated_memory`, `relevant_memory`
- output_schema: plain-text Python script (rdflib adds into existing `data` Graph); no JSON schema
- hot_path: yes
- full_text: |
    You are a software engineer tasked to create a Python script to extract ALL POSSIBLE relevant information from a user message that maps to a predefined RDF ontology.

    Given this RDF graph that defines our desired ontology and namespaces:
    ```
    ${ontology}
    ```

    And this user message:
    ```
    ${user_message}
    ```

    And these removed memories:
    ```
    ${updated_memory}
    ```

    And these relevant memories:
    ```
    ${relevant_memory}
    ```

    Analyze the user message to find AS MUCH new information AS POSSIBLE that could fit onto the above ontology while adhering to these rules:
    - First find all the new information in the user message that maps onto BOTH the above ontology and ESPECIALLY the removed memories.
    - Second check if the relevant memories can help extract even more information from the user message that maps onto the ontology or removed memories.
    - Third apply only the existing namespaces to the extracted information.
    - Finally create the script that will add the extracted information to an rdflib graph called `data`.
    - NEVER generate code that initializes new graphs, namespaces, classes, properties, etc.
    - GENERATE Python code to add the triples with the relevant information assuming rdflib Graph `data` and the newly added namespaces already exists.
    - GENERATE all necessary rdflib and rdflib.namespace imports for the code to run.
    - If there are no relevant information then RETURN a print an empty string and nothing else.
    - Please generate the code without using ``` or any other code formatting symbols. Return only plain text.

---

### commit_to_memory_error_handling
- packaging: txt (`.prompt` + `string.Template`)
- path: `memonto/prompts/commit_to_memory_error_handling.prompt`
- purpose: write_extract
- call_sites: [`memonto/core/retain.py:_run_script`]
- call_order: 3b in write flow (on `exec` failure inside `_run_script`)
- inputs: `error`, `script`, `user_message`, `ontology`
- output_schema: plain-text repaired Python script
- hot_path: reserved (invoked on exception; **fixed script is not re-executed** — see METHODOLOGY staleness)
- full_text: |
    You are a software engineer tasked to fix the following script that adds the following edges to an rdflib graph in Python.

    Given the following error when running the previous script:
    ```
    ${error}
    ```

    And the following script to add newly extract information:
    ```
    ${script}
    ```

    From the following user message:
    ```
    ${user_message}
    ```

    Onto to the following ontology:
    ```
    ${ontology}
    ```

    Fix the error and return a new Python script adhering to these rules:
    - NEVER generate code that initializes new graphs, namespaces, classes, properties, etc.
    - GENERATE Python code to add the triples with the relevant information assuming rdflib Graph `data` and the newly added namespaces already exists.
    - GENERATE all necessary rdflib and rdflib.namespace imports for the code to run.
    - If there are no relevant information then RETURN a print an empty string and nothing else.
    - Please generate the code without using ``` or any other code formatting symbols. Return only plain text.

---

### expand_ontology
- packaging: txt (`.prompt` + `string.Template`)
- path: `memonto/prompts/expand_ontology.prompt`
- purpose: graph_extract
- call_sites: [`memonto/core/retain.py:expand_ontology`]
- call_order: 1 in write flow (when `auto_expand=True`)
- inputs: `ontology`, `user_message`
- output_schema: plain-text Python mutating graph `ontology`
- hot_path: yes (gated by `auto_expand`)
- full_text: |
    Given the following RDF graph that defines our current ontology and namespaces:
    ```
    ${ontology}
    ```

    And the following user message:
    ```
    ${user_message}
    ```

    Analyze the user message to find relevant information that ARE NOT ALREADY represented by the above ontology BUT SHOULD then generate Python code while adhering to these rules:
    - First identify IMPORTANT information from the user message that DOES NOT MAP to the existing ontology.
    - Second filter out the ones that are either TOO SPECIFIC or TOO SIMILAR to the existing ontology. Focus on quality rather than quantity, only having a few additions is fine.
    - Then create the script that will add them to graph `ontology` and which namespaces they best fit under.
    - NEVER generate code that initializes rdflib graph, namespaces, classes, properties, etc.
    - GENERATE Python code to expand our original ontology which is stored in a graph called `ontology`.
    - If there are no relevant information then RETURN a print an empty string and nothing else.
    - Your response should include all necessary rdflib and rdflib.namespace imports.
    - Please generate the code without using ``` or any other code formatting symbols. Return only plain text.

---

### update_memory
- packaging: txt (`.prompt` + `string.Template`)
- path: `memonto/prompts/update_memory.prompt`
- purpose: update_patch
- call_sites: [`memonto/core/retain.py:update_memory` (ephemeral branch), `memonto/core/retain.py:update_memory` (persistent branch)]
- call_order: 2 in write flow (when `auto_update=True`)
- inputs: `existing_memory`, `ontology`, `user_message`
- output_schema: stringified list/dict of triples parseable by `ast.literal_eval` (ephemeral: list of `{s,p,o}`; persistent: id→`{triple:...}` shape expected by `find_updated_triples`)
- hot_path: yes (gated by `auto_update`)
- full_text: |
    Given these existing triples in our graph `data`:
    ```
    ${existing_memory}
    ```

    and this RDF graph that defines our desired ontology and namespaces:
    ```
    ${ontology}
    ```

    And this user message:
    ```
    ${user_message}
    ```

    Analyze the user message to find information that updates the existing triples while adhering to these rules:
    - First find all the information in the user message that updates existing information in the graph `data`.
    - Second replace the values of existing triples with the updated information without modifying anything else.
    - Finally output the dictionary of existing triples as a string that can be converted back to a list.
    - IGNORE any new information that does not update the existing triples.
    - NEVER add any new triples to the existing triples.
    - If there are no information to update then return the existing triples as they are.
    - Please return only the string of the dictionary without using ``` or any other code formatting symbols. Return only plain text.

---

### summarize_memory
- packaging: txt (`.prompt` + `string.Template`)
- path: `memonto/prompts/summarize_memory.prompt`
- purpose: retrieve_answer
- call_sites: [`memonto/core/recall.py:_recall`]
- call_order: 2 in read/recall flow (after graph gather)
- inputs: `context`, `memory`
- output_schema: one English paragraph (or “no stored memory”)
- hot_path: yes
- full_text: |
    You are trying to describe the following RDF graph in plain English.

    Here is the user message which serves as context:
    ```
    ${context}
    ```

    And here is the RDF graph that contains information relvant to the context:
    ```
    ${memory}
    ```

    Summarize the user message and the RDF graph in one paragraph while following these rules:
    - FOCUS on the telling a story about the who, what, where, how, etc.
    - LEAVE OUT anything not explicitly defined, do not make assumptions.
    - DO NOT mention the RDF graph schema and DO NOT mention the RDF graph at all.
    - If the RDF graph is empty then just return that there are currently no stored memory.
    - Make sure to use plain and simple English.

---

### chat
- packaging: txt (`.prompt` + `string.Template`)
- path: `memonto/prompts/chat.prompt`
- purpose: other
- call_sites: []
- call_order: n/a
- inputs: `memory`, `user_message`
- output_schema: free-form assistant reply
- hot_path: dead
- full_text: |
    Here are some relevant information that was collected over time about the user:
    ```
    ${memory}
    ```

    Respond to this user request keeping in ming the above information:
    ```
    ${user_message}
    ```

---

### bisect_memory_type
- packaging: txt (`.prompt` + `string.Template`)
- path: `memonto/prompts/bisect_memory_type.prompt`
- purpose: write_classify
- call_sites: []
- call_order: n/a
- inputs: `user_message`
- output_schema: enum token `PROCEDURAL` | `FACTUAL` | `NONE`
- hot_path: dead
- full_text: |
    Given the following user message:
    ```
    ${user_message}
    ```

    If you were asked to store that as a memory to be used later, is this more a procedural memory or is it more a factual memory or is it neither.

    Factual Knowledge Characteristics:
    - Static Information: Factual knowledge presents descriptions, properties, relationships, or attributes of entities or things.
    - Is/Has Statements: Facts often involve assertions about what something is, has, or belongs to.
    - Non-sequential: There’s typically no indication of time, steps, or sequence.
    - Examples of Clues:
        - Statements using “is,” “has,” “was,” “belongs to,” etc.
        - Descriptions of states, properties, relationships, or classifications.
    - Examples of Factual Text:
        - “John lives at 123 Main Street.”
        - “Water boils at 100°C.”
        - “Dogs are mammals.”

    Procedural Knowledge Characteristics:
    - Dynamic Information: Procedural knowledge involves steps, actions, sequences, or instructions that are meant to be followed in a specific order.
    - Imperative or Sequential Language: Procedures often use verbs that suggest an action, such as “do,” “perform,” “add,” “bake,” “next,” “then”, etc.
    - Temporal or Conditional: Procedures often involve a sequence of actions or events, sometimes with conditional steps (e.g., “if X, then Y”).
    - Examples of Clues:
        - Action verbs like “mix,” “bake,” “turn on,” “proceed,” etc.
        - Words that suggest order or sequence, such as “first,” “next,” “then,” “finally”.
        - Conditional phrases, such as “if,” “when,” “after,” “before.”
    - Examples of Procedural Text:
        - “First, mix flour and sugar. Then, add eggs and stir well.”
        - “If the machine beeps, press the reset button.”
        - “After boiling, reduce heat to a simmer.”

    Give me either PROCEDURAL, FACTUAL, or NONE as an answer ONLY with no extra words.

---


## Memori

**Staging:** `docs/survey/_staging/Memori/PROMPTS.md`

# Memori — Pass #5 prompts (SHA `56600c52`)

Local tree ships **one** hot-path prompt template (recall injection). Fact extraction / summarization / agent compaction prompts live in **Memori Cloud** and are not present as YAML/Jinja/constants in this repo.

---

### recall_inject_memori_context
- packaging: python_const|inline
- path: `memori/llm/pipelines/recall_injection.py:inject_recalled_facts`
- purpose: retrieve_answer
- call_sites: [`memori/llm/invoke/invoke.py:Invoke.invoke`, `InvokeAsync.invoke`, `InvokeAsyncIterator.invoke`, `InvokeAsyncStream.invoke`, `InvokeStream.invoke`]
- call_order: 1 in read/pre-LLM flow (before `inject_conversation_messages`, before provider call)
- inputs: `relevant_facts` (list of FactSearchResult|Mapping|str), optional per-fact `summaries` (conversation summaries via mentions), `date_created` timestamps
- output_schema: string appended/inserted as system / `system_instruction` / Anthropic `system` / Google system instruction / Responses API `instructions`
- hot_path: yes
- full_text: |
    <memori_context>
    Only use the relevant context if it is relevant to the user's query. Relevant context about the user:
    - {fact_content}. Stated at {YYYY-MM-DD HH:MM}
    ...
    
    ## Summaries
    
    - [{ts}]
      {summary_content}
    ...
    </memori_context>

    Assembly rules (verbatim fragments from code):
    - Wrapper open: `"\n\n<memori_context>\n"`
    - Guard sentence: `"Only use the relevant context if it is relevant to the user's query. "`
    - Body header: `"Relevant context about the user:\n"` + lines from `format_recalled_fact_lines`
    - Fact line: `f"- {content}"` or `f"- {content}. Stated at {ts}"`
    - Optional: `"\n\n## Summaries\n\n"` + lines from `format_recalled_summary_lines`
    - Summary line: `f"- [{ts}]\n  {content}"` or `f"- {content}"`
    - Wrapper close: `"\n</memori_context>"`

---

### cloud_aa_extract_structure
- packaging: opaque (server-side; not in repo)
- path: Memori Cloud `POST sdk/augmentation` / `POST cloud/augmentation` / collector `agent/augmentation` — client builders: `memori/memory/augmentation/augmentations/memori/_augmentation.py:AdvancedAugmentation._build_api_payload`, `core/src/augmentation/pipeline.rs:build_payload`, `memori/memory/augmentation/_handler.py:_post_cloud_augmentation`, `memori/agent.py:Agent.capture_turn`
- purpose: write_extract
- call_sites: [`AdvancedAugmentation.process`, `run_advanced_augmentation`, `handle_augmentation`, `Agent.capture_turn`]
- call_order: 1 in background write/augment flow (after conversation persist)
- inputs: conversation `messages` (+ optional prior `summary`), `meta` (attribution hashes, llm/framework/platform/sdk/storage dialect). Note: `system_prompt` is accepted on `AugmentationInput` but **not** included in the API payload (`tests/memory/augmentation/test_advanced_augmentation.py` asserts `"system_prompt" not in payload["conversation"]`).
- output_schema: JSON roughly `{entity: {facts[], triples|semantic_triples[], fact_embeddings?}, process: {attributes[]}, conversation: {summary?}}` → `memori/memory/_struct.py:Memories`
- hot_path: reserved (async background; required for structured memory)
- full_text: |
    <UNAVAILABLE — air-gapped; prompts execute inside Memori Cloud Advanced Augmentation service. Local SDK only serializes conversation + meta.>

---

### cloud_agent_compaction
- packaging: opaque (server-side)
- path: `GET agent/compaction` via `memori/agent.py:Agent.compaction`
- purpose: compress
- call_sites: [`memori/__init__.py:Memori.agent_compaction`]
- call_order: on-demand (agent integrations)
- inputs: `project_id`, optional `session_id`, `num_messages`
- output_schema: opaque `dict` from Cloud
- hot_path: reserved
- full_text: |
    <UNAVAILABLE — Memori Cloud agent compaction prompts not shipped in SDK.>

---

### cloud_agent_recall_summary
- packaging: opaque (server-side)
- path: `GET agent/recall/summary` via `memori/agent.py:Agent.recall_summary`
- purpose: summarize
- call_sites: [`memori/__init__.py:Memori.agent_recall_summary`]
- call_order: on-demand
- inputs: date range, `project_id`, `session_id`
- output_schema: opaque `dict` (tests expect `summaries` key)
- hot_path: reserved
- full_text: |
    <UNAVAILABLE — Memori Cloud agent recall-summary prompts not shipped in SDK.>

---

## Prompt inventory notes

- No `prompts/`, `*.yaml` prompt packs, Jinja templates, or instructor schemas for extraction in this tree.
- User/system messages from the host app are **data**, not Memori prompts; they are persisted with `role != system` stripped before cloud/BYODB message write (`Manager.execute` strips system roles).
- Recall injection is the only client-authored instructional text Memori adds to the host LLM call.


<!-- REPO_SLOT:MemoryBear -->

<!-- REPO_SLOT:memory-opensource -->

<!-- REPO_SLOT:MemOS -->

<!-- REPO_SLOT:memU -->

<!-- REPO_SLOT:OpenMemory -->

<!-- REPO_SLOT:powermem -->

<!-- REPO_SLOT:ReMe -->

<!-- REPO_SLOT:Second-Me -->

<!-- REPO_SLOT:SimpleMem -->

<!-- REPO_SLOT:telemem -->

<!-- END_OF_CATALOG -->
