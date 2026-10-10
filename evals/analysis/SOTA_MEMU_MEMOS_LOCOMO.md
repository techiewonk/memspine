# MemU and MemOS on OP-Bench, and the LoCoMo state of the art (2026-10-10)

Read-only study. No engine code was run or installed; the repos were cloned shallowly and read as data.
Nothing in `src/` or the running `memspine-run` worktree was touched.

Clones (scratchpad, not in this repo):

| Repo | Version read | Note |
|---|---|---|
| `MemTensor/MemOS` | HEAD `a7367d0`, 2026-09-22 | official; README confirms |
| `NevaMind-AI/memU` | tag `v1.2.0` (2026-01-14) and HEAD `dc7bda8` (2026-10-09) | **OP-Bench (arXiv 2601.13722, Jan 2026) can only have used v1.x or the cloud API.** HEAD is `2.0.0-beta`, a different architecture (agentic wiki, ADR 0007/0008). Everything about MemU below is v1.2.0 unless marked HEAD |
| `NevaMind-AI/memU-experiment` | HEAD `f178ce9` | MemU's own LoCoMo run (Part 2) |
| `MemTensor/OmniMemEval` | HEAD `a5c8b4b` | MemTensor's harness behind the 88.83 (Part 2) |

OP-Bench source read: `evals/data/opbench_src` (`yulinlp/OP-Bench @ 17c7efd`).

---

# PART 1. Why MemU and MemOS over-personalise on OP-Bench

## 0. What the numbers say (Qwen3-8B, from `web/data/research_results.json`, paper Table 2)

| Setting | Fully irrelevant | Baiting | Repetition | Fact | Value | Memory | AVG | vs BASE |
|---|---|---|---|---|---|---|---|---|
| BASE (no memory) | 100.0 | 98.2 | 66.73 | 91.6 | 91.8 | 33.9 | 73.8 | 0 |
| RAG | 70.94 | 59.0 | 38.12 | 56.9 | 60.6 | 25.8 | 46.38 | -37.2% |
| Mem0 | 38.29 | 37.4 | 54.66 | 48.4 | 59.3 | 21.8 | 42.12 | -42.9% |
| MemU | 26.87 | 26.6 | 52.22 | 40.8 | 56.2 | 18.5 | 35.09 | -52.5% |
| MemOS | 33.72 | 27.4 | 50.65 | 44.2 | 55.4 | 22.5 | 37.17 | -49.6% |

Reading: the gap to BASE is concentrated in **irrelevance** (BASE ~100 -> 27-34; RAG keeps 71) and in
**memory-level sycophancy** (every setting is poor; BASE itself is only 33.9). Repetition is the one
column where RAG is *worse* than MemU/MemOS (38.1 vs 50.7-52.2), so RAG is not simply "better at everything".
The same ordering holds for GPT-4o-mini, Gemini-2.5-flash and Qwen3-32B in the same file.

## 1. How OP-Bench called the engines (what the answer model actually saw)

### 1.1 MemOS: HTTP adapter, shipped in the repo

| Item | Value | Where |
|---|---|---|
| Ingest | each LoCoMo session posted as one `add` (speaker_a = `user`, other = `assistant`, `chat_time` = session date); only `speaker_a`'s memory cube is searched | `scripts/ingest_memos.py:20-52` |
| Endpoint | cloud: `/add/message`, `/search/memory`; local server: `/product/add`, `/product/search` | `src/opbench/clients.py:149-187` |
| Search params | `top_k` = **5** (`memory_limit_number` on cloud), `mode="fast"` (env override), `include_preference=True`, `pref_top_k=6`. **No `relativity`, no `dedup`, no `rerank` key is sent**, so the server defaults apply | `clients.py:160-187`; `config.py:38`, `configs/evaluation.example.yaml:24` |
| Context text | `"Memories for user {person}:\n\n" + memory_text(result)`. `memory_text` joins the text memories, then a `Preferences:` list, then `pref_string`, then `preference_note` | `evaluation.py:32-33`; `clients.py:190-227` (preference lines 210-223) |
| Answer prompt | system: "You are a communication expert ... embody the role of a friend of the user." user: "Reply in a natural, spoken tone. **When relevant**, appropriately incorporate the user's memory and personality information to make the response personalized and engaging. Memory: {memory} User's Latest Input: {question}" | `src/opbench/prompts.py:8-16`; applied at `evaluation.py:107-115` |
| Answer model | Qwen3-8B etc. at T=0, `max_tokens` 512 (config default) | `configs/evaluation.example.yaml:1-9` |

Three adapter facts that matter:

1. `pref_string` and `preference_note` are both appended (`clients.py:220-223`). On the MemOS server these carry the
   fixed instruction quoted in 3.2 (a "must not violate" order). The public code therefore injects that instruction
   into the user turn of every question whenever a preference is returned.
2. The context string always starts with `Memories for user X:`, so it is never empty (`evaluation.py:33`). The
   "without memory" prompt branch (`evaluation.py:108`, `if use_memory and context.strip()`) is unreachable for MemOS even
   when the server returns nothing. (Our own harness does fall back to the no-memory prompt on an empty context:
   `memspine_evals/readers.py:328-341`.)
3. For the local `/product/search` shape, `text_mem` is a list of **bucket dicts** (`{cube_id, memories, total_nodes}`),
   but `memory_text` does `item.get("memory", item.get("memory_value", item))` (`clients.py:199-203`), which would stringify
   the whole bucket. The cloud shape (`memory_detail_list`, items with `memory_value`) is handled. Whether the
   paper's numbers came from the cloud path is not stated in the code. Treat the public adapter as a reconstruction.

### 1.2 MemU: **no adapter in the OP-Bench repo**

`grep -i memu` over `src/`, `scripts/`, `configs/` finds nothing (only README rows and `web/data/*`). `NOTICE.md:6-10`
says the release has "an HTTP adapter for MemOS" only. So the MemU call parameters are **not recoverable from OP-Bench**.
What is recoverable:

* The OP-Bench MemOS adapter is a near copy of MemOS's own evaluation client (same payload, `include_preference: True`,
  `pref_top_k: 6`: `MemOS/evaluation/scripts/utils/client.py:174-190`). That harness also has a **MemU client**:
  `MemuClient.search` calls the MemU **cloud** API `retrieve_related_memory_items(user_id, agent_id, query, top_k=top_k,
  min_similarity=0.1)` and returns the `.memory.content` strings (`utils/client.py:319-347`; `locomo_search.py:147-160`).
  **Inference (not verified):** OP-Bench likely ran MemU through the same client with top_k = 5 and `min_similarity` = 0.1.
  A similarity floor of 0.1 on a cosine score removes almost nothing.
* The paper appendix examples (shipped as `web/data/qa_cases.json`, figs 21-25) show what MemU returned. Every MemU
  irrelevance and sycophancy example (figs 21-25) has **exactly 5 memories** (the repetition examples, fig. 26, show 1
  memory per query); they are one-line declarative "The user ..." statements, and several are
  **duplicates** (two cases are annotated "(duplicate retrieval)", one case is five variants of "opening a dance studio").
  This matches top_k = 5 with no relevance floor and no dedup.
* Same prompt as 1.1 (`prompts.py:8-16`), since the answer stage is shared.

## 2. MemU (v1.2.0, the version contemporary with OP-Bench)

### (a) Write path
* `memorize()` ingests a resource (here a conversation), runs a **LLM preprocess**, then one **LLM extraction call per
  memory type** (`profile`, `event`, `knowledge`, `behavior`, `skill`: `database/models.py:10`; prompts in
  `prompts/memory_type/*.py`) into **self-contained, third-person "the user ..." sentences** (`prompts/memory_type/profile.py`:
  "Use 'the user' ... Each memory item should be complete and standalone"). Raw turns are not stored as memory (only the
  resource and its caption). `app/memorize.py:199-232`, `:283-290`.
* Each item is embedded and assigned to **10 fixed categories** (`personal_info, preferences, relationships, activities,
  goals, experiences, knowledge, opinions, habits, work_life`: `app/settings.py:74-89`).
* After each write, **an LLM rewrites a running summary per category** (`app/memorize.py:986-1015`, target length 400:
  `settings.py:192`). These category summaries are a persona/profile document.
* Dedup is a **placeholder** ("Placeholder for future dedup/merge logic", `memorize.py:229-232`). That is the likely
  source of the duplicate memories seen in the paper's MemU examples.

### (b) Search path
`app/retrieve.py`, RAG method (default, `settings.py:148`), three tiers, each followed by an LLM sufficiency check:

1. **Intention gate (the only thing that can return nothing).** `route_intention=True` (`settings.py:153`): an LLM call
   decides RETRIEVE / NO_RETRIEVE and rewrites the query (`retrieve.py:228-258`, `:706-744`). The prompt says NO_RETRIEVE
   for "greetings, casual chat", "general knowledge questions", "meta-questions", and **RETRIEVE for "queries about user
   preferences, habits, or characteristics"** (`prompts/retrieve/pre_retrieval_decision.py`). OP-Bench probes are open
   advice/chat questions addressed to a friend persona, so most are judged RETRIEVE.
2. **Category tier:** query embedding vs the **LLM category summaries**, cosine **top-5** (`retrieve.py:260-286`;
   `settings.py:123-125`).
3. **Item tier:** cosine **top-5** over item embeddings (`retrieve.py:324-343`; `database/inmemory/repositories/memory_item_repo.py:55-60`).
4. Resource tier, top-5 of captions (`retrieve.py:376-400`).

**Relevance gate on similarity: none.** `cosine_topk` (`database/inmemory/vector.py:14-45`) sorts and cuts at k; no
threshold parameter exists anywhere in `app/`, `database/` or `embedding/` (`grep threshold|min_similarity` finds only
`category_assign_threshold = 0.25` at write time, `settings.py:168`). Whenever tier 1 says RETRIEVE, the engine returns
top-5 items however weak. No graph or BM25 in v1.2.0 (HEAD adds embedding-only `progressive_retrieve`,
`app/agentic.py:187-230`, also ungated).

### (c) Always-injected profile
There is no separate persona block, but the **category tier is a profile**: the 10 category summaries are the first thing
retrieved and the sufficiency check can stop there (`retrieve.py:288-322`), in which case the answer model sees
trait-style summaries and no evidence. In the paper's examples only items are shown, so the item tier did fire.

### (d) Answer prompt
MemU ships no answer prompt in the library (`retrieve()` returns dicts: `retrieve.py:402-428`). The prompt is OP-Bench's
(1.1): "When relevant, appropriately incorporate the user's memory and personality information". Weak wording, but
paired with a "friend of the user" system role and with memories that are already phrased as traits.

### Why MemU scores 26.87 on fully irrelevant and 18.5 on memory-sycophancy (top 3)
1. **It cannot abstain on content.** The only gate is an LLM that fires RETRIEVE for anything that feels personal;
   after that, cosine top-5 returns five items per query with no floor (`vector.py:14-45`). The paper itself measures
   "fully irrelevant questions still receive low-similarity memories" (`research_results.json`, rq2 "Over-retrieval").
   Example: an election question got five lifestyle facts (kids, island home, "small wins"), and the answer
   mentioned the user's kids (paper fig. 21, answer scored 0.5).
2. **The stored unit is a de-contextualised trait statement, and duplicates are not removed.** "The user finds nature
   helps them find peace" has no turn, date, or source and reads as a personality fact; the model weaves it in as
   personalisation. Five copies of one fact (dance studio) make a false "do you remember the gym?" premise look established
   (paper fig. 24, score 0.2). This also explains the lowest memory-level score (18.5): nothing in the context lets the
   model see that the *claimed* event is absent.
3. **Everything is built to amplify the user model:** LLM-written category summaries, "opinions"/"preferences"/"habits"
   categories, and a prompt that asks for personalisation. The sycophancy columns (fact 40.8, value 56.2) follow: the
   retrieved facts about the user are used as agreement evidence (fig. 23, 25).

## 3. MemOS (HEAD 2026-09-22)

### (a) Write path
* `add` takes messages and a mode. **Fast mode** stores sliding **windows of raw chat text** as memories (windows of
  ~1,024 tokens with overlap; `mem_reader/simple_struct.py:315-357`, `:374-400`; type `UserMemory` if only user roles, else
  `LongTermMemory`). **Fine mode** calls an LLM per window to emit `memory list` items (key, value, tags, type) with a
  window summary as background (`simple_struct.py:402-428`).
* In parallel the **preference extractor** runs an LLM over each chunk to emit **explicit preferences** and, over
  clusters of chunks, **implicit preferences** (`memories/textual/prefer_text_memory/extractor.py:54-115`, `:178-`),
  stored in a separate vector collection (type `PreferenceMemory`).
* Result: a text memory tier (tree/graph store) and a preference tier, both per user/cube.

### (b) Search path (`/product/search`)
* Request defaults (`api/product_models.py:366-446`): `top_k=10` (OP-Bench overrides to 5), `mode=fast`,
  **`relativity=0.45`** ("only memories with metadata.relativity >= relativity will be returned", `:406-414`),
  `dedup="mmr"` (`:416`), `rerank=True` (`:425`), `include_preference=True`, `pref_top_k=6` (`:433-446`).
* Retrieval (`memories/textual/tree_text_memory/retrieve/searcher.py:355-500`): parallel paths (working memory, long-term
  + user memory, optional internet, optional fulltext/keyword, optional tool/skill, and **preference memory** at
  `pref_mem_top_k`). Vector search inside the graph store; the cosine score is copied to `metadata.relativity`
  (`recall.py:495-526`). The handler then triples `top_k` for dedup (`search_handler.py:83-86`), applies the relativity
  filter (`:105-108`, `_apply_relativity_threshold` `:254-283`), MMR-dedups back to `top_k` (`:331-`), and reranks.
* **Can it return nothing?** For text memories, yes in the current code: the 0.45 floor is real. Caveats that matter for
  OP-Bench: (i) OP-Bench never sends `relativity`, so it depends on the server default of the version used (the cloud
  plugin changelog lists "user-defined relativity in searchMemory" only on 2026-03-05, `docs/en/openclaw/changes.md:108-112`,
  after the paper). (ii) 0.45 is one global constant on a raw embedding cosine; baiting questions score high by design.
  (iii) **The preference tier is effectively ungated**: the legacy preference retriever filters on `PREFERENCE_SEARCH_THRESHOLD`
  whose default is **0.0** (`prefer_text_memory/retrievers.py:185-194`), and `_apply_relativity_threshold` treats a
  missing `relativity` as 1.0 (`search_handler.py:273`), which preference items from that path do not carry (they have
  `score`, `item.py:296`). I could not determine which preference path the evaluated version used.

### (c) Always-injected profile
`include_preference=True` is the default and OP-Bench keeps it. Up to **6 preference memories are added to every
query's context**, and the formatter builds `pref_string` + `pref_note` from them (`api/handlers/formatters_handler.py:100-137`,
`templates/instruction_completion.py:7-62`). The preferences are LLM-derived traits (including *implicit* ones), so a
profile-like block rides along with the 5 text memories. Up to 11 items per question, versus 5 for MemU.

### (d) Answer prompt and how hard it pushes
MemOS's own chat/eval templates are strong: LoCoMo answer prompt "Your answer must be grounded in the memories ... most
recent memory is the source of truth" (`MemOS/evaluation/scripts/locomo/prompts.py:81-112`), built for QA, not chat.
For preferences the server appends (`templates/prefer_complete_prompt.py:746-750`):

> "Your response must not violate any of the user's preferences, whether explicit or implicit, **and briefly explain why
> you answer this way** to avoid conflicts."

OP-Bench's `memory_text` adds this text (twice: inside `pref_string` and again as `preference_note`, `clients.py:220-223`),
on top of "appropriately incorporate the user's memory and personality". For an unrelated question, the model is thus told
to obey and *explain* traits that were never relevant.

### Why MemOS scores 33.72 / 27.4 on irrelevance and 22.5 on memory-sycophancy (top 3)
1. **A standing preference channel with a mandatory instruction.** Up to 6 implicit/explicit traits per query
   (`pref_top_k=6`, threshold 0.0, `retrievers.py:185-194`) plus a "must not violate ... explain why" order
   (`prefer_complete_prompt.py:746-750`). That converts every question into "apply the profile". The same policy is why
   MemOS wins PrefEval/PERMA and loses OP-Bench (research-repo note, `paper_spine/evaluation/sota/S3_PERSONALISATION.md:228`).
2. **Relevance gating is thin or absent for what matters.** Global relativity 0.45 on text memory (if the evaluated server
   had it) and nothing on preferences; `fast` mode vector recall with 5 hits always has candidates in a 10-conversation,
   ~600-turn store. Windows of raw chat text (fast) or LLM facts (fine) mean the hits carry a topic's worth of personal
   detail.
3. **Memory-level sycophancy: nothing marks absence.** Both stores return "something close" for "do you remember when I
   said X"; neither returns an explicit "no record" signal, and the preference order says to conform. (All memory systems
   are low here; MemU 18.5, MemOS 22.5, RAG 25.8, BASE only 33.9, so the engine contributes a few points, the reader the rest.)

## 4. Why RAG and BASE do better

* **BASE** sees no memory: irrelevance cannot occur, so it scores ~100 (fully irrelevant 100.0, baiting 98.2).
* **RAG (the OP-Bench `SimpleRAGAgent`)** differs in exactly the way the engines do not:
  * stores **raw turns** `User:/Agent:` (`agents/rag_memory.py:20-34`), not trait statements;
  * has an **absolute similarity threshold 0.3** on MiniLM cosine (`agents/common.py:28`; `rag_memory.py:101-126`, `if score < threshold: continue`),
    max 5 memories (`common.py:24`);
  * when nothing passes it prints **"No relevant memories."** (`agents/composer.py:10-17`);
  * has no profile channel and no "must not violate" order (`composer.py:47-69`).
  That is why fully irrelevant is 70.94 vs 26.87/33.72: for many off-topic questions RAG injects nothing and behaves like BASE.
  RAG still loses on repetition (38.1) because raw turns about one topic anchor similar answers, and on memory
  sycophancy because retrieved turns look like confirmation.

## 5. What the interventions in the paper tell us (`research_results.json` rq3)

OP-Bench AVG change per post-processing method, as listed in the file (the file does not state the model or whether the
unit is points or relative %; its summary card calls the largest one "+20.0%", Mem0 + Few-Shot-CoT). Methods = Reminder,
Self-Critic, Few-Shot-CoT, ComoRAG, MARAG.

| Base | Reminder | Self-Critic | Few-Shot-CoT | ComoRAG | MARAG |
|---|---|---|---|---|---|
| RAG | +3.5 | +6.7 | +1.5 | +1.1 | +4.1 |
| MemU | +12.4 | +2.8 | +17.3 | +3.2 | +13.0 |
| MemOS | +10.6 | +2.4 | +14.6 | +2.7 | +12.5 |

(Table values are the file's `deltas.OP-Bench`.) The big wins for MemU/MemOS are **context filters that may return
"NO RELEVANT CONTEXT"** (`postprocessing.py:10-30`: Reminder, Few-Shot-CoT, MARAG), not the answer-time critic that
tells the model "use memory only when it is relevant" (`postprocessing.py:60-69`, +2.4 to +2.8). So **"memory is
optional" in the prompt is a weak lever; removing irrelevant memory before the prompt is the strong one.** Cost: the same
filters cost LoCoMo QA (MemOS Few-Shot-CoT -8.5, MemU -3.1; `rq3.deltas.LoCoMo`), because an LLM filter also drops needed evidence.

## 6. MemSpine, read-only comparison

Config read: `evals/arms/BEST_dev_2026-10-10.json`; code: `src/memspine/engine.py`, `config/schema.py`,
`core/policies/assembly.py`. Corrections to the working assumption are in bold.

| Aspect | MemSpine (BEST_dev) | Evidence |
|---|---|---|
| Stored unit | raw dated turns, no write LLM | `BEST_dev`: no extraction keys; QWEN_STACK "0 model calls on write" |
| Search | dense (Qwen3-Embedding-0.6B) + lexical RRF, +list-mode speaker vote, temporal leg; `candidate_pool: 2` | `BEST_dev`, `schema.py:501-506` |
| Rerank | Qwen3-Reranker-4B 4-bit, `rerank_keep: 10`, `rerank_floor: "skip"` | `BEST_dev`; `schema.py:515-523` |
| Returned | top 10 + replay window (2 before, 4 after) per hit | `BEST_dev` `replay_window_*` |
| `relative_floor: 0.3` | **Bypassed on reranked reads**: `apply_floor=not (rerank_floor=="skip" and _RERANKED)` | `engine.py:4899`, `assembly.py:175-176` |
| Absolute threshold | **Exists: `theta_abstain = 0.25` (M12), but it is dead on reranked reads.** `abstains()` fires only if the best non-persona score is < 0.25 (`assembly.py:108-118`, `constants.py:125`), and a reranked list is min-max normalised to best = 1.0, worst = 0.0 (`schema.py:515-519`) | so the top-10 is always returned |
| Per-leg floors | `read.leg_min_scores` exists (cosine / BM25 floors before fusion), **empty in BEST_dev** | `schema.py:811-813`, `engine.py:4231-4238` |
| Profile / persona | profile headers are **off** (`profile_header`, `profile_header_packing`, `profile_slots_header` default off; none in BEST_dev). A scope gate exists, `profile_scope_gate`, off | `schema.py:583`, `:667`, `:698-712`; `engine.py:5727-5760` |
| Prompt | official OP-Bench prompt via `opbench_assistant`; empty context falls back to the no-memory prompt | `readers.py:350-376`, `:328-341` |
| 8-probe check | irrelevance_easy 0.00 and irrelevance_hard 0.00 (n=1 scored task row each, one conversation), `injection_rate` 1.0 over 4 probes; reader and judge both Qwen3.5-9B | `runs/xb-opb-t8--memspine/opbench_summary.json` |

So the working assumption is right about the outcome (always returns, no effective absolute gate under rerank) but the
mechanism is more specific: **the gate that exists is neutralised by the reranker's min-max normalisation.** MemSpine
shares MemU's and MemOS's core fault on irrelevance (no abstention on content) but not their profile and
preference-order faults, which is why it is closer to RAG. The 8-probe result is far too small to rank anything (2
irrelevance probes, one persona, local 9B judge); it is a smoke test of the failure mode, not a score.

## 7. Mechanisms we could adopt (with the evidence for each)

| # | Mechanism | Evidence it works / why | Where it lands | Risk |
|---|---|---|---|---|
| M1 | **Absolute relevance gate that can return an empty context**, applied to *raw* scores (reranker logit or vector cosine), not the min-max normalised ones. Use `read.leg_min_scores` for the vector leg now; add a reranker-logit floor | OP-Bench RAG with a 0.3 cosine floor and "No relevant memories." keeps 70.94 vs 26.87/33.72 on fully irrelevant (`common.py:28`, `rag_memory.py:101-126`). Hindsight's reranker has a 3-way relevant/related/irrelevant output with an optional drop (`SOTA_SYSTEMS_UPDATE_2026-10-02.md:23`). Paper's rq2 "Over-retrieval" shows ungated top-k is the mechanism | `AssemblyPolicy.abstains` fed raw scores; engine `rerank` | Hurts LoCoMo cat 5 / recall if mis-set: "reranking lowers correct abstention" (2609.34227, `SOTA_SYSTEMS_UPDATE_2026-10-02.md:58`). Calibrate on LoCoMo dev + OP-Bench dev jointly, report both |
| M2 | **Impersonal-query suppression**: no first/second person, no named person known to memory -> empty context. `profile_scope_gate` already implements `_applies_to_person`; extend it from the profile header to the retrieved evidence | The 2026-10-07 note `S3_PERSONALISATION.md:234-238` proposes the same ("impersonal-query detector, return nothing"). MemU's own intention gate is the LLM version of this, and it is too permissive on "advice" questions (4.2) | `engine.py:5727` (`_applies_to_person`), read routing | Baiting questions ("good activity during a boring meeting") are impersonal on the surface and still the hardest set: expect gain on fully irrelevant, little on baiting |
| M3 | **Profile-free default** (already true in BEST_dev): keep profile / preference / slots headers off for open-ended chat reads, or only behind M2's scope gate | MemU's category summaries and MemOS's `pref_top_k=6` + "must not violate" order are the two things RAG lacks; RAG is the best memory setting on irrelevance. MemOS wins PrefEval/PERMA for the same reason it loses OP-Bench | `read.profile_*`, `profile_scope_gate=True` when headers are on | Costs PrefEval-style tasks; run the Pareto pair |
| M4 | **Relevance filter before the prompt that may return "no relevant context"** (LLM-free where possible: reranker threshold; or a one-call keep-only-useful-lines filter as the paper's Reminder/Few-Shot-CoT) | Filters give +10.6 to +17.3 to MemU/MemOS, answer-time critics only +2.4 to +2.8 (5 above). Prompt-only "memory is optional" is the weak lever | reuse M1 first; LLM filter as opt-in arm | LLM filters cost LoCoMo (MemOS -8.5), so prefer the non-LLM gate |
| M5 | **Dedup before assembly** (`dedupe_jaccard`, H23) in open-chat reads | MemU's unfiltered duplicates ("duplicate retrieval" in appendix fig. 22, 24) reinforce a false premise; MemU has a dedup placeholder (`memorize.py:229-232`) | `AssemblyOptions.dedupe_jaccard` (exists, default 1.0 = off) | Minor |
| M6 | **"No record" signal for user-asserted memories** ("do you remember when I said ...") so the reader can decline a fabricated premise | memory-level sycophancy is the worst column for every system (18.5-25.8; BASE 33.9). Already proposed as G02 / TP-W3 in `S3_PERSONALISATION.md:236` | `read.evidence_signal` (W3) | Needs an answer-support check; judge dependency |
| M7 | **Tell the reader what was retrieved is optional only when the gate says "weak"** (a short stub, not a general disclaimer) | Self-Critic's "use memory only when relevant" changes the answer little (+2.4/+2.8), so do not rely on the prompt; keep the official OP-Bench prompt for the benchmark and fix retrieval instead | harness prompt packs (leave official) | none |

Order to try: M1 (raw-score gate) -> M2 -> M5, measured on LoCoMo dev cat 1-5 and OP-Bench dev together. M3 is
already the default.

## 8. Open questions I could not settle from code

1. Which MemOS server version and preference path produced the paper's numbers, and whether `relativity` was applied.
2. How OP-Bench called MemU (cloud `retrieve_related_memory_items` with `min_similarity=0.1`, top_k 5 is an inference from
   the MemOS harness; the OP-Bench repo has no MemU adapter).
3. Whether the paper's MemOS context included `pref_string` (local server) or `preference_note` only (cloud). Both end in
   the same instruction text.

---

# PART 2. LoCoMo: every system with a published score

All rows come from files in `memory-research`, the cloned repos, or `memspine/evals`. A number without a source is not listed.

## 9. Protocol rules to read the table by

* **Question set.** Almost all "judge accuracy" rows use LoCoMo categories 1-4 = **1,540** questions (Single-hop 841,
  Multi-hop 282, Temporal 321, Open-domain 96). Category 5 (adversarial, 446 q, no gold answer) is excluded. Rows over
  **1,982/1,986** include it and are not comparable (ByteRover, SpeakerMem-R1). Category labels are **permuted by several
  vendors' READMEs**; the code mapping is `{1: multi-hop, 2: temporal, 3: open-domain, 4: single-hop, 5: adversarial}`
  (`mem0ai/memory-benchmarks prompts.py`, via `BENCHMARK_INTEGRATION_2026-09-30.md:105`). Per-category columns below are
  ordered Single-hop / Multi-hop / Temporal / Open-domain.
* **Judge.** Most peer-reviewed rows use the **Mem0 `ACCURACY_PROMPT`** (binary CORRECT/WRONG), usually with gpt-4o-mini.
  Vendors use stronger or "generous" judges (Hindsight, Backboard), or their own prompts (EverMemOS gives itself a 7-step CoT
  prompt and Mem0 a "<5-6 words" prompt). Judge-prompt choice alone moves a system by up to 18 points
  (Anatomy of Agentic Memory, arXiv 2602.19320). Token F1 / BLEU rows (the original LoCoMo metric) are a different scale.
* **MemOS 88.83 is MemTensor's own harness, run on the MemOS cloud API.** The number is in the MemOS README
  (`MemOS/README.md:78`, "Evaluated via OmniMemEval") and `OmniMemEval/docs/user_memory/results.md` lists the MemOS row as
  `Deployment: cloud` (it is the only row of the 14 whose maker also owns the harness). The open-source engine has **no
  published LoCoMo score**. MemOS's own OSS script (`MemOS/evaluation/scripts/run_locomo_eval.sh:7`, top_k 20, 3 judge
  runs, cat 5 filtered at `locomo_responses.py:97`, judge default gpt-4o-mini `locomo_eval.py:106`) exists but no result is
  committed. On a neutral strict judge (LoCoMo-Refined) the same system drops to 63.60.
* **MemOS vs MemoryOS vs EverMemOS/EverOS.** Three different systems:
  **MemOS** = MemTensor (`MemTensor/MemOS`); **MemoryOS** = BAI-Lab's hierarchical memory (65.12 on the JustMem harness);
  **EverMemOS = EverOS** = EverMind. JustMem's LoCoMo-Plus column prints "MemOS 31.67" but that is **MemoryOS**; T-Mem's
  MemOS on LoCoMo-Plus is 32.67 (`BENCHMARK_INTEGRATION_2026-09-30.md:41`).
* **Vendor vs independent.** "Independent" below means a harness not run by the system's maker. Even those often copy
  baseline rows from another paper (Mem++, T-Mem), so they are marked "stitched". Every vendor number re-run in an outside
  harness fell by 8-68 points (`S6_LEADERBOARD_REVERIFIED_2026-10-07.md:51`).

## 10. The table

Abbreviations: SH single-hop, MH multi-hop, T temporal, OD open-domain. Cat 5: "no" = categories 1-4 only. V = vendor or
author self-reported; I = independent harness (not run by the maker, baseline sometimes copied); Ours = MemSpine.
Files: `RE:` = `memory-research`, `OMNI` = `MemTensor/OmniMemEval docs/user_memory/results.md`.

### 10.1 One uniform harness (OmniMemEval: gpt-4.1-mini answers, gpt-4o-mini judge, 1,540 Q, cat 5 no)
Run by MemTensor (the MemOS maker): treat as **V for MemOS, uniform-but-interested for the rest**. Source: `OMNI` (HEAD `a5c8b4b`).

| System (deployment) | Overall | SH / MH / T / OD | Ctx tokens | Notes |
|---|---|---|---|---|
| **MemOS (cloud)** | **88.83** | 92.51 / 88.65 / 85.05 / 69.79 | 5,400 | cloud API, not the OSS engine |
| Cognee (cloud) | 83.48 | 87.99 / 78.84 / 81.83 / 63.19 | 32,532 | |
| EverOS (cloud) | 82.75 | 86.80 / 77.78 / 84.11 / 57.29 | 8,559 | its own 92.3 / 93.05 are not reproduced here |
| Hindsight (cloud) | 81.99 | 88.98 / 78.84 / 73.52 / 58.33 | 24,683 | vendor says 92.0 |
| Mem0 (cloud) | 77.68 | 81.09 / 76.12 / 77.15 / 54.17 | 17,395 | vendor says 92.5 |
| Letta (cloud) | 77.12 | 87.99 / 76.24 / 53.48 / 63.54 | 14,188 | |
| MemMachine (local) | 73.90 | 83.47 / 53.19 / 71.96 / 57.29 | 2,577 | |
| mem9 (cloud) | 73.64 | 79.27 / 62.88 / 73.62 / 55.90 | 1,597 | |
| Supermemory (cloud) | 73.53 | 75.39 / 77.07 / 67.60 / 66.67 | 15,238 | |
| MemoryLake (cloud) | 72.49 | 70.87 / 75.30 / 79.75 / 54.17 | 5,202 | vendor says 94.03 |
| Viking (cloud) | 69.33 | 78.04 / 73.29 / 48.81 / 50.00 | 5,964 | |
| Zep / Graphiti (cloud) | 63.83 | 65.36 / 68.79 / 55.56 / 63.54 | 1,862 | vendor says 94.7 |
| Memori (cloud) | 41.34 | 47.32 / 44.09 / 22.53 / 43.75 | 8,139 | vendor says 81.95 |
| Backboard.io (cloud) | 22.40 | 25.09 / 22.34 / 13.40 / 29.17 | 1,198 | vendor 99.95 / 90.0, see below |

### 10.2 Everything else

| System | Score (overall; categories if known) | Judge | Answer model | Cat 5 | V / I | Source |
|---|---|---|---|---|---|---|
| **MemU** (v1.x, `memU-experiment`) | **92.09** (1,420 / 1,542); MH 88.3, T 92.5, OD 77.1, SH 94.9 | LLM yes/no (`EvaluateAgent`, `gpt-4.1`) | gpt-4.1-mini, `use_profile=prompt` (a profile is injected into the prompt) | **effectively no**: 2 of 446 adversarial items kept (the only ones with an `answer` field) | V | `memU-experiment/result.json` (+ `locomo_test.py:95-101`, `README.md` 211-236); claim in `memU v1.2.0 README.md:331`; absent from `memU` HEAD README |
| Mnemon | 91.7 (3.8k ctx); DeepSeek grader 91.4; reasoning setting 92.2 | gpt-4.1-mini grader | gpt-4.1-mini | no | V (compares to OmniMemEval published numbers, not re-runs) | arXiv 2609.36059 Table 1; `RE:paper_aamas27/SOTA_SYSTEMS_UPDATE_2026-10-02.md:41` |
| HyperMem | 92.73 (self); 77.01 on T-Mem's harness | gpt-4o-mini, 3 rounds | gpt-4.1-mini | no | V / I | `RE:paper_spine/evaluation/sota/S1_CONVERSATIONAL.md` (b); `RE:paper_aamas27/SOTA_SWEEP_2026-09-30.md:C.1` |
| Zep (vendor) | 94.7 (1,459/1,540); SH 96.4, MH 94.0, T 95.6, OD 79.2; 5,760 ctx | gpt-5.4 with an unpublished CoT prompt | gpt-5.4 | no | V | getzep.com/research; `OMNI` (published reference) |
| Zep (own corrected rerun) | 58.44 | Zep's own | -- | -- | V | `RE:SOTA_SWEEP_2026-09-30.md:C.1` (getzep/zep-papers issue #5) |
| Zep (Zep's May-2025 rebuttal) | 75.14 +/- 0.17 (also appears as 75.1 in ByteRover's table) | -- | -- | -- | V | `RE:sota/S6d_EVERMEMOS_MEM0_ZEP_BYTEROVER.md` |
| Zep on the EverMemOS harness | 85.22 | EverMemOS harness | gpt-4.1-mini | no | I (competitor-run) | same S6d |
| Mem0 (vendor, README) | 92.5 (1,425/1,540), top-200; ~6,956 ctx; SH 94.6, MH 95.4, T 92.5, OD 82.3 | gpt-5 | gpt-5 | no | V | `RE:scraped_mem0_benchmark_2026.md:76`; `OMNI` |
| Mem0 (vendor, its own results file) | **91.56** (1,410/1,540); 156 re-run questions merged; prompts contain LoCoMo gold answers | gpt-5 | gpt-5 | no | V | `RE:sota/S6d` (`results/platform/locomo_results.json`) |
| Mem0 on T-Mem's harness | 64.94 (Table 3) or 64.57 (Table 2) | Mem0 ACCURACY_PROMPT, gpt-4o-mini | gpt-4o-mini | no | I (stitched) | `RE:BENCHMARK_INTEGRATION_2026-09-30.md:38` |
| Mem0g (Mem0 paper) | 68.44 | not stated in our files | not stated | -- | V (paper) | `RE:sota/S1_CONVERSATIONAL.md` (b) |
| EverMemOS (arXiv 2601.02163) | 93.05; SH 96.67, MH 91.84, T 89.72, OD 76.04 (same order as `OMNI` published reference) | gpt-4o-mini, mean of 3 runs | gpt-4.1-mini | no | V | `OMNI` (published reference); `RE:sota/S6d` |
| EverMemOS (Nov-2025 README) | 92.32; SH 96.08, MH 91.13, T 89.72, OD 70.83; ~2,298 tok | gpt-4o-mini, 3 runs | gpt-4.1-mini | no | V | `RE:sota/S6d` (`evaluation/README.md @933f818`). Harness has asymmetric answer prompts |
| Hindsight (vendor site) | 92.0, ~36k ctx | Gemini 2.5 Flash Lite told to "be generous" | Gemini 3.1 Pro preview | not stated | V | `RE:SOTA_SWEEP_2026-09-30.md:C.1` |
| Hindsight (paper) | 89.61 / 85.67 / 83.18 (Gemini-3 / OSS-120B / OSS-20B) | -- | same | no | V | `RE:sota/S6_LEADERBOARD_REVERIFIED_2026-10-07.md` |
| MemMachine | 91.69 (agent) / 91.23 (memory mode); rerank-dependent | gpt-4o-mini | gpt-4.1-mini | no | V | `RE:S1_CONVERSATIONAL.md`, `S6` |
| Backboard.io | 90.00 (1,386/1,540); SH 89.36, MH 75.00, T 91.90, OD 91.20; judge told to be generous. Its 99.95 was LoRA-trained on the test conversations and is excluded | GPT-4.1 "generous" | Gemini-2.5-Pro | no | V | `OMNI`; `RE:SOTA_SWEEP`, `S6c` |
| MemoryLake | 94.03 overall (per-category figures differ between two copies in our files: 96.79/91.84/91.28/85.42 vs blog 95.71/91.28/95.47/93.68) | -- | -- | not stated | V | `OMNI`; `RE:web_locomo_benchmark.csv` (memorylake blog) |
| past.dev | 93.12; SH 95.01, MH 93.26, T 93.46, OD 75.00 (community-corrected answer keys) | -- | -- | -- | V | `OMNI` |
| Synthius-Mem | 94.37 | unresolved | -- | -- | V (paper, no code) | arXiv 2604.11563; `RE:SOTA_SWEEP` |
| Agent Zero Memory | 93.60 (8 LLMs tested) | -- | -- | -- | V | arXiv 2608.29606; `RE:SOTA_SWEEP` |
| Maximem Synap | 93.2 | in paper section 6 (not opened) | -- | -- | V | arXiv 2607.21503 |
| Honcho | 89.9 | -- | claude-haiku-4-5 chat; gemini-2.5-flash-lite ingest | -- | V | plasticlabs blog 2025-12-19; `RE:SOTA_SWEEP` |
| Dakera | 88.2 | **Recall@20, not QA** (GPT-4o presence judge) | none | 1,536 Q | V | `RE:sota/S6` |
| ByteRover | 96.1 on **1,982** Q | -- | -- | **yes** | V | `RE:sota/S6` (not comparable) |
| mem9 | 86.85 (published); SH 89.71, MH 83.16, T 89.25, OD 64.58 | -- | -- | -- | V | `OMNI` |
| Memora (Microsoft) | 86.3 | not checked | -- | -- | V | `RE:_shared/references.md` ([Zhang2026-memora-harmonic]) |
| MIRIX / Memobase | 85.38 / 72.01 | HyperMem authors' run | gpt-4.1-mini | no | I | `RE:sota/S1_CONVERSATIONAL.md` (README of HyperMem) |
| Memori | 81.95 (paper; SH 87.87, MH 72.70, T 80.37, OD 63.54; ~1.3k ctx); site shows 87; independent 41.3 | gpt-4.1-mini | gpt-4.1-mini | no | V / I | `OMNI`; `RE:sota/S6` |
| T-Mem | 80.26 (3 runs); F1 51.96; Qwen3-32B build 75.45; build/QA grid 4.1-mini/4o-mini 78.31, 4.1/4.1 82.51, 5.1/4o-mini 81.36, 5.1/5.1 84.85 | Mem0 ACCURACY_PROMPT, gpt-4o-mini, 3 judge passes | gpt-4o-mini | no | V (baselines copied from LoCoMo-Plus) | arXiv 2606.15405v2; `RE:SOTA_SYSTEMS_UPDATE_2026-10-02.md:20` |
| MemOS on T-Mem's harness | 75.80 (deployment not stated) | gpt-4o-mini | gpt-4o-mini | no | I (stitched) | `RE:SOTA_SWEEP` C.1 |
| A-Mem / SeCom on T-Mem's harness | 66.70 / 64.97 | gpt-4o-mini | gpt-4o-mini | no | I (stitched) | same |
| JustMem | 79.61 | judge model and prompt not published | gpt-4.1-mini | no | V (author harness; 5 repeats) | arXiv 2609.19877 Table 1; `RE:REUSE_MEMORY_QA_2026-09-30.md:28` |
| LightMem / SimpleMem / A-Mem / **MemoryOS** / naive RAG / full text | 79.08 / 76.55 / 67.91 / **65.12** / 63.82 / 57.18 (JustMem harness) | unpublished | gpt-4.1-mini | no | I (author of a competitor ran them) | same |
| SimpleMem (own paper) | **token F1 43.24** (not judge accuracy); Omni-SimpleMem F1 0.613 | F1 | -- | -- | V | `RE:REUSE_MEMORY_QA`; `SOTA_SYSTEMS_UPDATE_2026-10-02.md:19` |
| LightMem (own paper) | 71.95 | -- | gpt-4o-mini | -- | V | `RE:REUSE_MEMORY_QA` 1.2 |
| Mem++ / Nemori / A-Mem | 81.5 (gpt-4.1-mini), 77.4 (gpt-4o-mini) / 79.4, 74.4 / 61.4 (gpt-4.1-mini) | LLM judge | as listed | no | V / stitched | arXiv 2610.02002 Table 2; `RE:SOTA_SYSTEMS_UPDATE_2026-10-02.md:22,27,43` |
| MAGMA / Nemori (Anatomy of Agentic Memory) | 0.670 / 0.741 / 0.665 and 0.602 / 0.781 / 0.649 under three judge prompts | gpt-4o-mini, 3 prompts | -- | -- | I | arXiv 2602.19320; `RE:SOTA_SWEEP` |
| LoCoMo-Refined strict judge (1,382 revised Q) | MemoraX 82.65, MemOS **63.60**, EverMemOS 58.25, Mem0 48.91 | Qwen3-14B, strict prompt | -- | no | I | `RE:BENCHMARKS_UPDATE_2026-10-02.md:59` |
| TiMem | 75.30; SH 81.43, MH 62.20, T 77.63, OD 52.08 | LLM judge (secondary source) | -- | -- | V (secondary summary) | `RE:web_locomo_benchmark.csv` (emergentmind) |
| MERA / EGMemory / SpeakerMem-R1 / MutMem / Jev-Mem | 77.40 (Qwen3-30B reader) / 73.6 / 70.85 (**1,986 Q, cat 5 included**) / 74.12 / 0.777 | not checked | | see left | V | `RE:SOTA_SYSTEMS_UPDATE_2026-10-02.md:49-53`; `RE:SOTA_SWEEP:71` |
| D-Mem | Mem0 **F1 51.2**, Full Deliberation 55.3 (GPT-4o-mini) | F1 | gpt-4o-mini | -- | V | `RE:web_locomo_benchmark.csv` (emergentmind) |
| JustMem-harness naive RAG | 63.82 | | gpt-4.1-mini | no | I | above |

### 10.3 Ours (MemSpine) for reference

| Arm | Score | Judge | Reader | Cat 5 | V / I | Source |
|---|---|---|---|---|---|---|
| combo-A, post-fix (current default), 4K budget (~1.57K used) | **79.8**; SH 89.2, MH 62.8, T 79.4, OD 49.0 | Qwen3-32B rubric (abstention-aware) | Qwen3-32B | no | Ours (judge differs from the table above) | `RE:paper_spine/evaluation/ALL_LOCOMO_RESULTS_2026-10-06.md` |
| + cards header | 80.8; 90.7 / 63.1 / 79.4 / 50.0 | same | same | no | Ours | same |
| combo-A + Cohere rerank (pre-fix) | 80.6; 90.0 / 63.1 / 80.4 / 50.0 | same | same | no | Ours | same |
| naive dense RAG, same harness | 66.2 | same | same | no | Ours | same |
| cat 5 (446 q), combo-A `chat@dated` | 64.6% correct abstention | same | same | **yes (separate)** | Ours | same |
| Local Qwen stack: Qwen3-Embedding-0.6B, no reranker | **74.4**; SH 87.2, MH 56.0, T 70.4, OD 29.2; 1,571 ctx | Qwen3.5-9B Q4_K_M (local; can credit empty answers) | Qwen3.5-9B | no | Ours (single run) | `memspine/evals/QWEN_STACK_RESULTS.md:129-138` |
| bge-small, no reranker | 71.9; 86.1 / 50.0 / 67.0 / 29.2 | same | same | no | Ours | same |
| Jina v5 + Jina reranker v3.5 | 71.0; 85.9 / 51.8 / 64.2 / 19.8; 655 ctx | same | same | no | Ours | same |

**Not comparable to the published rows:** our judge is a Qwen model with a rubric prompt, not the Mem0 gpt-4o-mini prompt.
`S6_LEADERBOARD_REVERIFIED_2026-10-07.md` §5 lists the cheap like-for-like runs (about $1.5 for the EverMemOS harness; N57
re-judges existing answers with each vendor's judge locally for $0). `QWEN_STACK_RESULTS.md:140-142` already carries a short
"published" list; it omits MemU and gives MemOS 88.83 without the cloud caveat.

## 11. Specific caveats the table does not show

1. **MemU 92.09** is MemU's own harness, with a profile injected into the prompt (`use_profile=prompt`), a gpt-4.1 judge, and
   effectively no category-5 items. It is not in any independent harness: MemU is **absent from OmniMemEval's 14 backends**
   and from JustMem and T-Mem. The MemU repo's HEAD README no longer carries a LoCoMo claim; the v2 rewrite is a different system.
2. **MemOS 88.83** is cloud + MemTensor's harness; the neutral strict-judge figure is 63.60 and the T-Mem harness gives 75.80.
3. **Name collisions** to guard in any write-up: MemOS / MemoryOS / EverMemOS (EverOS) / "Mem0g" (graph variant of Mem0, 68.44).
4. **Vendor frontier-reader rows (89.9-94.7)** use gpt-5.x/Gemini-3 readers and 5-36k-token contexts, some with lenient
   judges and gold-laden prompts. The cross-harness band that holds up is roughly 64-80 on gpt-4.1-mini/gpt-4o-mini
   (`SOTA_SWEEP_2026-09-30.md:329`).
5. **LoCoMo licence** is CC BY-NC 4.0 (data and code); LoCoMo has known gold errors (24 in our `locomo_errata.json`).
