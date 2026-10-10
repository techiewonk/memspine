# Framework and tool-call survey: what memory engines build on, and what MemSpine should adopt

Date: 2026-10-10. Branch: `feat/local-qwen-stack`. Read-only study; no engine code was run or installed.
Question (user): does any memory system use LangChain or another framework for tool calls and similar,
and which of those patterns should MemSpine adopt, while staying light?

## 0. Method and evidence base

Code was read in shallow clones under the session scratchpad (`engines/<repo>/...`; every path below is
relative to the clone root and carries a line number). Repos read: Mem0 (v2.2.1), Graphiti, Letta (the
`letta/` folder is a docs stub; server code is read from `letta-old/`), LangMem, Cognee (+ `cognee-mcp`),
A-Mem, MIRIX, MemOS, memU, Memobase, Hindsight, Supermemory, EverOS; and, newly cloned for this study,
Zep (`integrations/`, `mcp/`), LangGraph, LlamaIndex, CrewAI, OpenAI Agents SDK, AG2, Agno, DSPy,
Pydantic-AI, Mastra, `mem0-mcp`. Limits: Mastra's `@mastra/memory` source is not in the shallow clone
(docs only, flagged `docs`); OpenMemory (mem0's MCP product) is not in the Mem0 tree, so Mem0's MCP is read from
`integrations/`. MemSpine is read on the branch head, `src/memspine/...`. Benchmark numbers quoted for
MemSpine come from `evals/analysis/GAP_REGISTER.md` (LoCoMo dev multi-hop 64.0, open-domain 40.6, cat 5 69.4;
OP-Bench irrelevance 15.0, baiting 3.0). Effects of any proposed change on those numbers are hypotheses,
not measurements; none has been run.

## 1. Answer in one paragraph

Almost no memory engine is *built on* LangChain. Only LangMem is (core deps `langchain`, `langgraph`,
`trustcall`, `langmem/pyproject.toml:9-16`) and it is the only one whose store is a LangGraph `BaseStore`.
Everyone else owns its LLM client layer (Letta, Hindsight, MemOS, Graphiti, memU, EverOS: native SDK
clients) or rides litellm (Hindsight `:83`, Cognee `:45`, A-Mem `:23`, MemSpine `:32` core) and, for schema
output, instructor (Cognee `:46`, CrewAI `:18`) or the provider's `json_schema`/`json_object`
mode (Graphiti, Mem0, A-Mem, Memobase). LangChain/LlamaIndex appear as *optional* adapters (Mem0 extras,
Cognee extras `:164-169`), as text-splitters (MemOS, Cognee), or as separate integration packages (Zep's
`zep-langgraph`, Hindsight, Supermemory). Framework use is far more common on the *agent-facing* side
(tool sets, MCP) than on the extraction side. Tool-calling memory comes in three real forms: agent-managed
write/edit tools (Letta, LangMem, MIRIX, Agno, Mem0 plugin, Supermemory), MCP servers (9 of the 14
memory engines surveyed: Mem0 plugin, Graphiti, Cognee, MemOS, Memobase, Hindsight, Supermemory, Zep, and Letta as an MCP client), and reflect/deep-search agents that loop over *read* tools (Hindsight,
MemOS, Cognee, CrewAI, EverOS). MemSpine has none of the three today, but it has the strongest write-side
safeguards, so the adoption job is to add the surfaces on top of the firewall, not to import a framework.

## 2. Matrix, per dimension

Legend: **core** = hard dependency; **opt** = optional extra or separate package; **-** = none found.

### 2.1 Framework dependency and LLM-client abstraction

| System | Built on a framework? | LLM client abstraction | Evidence |
|---|---|---|---|
| Mem0 | No. LangChain/LlamaIndex are **opt**: `extras` group and an `llms.langchain` provider that raises ImportError if absent | Own `LlmFactory` provider map (18 providers); openai SDK is core, litellm is an **opt** `llms` extra | `pyproject.toml:13-22` (core), `:56-66` (llms, litellm `:60`), `:67-76` (langchain `:69-71`); `mem0/utils/factory.py:42-60`; `mem0/llms/langchain.py:7-10` |
| Graphiti | No. `langchain-aws` only in the `neptune` extra; langgraph/langchain only in dev group | Own `LLMClient` classes over the openai SDK, plus anthropic/gemini/groq extras; `json_schema` default | `pyproject.toml:13-22`, `:40`, `:51-57`; `graphiti_core/llm_client/openai_generic_client.py:37,66` |
| Letta (letta-old) | No. `llama-index` is a **core** dependency but used for chunking/connectors only; langchain in optional groups | Own native client per provider (anthropic, openai, bedrock, google, ... 20+ files) | `pyproject.toml:43-44`, `:134-149`; `letta/services/file_processor/chunker/llama_index_chunker.py`; `letta/llm_api/llm_client_base.py:26` |
| LangMem | **Yes, LangChain + LangGraph core**, plus trustcall | LangChain `BaseChatModel` (`init_chat_model` strings) | `langmem/pyproject.toml:9-16`; `src/langmem/knowledge/extraction.py:161` |
| Cognee | No. litellm and instructor are **core**; langchain and llama-index are **opt** extras | litellm + instructor (`from_litellm`) | `pyproject.toml:45-46`, `:164-169`; `cognee/infrastructure/llm/structured_output_framework/litellm_instructor/llm/generic_llm_api/adapter.py:133` |
| A-Mem | No | litellm `completion` (core) | `pyproject.toml:23`; `agentic_memory/llm_controller.py:5` |
| MIRIX | No framework core. `llama_index` is core but used in `embeddings.py`; langgraph/langchain only in `samples/` | Own `llm_api` package over openai SDK | `pyproject.toml:42,63,80`; `samples/pyproject.toml:82-84` |
| MemOS | No. `langchain-text-splitters` only; langgraph in an eval group | Own `llms/` factory (openai, ollama, HF, vllm) | `pyproject.toml:39-47`, `:97`, `:202` |
| memU | No (v2). Core deps are httpx, numpy, openai, pydantic, sqlmodel | openai SDK; extraction is delegated to the host agent (see 2.3) | `pyproject.toml:21-29`; `src/memu/agentic_backend.py:29-41` |
| Memobase | No | openai SDK, `json_object` | `requirements.txt:3`; `src/server/api/memobase_server/llms/__init__.py:32` |
| Hindsight | No. litellm and fastmcp are **core**; ~6 agent-framework integrations are separate packages | Own `LLMInterface` + 20 providers (litellm is one provider among them) | `hindsight-api-slim/pyproject.toml:39,83`; `hindsight_api/engine/providers/`; `README.md:244` |
| Supermemory | No. Per-framework SDK packages (Vercel AI, Mastra, OpenAI, VoltAgent, MS Agent Framework, Pipecat, LiveKit) | Hosted service; SDKs are thin | `packages/tools/src/{ai-sdk,mastra,openai,vercel,voltagent}`; `packages/agent-framework-python/` |
| EverOS | No | openai SDK; `response_format: type[BaseModel]` | `pyproject.toml:49-50`; `src/everos/component/llm/client.py:51` |
| Zep | No (Graphiti core). 11 **opt** integration packages (langgraph, crewai, autogen, ag2, adk, mastra, pydantic-ai, strands, vercel-ai, livekit, ms-agent-framework) | n/a | `integrations/langgraph/python/pyproject.toml:2,8-10` (`zep-langgraph` depends on langchain-core, langgraph, zep-cloud) |
| CrewAI (memory) | Is the framework. instructor **core**, litellm **opt** | instructor over openai; litellm extra | `lib/crewai/pyproject.toml:18`, `:98` |
| LlamaIndex, LangGraph, OpenAI Agents SDK, Agno, Pydantic-AI, DSPy, AG2, Mastra | Frameworks themselves | n/a | see 2.2 |
| **MemSpine** | No. | `LLMService` Protocol (`chat(messages, **options) -> str`); litellm is **core** (ADR-024), llama.cpp in-proc **opt**; the `[structured]` extra lists instructor but no code imports it | `src/memspine/services/llm/base.py:33-39`; `pyproject.toml:32,41`; `services/llm/structured.py:5` (docstring only) |

Read-across: the "light" engines (Graphiti, memU, EverOS, Memobase, Letta) avoid litellm and call the openai
SDK or native clients. MemSpine already has the right seam (a Protocol), so a lighter OpenAI-compatible
httpx client is possible later; it is not the point of this study and is not a gap row.

### 2.2 Tool-calling memory (agent-managed memory)

| System | Tools exposed to the LLM | How the agent decides | Safeguards |
|---|---|---|---|
| Letta (letta-old) | `memory` (create / str_replace / insert / delete / rename), older `core_memory_append|replace`, `memory_replace|insert|apply_patch|rethink|finish_edits`, `archival_memory_insert|search`, `conversation_search` | Tool descriptions plus memory-block text in the system prompt; `request_heartbeat` param lets the model chain steps | per-block `read_only` (`schemas/block.py:36`); char limit `CORE_MEMORY_BLOCK_CHAR_LIMIT` (`constants.py:435`); tool-output truncation (`constants.py:200`); per-tool `default_requires_approval` and `requires_approval` tool rule (`schemas/tool.py:59`, `tool_rule.py:350`), denial path at `agents/letta_agent.py:1752`; `max_steps` loop (`:247`). Tools: `functions/function_sets/base.py:10,164,194,246,263,311,391,453,488` |
| LangMem | `manage_memory(content, id, action: create|update|delete)` and `search_memory(query, limit, offset, filter)` as LangChain tools over a `BaseStore` | Default tool instructions say "proactively call when you identify a new USER preference..." (`knowledge/tools.py:28-33`); search instruction string is empty (`:359`) | `actions_permitted` tuple restricts verbs (`:34`, example `:195`); namespace templated from `langgraph_user_id` at runtime. No trust, no confirmation |
| Mem0 | OSS core exposes methods, not tools. The Hermes plugin ships `mem0_add` (verbatim, no LLM), `mem0_search`, `mem0_update`, `mem0_delete`; the agent-plugin MCP ships only `search_memories` | Prompt text in the schema description: add "the moment the user states a lasting preference", search "several times - vary the wording and run follow-up searches" | add is verbatim (no extraction cost). MCP tool is read-only. `integrations/hermes-plugin-mem0/__init__.py:434-501`; `integrations/agent-plugin-core/python/mcp_server.py:22-182` |
| MIRIX | Per-memory-type agents with `core_memory_append|rewrite`, `episodic_memory_insert|merge|replace`, `semantic_memory_insert|update`, `knowledge_vault_insert|update`, `resource_memory_*`, `skill_create|edit|delete`, `search_in_memory` | A meta agent plus one specialist agent per memory type (`agent/meta_agent.py`, `agent/*_memory_agent.py`) | **Best write-side tool safeguards found**: per-tool argument validators (`agent/tool_validators.py:22-196`), skill edit budget (`function_sets/memory_tools.py:523-560,681`), size gate (`:565`), delete authorization (`:624`), `MAX_CHAINING_STEPS=10` (`constants.py:86`) |
| Agno | `add_memory`, `update_memory`, `delete_memory`, `clear_memory` as agent tools | `enable_agentic_memory=True` (`agent/agent.py:135`) adds the tools; `update_memory_on_run` runs a background manager instead (`:137`) | per-verb enable flags (`memory/manager.py:1329-1333`); empty-string update refused (`:1377`); `clear_memory` is exposed with no confirmation (`:1413`) |
| CrewAI | `RecallMemoryTool`, `RememberTool` | `create_memory_tools(memory)` hands them to an agent (`tools/memory_tools.py:25,75,104`) | none beyond scope views |
| Hindsight | Reflect-agent tools `search_mental_models`, `search_observations`, `recall`, `read_mental_models`, `expand`, `done` (internal); public MCP: `retain`, `sync_retain`, `recall`, `reflect`, banks, mental models | Reflect agent is prompted to gather evidence then call `done`; tool choice can be forced (`agent.py:965,1201-1215`) | MCP tool annotations read-only / destructive / write (`mcp_tools.py:560-599`); per-tool enable list (`:799`); audit wrapper (`:914`); `DEFAULT_MAX_ITERATIONS=10` (`agent.py:150`) |
| Cognee | MCP: `remember`, `recall`, `code_search`, `forget`, `improve`, `cognify_status`. Agentic retriever calls registry tools plus `load_skill` | LLM emits a structured `AgentStep` (tool_call or final_answer), not native tool calling | `execute_tool` enforces per-dataset permissions like search (`modules/retrieval/agentic_retriever.py:1-11`); tool tiers via `apply_tool_mode` (`cognee-mcp/src/server.py:209`); output cap `MAX_TOOL_OUTPUT_CHARS=8000` (`agentic_retriever.py:40,509`) |
| MemOS | MCP: `chat`, `search_memories`, `add_memory`, `get_memory`, `update_memory`, `delete_memory`, `delete_all_memories`, cube/user admin, `control_memory_scheduler` | Client LLM chooses; no policy | none visible; admin and destructive verbs share the tool list (`api/mcp_serve.py:142-538`). Cautionary example |
| Supermemory | MCP: `add_memory` (action save|forget), `search_memory`, `get_profile`, `list_memories`, `guided_save`, `select_space`, namespaces, graph tools | `guided_save` is a prompt-style helper; space is chosen explicitly | shared `MEMORY_TOOL_ANNOTATIONS`; the active space is bound before writes (`apps/mcp/src/server/tools/add-memory.ts:23-60`) |
| Zep | MCP is read-oriented: `search_graph`, `get_user_context`, `get_user`, `list_threads`, `get_user_nodes|edges`, `get_episodes`, `get_thread_messages`, `get_node` (no write tools) | n/a; the LangGraph package injects context before the model call and persists after | `mcp/zep-mcp-server/internal/server/tools.go:9-49`; `integrations/langgraph/python/src/zep_langgraph/hooks.py:55`, `persistence.py:197` |
| Mastra | `updateWorkingMemory` tool over a template (resource or thread scope); semantic recall is automatic | Agent updates the working-memory template | docs: `docs/src/content/en/docs/memory/working-memory.mdx:30-35,353` (source not in clone) |
| Mem0 core, Graphiti core, A-Mem, memU, Memobase core, EverOS | Library or service API only | n/a | Graphiti ships an MCP server (below) |
| LlamaIndex, LangGraph, OpenAI Agents, AG2, Pydantic-AI, DSPy | No agent-facing memory tools in the library. LangGraph defines the `BaseStore` that LangMem tools target; Pydantic-AI and DSPy have no memory module (grep found none); DSPy has a `ReAct` tool loop (`dspy/predict/react.py:16`) | n/a | n/a |
| **MemSpine** | **None.** REST only (`protocols/rest/app.py:304-597`); MCP seat reserved (`protocols/__init__.py:1-2`) | n/a | n/a (but see section 3: the write-side safeguards exist) |

### 2.3 Structured output and extraction calls

| System | Mechanism | Update decision | Evidence |
|---|---|---|---|
| Mem0 v2 | Single `json_object` call with an additive extraction prompt that sees existing memories; plain `json.loads` then `extract_json` fallback | v2 dropped the ADD/UPDATE/DELETE/NOOP tool-call round trip: extraction is additive, updates are explicit API calls | `mem0/memory/main.py:944-981`, `:2619-2655` |
| Graphiti | Pydantic `response_model` per prompt; `responses.parse` (OpenAI), forced tool for Anthropic, `json_schema` (default) or `json_object` for generic endpoints | LLM-judged duplicates and contradictions, structured | `llm_client/openai_client.py:103`; `anthropic_client.py:211-213,291-298`; `openai_generic_client.py:37,66` |
| LangMem | **trustcall** `create_extractor(model, tools=[schema], tool_choice="any")`: JSON-patch updates of existing schema objects, bounded by `max_steps` | `enable_inserts`, `enable_deletes` flags | `knowledge/extraction.py:161,224-298` |
| Cognee | instructor over litellm, mode per provider | n/a | `.../instructor_modes.py:19`; `pyproject.toml:46` |
| A-Mem | `response_format = json_schema` | LLM decides evolve / strengthen | `agentic_memory/memory_system.py:205,623` |
| Memobase | `json_object` | n/a | `memobase_server/llms/__init__.py:32` |
| EverOS | `response_format: type[BaseModel]` | n/a | `component/llm/client.py:51` |
| MIRIX | Native function calling: the extraction agent *is* a tool-calling agent writing through `*_insert` tools, with validators | validators plus edit budgets | `agent/tool_validators.py`, `memory_tools.py` |
| CrewAI | instructor | n/a | `pyproject.toml:18` |
| memU v2 | None in the engine path: the host agent extracts and calls `commit_results` | host decides | `agentic_backend.py:35`; `app/agentic.py:353` |
| **MemSpine** | Prompt-declared YAML or JSON, `yaml.safe_load` with a line-based salvage reader, `json-repair`, then pydantic `model_validate`; failure raises `LLMError` with raw text. 12 call sites. No schema-constrained decoding, no retry-with-error | Decider tasks and `latest_wins` are rule/decider based, not LLM tool calls | `services/llm/structured.py:30-130`; `engine.py:5733,7610,11924,12182` and others |

### 2.4 Protocol surfaces

| System | MCP | REST | Framework adapters |
|---|---|---|---|
| Mem0 | stdio JSON-RPC written by hand in 230 lines, no MCP SDK (`agent-plugin-core/python/mcp_server.py:152-215`) | cloud + `mem0-ts` | Vercel AI SDK, Strands, n8n, Zapier, 8 coding-agent plugins; LangChain as an LLM provider only |
| Graphiti | FastMCP-style server (`mcp>=2`): `add_memory`, `search_nodes`, `search_memory_facts`, `delete_entity_edge`, `delete_episode`, `add_triplet`, `get_episodes`, `build_communities`, `clear_graph`, `get_status` (`mcp_server/src/graphiti_mcp_server.py:403-1146`) | FastAPI `server/` | via Zep integrations |
| Letta | `mcp[cli]`, `fastmcp` core deps (`pyproject.toml:57,76`); the server manages external MCP servers as tool sources (`letta/server/rest_api/routers/v1/mcp_servers.py`), i.e. MCP client side | REST (core product) | LangChain tools importable as Letta tools |
| LangMem | none | none | LangGraph `BaseStore` + prebuilt agents |
| Cognee | `cognee-mcp` (fastmcp 3) with tool tiers | FastAPI | langchain / llama-index extras |
| MIRIX | MCP **client** (`functions/mcp_client/`) | FastAPI | none |
| MemOS | `api/mcp_serve.py` (fastmcp, core) | FastAPI | none |
| memU | none | optional cloud | host adapters via hooks and CLI for Claude Code, Codex, Cursor, Hermes, OpenClaw, Pi |
| Memobase | `src/mcp` (httpx + `mcp[cli]`) | REST core | OpenAI wrapper clients |
| Hindsight | fastmcp core (`mcp_tools.py:602+`, also `mcp_local.py`) | FastAPI | LangGraph, LlamaIndex, CrewAI, Pydantic-AI, OpenAI Agents, Google ADK (separate packages, `README.md:244`) |
| Supermemory | Cloudflare-hosted MCP with widgets | REST | Vercel AI, Mastra, OpenAI, VoltAgent, MS Agent Framework, Pipecat, LiveKit |
| Zep | Go MCP server, read tools | REST | 11 integration packages |
| EverOS | none found | FastAPI | Claude Code plugin in `use-cases/` |
| A-Mem | none | none | none |
| **MemSpine** | **none** (reserved seat) | FastAPI, 30+ routes incl. quarantine, grants, skills, plans, watches, audit (`protocols/rest/app.py:304-597`), `[rest]` extra | **none** |

Target interfaces (what an adapter has to satisfy):

| Framework | Contract | Evidence |
|---|---|---|
| LangGraph | `BaseStore(ABC)` with `get`, `put`, `search`, `delete`, `list_namespaces`, `batch` over ops `GetOp|SearchOp|PutOp|ListNamespacesOp`; namespace is a tuple | `libs/checkpoint/langgraph/store/base/__init__.py:708,733-966` and ops `:157,203,368,431` |
| LlamaIndex | `BaseMemory` (`get(input)`, `put`, `put_messages`, `set`, `reset`) and, more useful, `BaseMemoryBlock` (`_aget`, `_aput`, `priority`, token-budgeted truncation) inside `Memory` | `llama-index-core/llama_index/core/memory/types.py:14-69`; `memory.py:103,188,205` |
| OpenAI Agents SDK | `Session` Protocol: `get_items(limit)`, `add_items`, `pop_item`, `clear_session` (chat history, not long-term memory) | `src/agents/memory/session.py:53-96`; extensions `extensions/memory/*.py` |
| CrewAI | `Memory.remember/recall(depth=shallow|deep)` | `memory/unified_memory.py:435,686-692` |
| Agno | `MemoryManager` with `_get_db_tools` | `memory/manager.py:1323` |

### 2.5 Hot-path versus background

| System | Hot path | Background |
|---|---|---|
| Letta | Memory tool calls inside the agent step | Sleeptime agents share memory blocks: `SleeptimeManager`, `sleeptime_agent_frequency` (`schemas/group.py:119-125`; `agents/voice_sleeptime_agent.py:30`) |
| LangMem | `manage_memory` tool inline | "Background memory manager" `create_memory_store_manager` (`extraction.py:1666`) fired through `ReflectionExecutor.submit(..., after_seconds)` debounce (`reflection.py:54,254,328`) |
| Mem0 | Extraction inline in `add` (one LLM call) | Async API variants (`main.py:2640`); no queue |
| Graphiti | `add_episode` inline | MCP server queues episodes per group, sequential worker (`mcp_server/src/services/queue_service.py:30-50`; `graphiti_mcp_server.py:509`) |
| Agno | `enable_agentic_memory` tools | `update_memory_on_run` memory manager after the run (`agent/agent.py:137`) |
| Mastra | working-memory tool | Observer and Reflector background agents, `bufferOnIdle` (docs `observational-memory.mdx:16,204`) |
| MemOS | search inline | `mem_scheduler` task queue (`mem_scheduler/base_scheduler.py:69`) |
| Memobase | `insert_blob_to_buffer` | buffer flushed later (`controllers/buffer.py:33`) |
| Cognee | search | `cognify` pipeline; MCP `_track_background` (`server.py:120`) |
| MIRIX | tool-calling specialist agents | `background_agent.py`, `auto_dream_agent.py`, `reflexion_agent.py` |
| memU | LLM-free single-shot retrieval (`app/agentic.py:187-231`) | `commit_results` runs no ingest or LLM (`app/agentic.py:353`); the host agent does extraction through `hosts/bridging` |
| **MemSpine** | `write` and `write_messages` run enrichment inline (grep of `engine.py` finds no `create_task` or queue) | Strong: sleep cycle `consolidate → decay → compress → prune`, idempotent step functions, runner-agnostic (`workers/pipelines.py:1-9,966`; `engine.sleep` `:11567`; `workers.sleep_interval_seconds` `config/schema.py:267`; inline / dbos / taskiq runners) |

### 2.6 Agentic retrieval

| System | Loop | Stop | Query rewrite |
|---|---|---|---|
| Hindsight | Reflect agent, tool loop with forced steps | `done` tool, 10 iterations (`engine/reflect/agent.py:150,1193-1215`) | LLM-chosen tool arguments |
| MemOS | `DeepSearchMemAgent`: QueryRewriter, search, Reflector sufficiency, refine for missing entities | `status == sufficient` or `max_iterations` (`mem_agent/deepsearch_agent.py:85,121,144,169-192,324`) | yes, from missing entities |
| Cognee | `GRAPH_COMPLETION_COT` (`max_iter=4`, follow-up queries each round), `AGENTIC_COMPLETION` ReAct with tools, `FEELING_LUCKY` lets the LLM pick the search type | iterations / `final_answer` (`retrieval/graph_completion_cot_retriever.py:70,203`; `agentic_retriever.py:487`; `SearchType.py:15,17,21`) | yes |
| CrewAI | `RecallFlow`: query distillation into sub-queries, parallel search, confidence-routed deepening | `exploration_budget`, confidence (`memory/recall_flow.py:1-9,55,275`) | yes |
| EverOS | sufficiency check then a second round, benchmark-frozen constants | 2 rounds (`memory/search/agentic.py:1-40,239`) | yes (opaque `everalgo`) |
| LangMem | `create_memory_searcher` generates queries from context via forced `bind_tools(... tool_choice="search_memory")` | one pass (`extraction.py:695,784`) | yes |
| Mem0 | none in engine; the plugin tool description tells the agent to search several times (`hermes-plugin-mem0/__init__.py:473-475`) | agent-driven | agent-driven |
| Letta | agent calls `archival_memory_search` repeatedly via heartbeat | `max_steps` | agent-driven |
| Graphiti, Zep, A-Mem, Memobase, memU, Supermemory | single retrieval (Graphiti `search_` recipes) | n/a | no |
| **MemSpine** | Single-shot only: LLM planner emits subqueries (`planner: llm`, `planner_version: v2/v3`, `config/schema.py:478-489`), rule or decider read modes (`engine.py:12043`), **one** bridge hop gated by cue or weak score (`schema.py:1038-1055`; `engine.py:2905,5303,6262`). No iteration, no sufficiency check, no step budget | n/a | partial (planner subqueries) |

## 3. MemSpine against each pattern

| Pattern | MemSpine | Verdict |
|---|---|---|
| Framework-free core | Yes (litellm is the heaviest dep) | Parity with the light group; no change |
| Own LLM Protocol | `LLMService` (`base.py:33`) | Parity (Hindsight, Graphiti, Letta have the same seam) |
| Structured output tolerance | Lenient YAML salvage, json-repair, pydantic (`structured.py`) | Better than Mem0's `json.loads` + regex fallback; **lacks** schema-constrained decoding (Graphiti) and retry-with-error (trustcall style) |
| `[structured]` instructor extra | Declared (`pyproject.toml:41`), not wired | **Doc/code mismatch**: remove the extra or wire it; this study recommends removing it |
| Agent memory tool set | None | **Lacks** |
| MCP server | None; seat reserved | **Lacks** (peers lead: `ECOSYSTEM_COMPARISON.md:45`) |
| Framework adapters | None | **Lacks** (optional extras are the right shape) |
| Write safeguards for agent-initiated writes | Firewall: role trust caps, `mcp`/`rest`/`ingest` channels capped (`core/policies/trust.py:41`), instruction flag, anomaly and quarantine with operator approval (`core/firewall.py:237`; REST `:426-449`), lineage taint audit (`:597`) | **Has it, and better**: only MIRIX validators/budgets, Letta approval and read-only blocks come close, none caps trust by origin or carries a reach bound |
| Tool-output safety | `core/escaping.py` marker escaping exists for context; no tool result format yet | Partial |
| Verbatim agent write (no LLM on hot path) | `write` is raw by design | **Has it** (Mem0's plugin ships this as `mem0_add`) |
| Explicit forget, erase, correct | `forget`, `erase_subject`, `forget_requests`, `correct` (`engine.py:7956-8473,8912`) | **Has it, and better** (Agno exposes `clear_memory` with no confirmation; MemOS `delete_all_memories`) |
| Background consolidation | Sleep cycle, idempotent, replayable | **Has it, and better** than Letta sleeptime (deterministic pipelines, no extra agent) |
| Deferred ingest extraction | Absent | **Lacks** (Graphiti queue, Memobase buffer, LangMem debounce) |
| Agentic read loop | One-shot planner and one bridge hop | **Lacks** the loop (Hindsight, MemOS, Cognee, CrewAI) |
| Read-only MCP tier | n/a | **Lacks** (Zep, mem0 plugin ship read-only) |
| Tool annotations (read-only / destructive hints) | n/a | **Lacks** (Hindsight `mcp_tools.py:592`, Supermemory) |

## 4. What to adopt

Principles: no hard dependency on LangChain, LlamaIndex, LangGraph, instructor, or an MCP SDK in the core
wheel. Everything below is either pure Python on the existing `Engine`, or an optional extra imported
lazily. All agent-initiated writes go through the existing firewall; the new surface adds nothing that can
bypass it.

Ranking by value over cost (S = about a day, M = a few days, L = a week or more; value is for the project
goals: score on the two benchmarks, adoption, and safety posture):

| Rank | Adoption | Value | Cost | Gap id |
|---|---|---|---|---|
| 1 | Agent memory tool-set + agent-write guard | high (enables 2, 3, 4, 7) | M | I56 |
| 2 | MCP server surface (zero-dependency stdio + optional SDK transport) | high (adoption, N1 demo surface) | M | I57 |
| 3 | Agent-gated memory use for OP-Bench (agent decides whether to search) | high for irrelevance 15.0 and baiting 3.0, if the hypothesis holds | S after 1 | I59 |
| 4 | Structured output: measure first, then constrained-decoding retry; drop the unused extra | medium (parse failures on small Qwen) | S | I60 |
| 5 | Agentic multi-step retrieval read mode, opt-in, step-budgeted | medium-high for multi-hop 64.0; low for open-domain 40.6 | M | I58 |
| 6 | Deferred (queued) ingest enrichment | medium (latency of tool writes, throughput) | M | I62 |
| 7 | Optional adapters: LangGraph `BaseStore`, LlamaIndex memory block, OpenAI Agents `Session` | medium (adoption only, no benchmark effect) | M | I61 |

### 4.1 Memory tool-set for agents, with the firewall applied (I56)

Design: a new module `memspine/agent_tools` with no third-party imports. It defines a neutral function
list (OpenAI-style `{"name","description","parameters":{JSON Schema}}`, which every framework and MCP can
consume) and a dispatcher `call(name, args, ctx)` that runs on the `Engine`. REST, MCP and adapters all
call the dispatcher, so the guard lives in one place.

| Tool | Arguments (JSON Schema, required first) | Engine call | Notes |
|---|---|---|---|
| `memory_search` | `query: string`, `top_k: int 1..20 (default 8)`, `as_of?: date`, `kinds?: [string]`, `purpose?: string` | `Engine.search` | read-only; returns `{id, text, trust, src, valid_from, score}` rows; text passed through `escape_markers` and wrapped as data |
| `memory_write` | `content: string (max 1000 chars)`, `kind?: enum`, `entity?`, `attribute?` | `Engine.write` with `channel="agent_tool"` | verbatim, no LLM on the hot path (Mem0 `mem0_add` pattern); firewall caps trust (the channel joins `_EXTERNAL_CHANNELS`, `trust.py:41`); stamped `src:assistant-proposed` |
| `memory_update` | `id: string`, `content: string`, `reason: string` | `Engine.correct` | supersede, never in-place overwrite; allowed on records the same principal wrote, or any record when the operator profile is set |
| `memory_forget` | `id: string`, `reason: string` | `Engine.forget_requests` | creates a forget request; executes directly only for the agent's own records; bulk and erase verbs are not in the tool list |
| `memory_confirm` | `id: string`, `user_quote: string` | new: promote `assistant-proposed` to `user-confirmed` | the tool verifies that `user_quote` occurs verbatim in a recent user turn of the session; the agent cannot confirm on its own say-so |

Decision policy: prompt-based (tool descriptions, as in LangMem and Mem0: "call when a lasting preference is
stated"), with the engine as the authority. Do not copy the Agno `clear_memory`/MemOS `delete_all_memories`
pattern: destructive bulk verbs stay out of the model-facing list.

Guards (new, small): (a) channel and trust cap as above; (b) per-session write budget (default 5 writes per
turn, 30 per session) and 1000-character limit, validators in the style of MIRIX `tool_validators.py`;
(c) duplicate check before write (existing dedupe stage); (d) write results return the verdict
(`stored|quarantined|duplicate`) so the model cannot assume success; (e) tool outputs are truncated
(Letta `constants.py:200`, Cognee `MAX_TOOL_OUTPUT_CHARS`); (f) tool results never re-enter the write path
as trusted text: records read through a tool keep their `retrieved` provenance if the model echoes them into
a new write (this is the existing taint audit, N1).

Benefit: OP-Bench: this is the precondition for agent-decided memory use (4.3). LoCoMo: none by itself.
Risks: the agent fabricates writes (mitigated by trust cap, `assistant-proposed` provenance, quarantine,
`memory_confirm`), prompt injection through tool results (mitigated by escaping and the instruction flag on
read content), runaway loops (step budget). Resource: pure Python, no new dependency; about 300 lines plus
tests; tool definitions add about 600 prompt tokens per call when exposed.

### 4.2 MCP server (I57)

Two layers. Layer 1 (core, zero dependency): a minimal stdio JSON-RPC handler for `initialize`,
`tools/list`, `tools/call`, written the way Mem0's `agent-plugin-core/python/mcp_server.py:152-215` does it
(about 230 lines, no SDK). Layer 2 (extra `[mcp]`): the official `mcp` package for streamable HTTP and auth,
for deployers who want it. Tool tiers and annotations copied from the peers: `read` (search), `write`
(write, update, confirm), `admin` (forget, quarantine approve; operator profile only), with MCP
`readOnlyHint` / `destructiveHint` as Hindsight does (`mcp_tools.py:592-599`) and a read-only profile as
Zep ships. Namespace comes from server config, not from the model (Supermemory binds the space before the
write, `add-memory.ts:23`). The `mcp` channel is already in `_EXTERNAL_CHANNELS`, so MCP-origin writes
are capped by construction.

Benefit: adoption by Claude Desktop, Cursor and others; a demo surface for N1. Risks: MCP clients can call
tools in loops, so the dispatcher rate limit (4.1) is mandatory. Resource: layer 1 free; layer 2 one
optional wheel.

### 4.3 Agent-gated memory use (I59) and what it can do for OP-Bench

Idea: instead of always injecting retrieved context (which LoCoMo rewards and OP-Bench punishes,
GAP_REGISTER progress log 14:25), expose `memory_search` as a tool and let the model decide whether to call
it. If it does not call, the context contains no memory, which is exactly the right behaviour for an
irrelevant or baiting prompt. Hypothesis (unmeasured): irrelevance 15.0 and baiting 3.0 rise because the
model sees the question without memory unless it asks. Counter-risk: small local Qwen under-calls tools on
LoCoMo, losing recall. Mitigation: a `first_step: forced|free` switch (Hindsight forces a tool sequence,
`reflect/agent.py:1201-1215`), arm A forced for LoCoMo, arm B free for OP-Bench; compare with the
already-built `relevance_gate: store_calibrated` (I29/I30/I37), which gets the same effect without an extra
model call. Treat the tool-gated arm as a research comparison; if the gate already closes the gap, do not ship
the tool-gated mode. Cost: one extra model turn when memory is needed. Needs 4.1 first.

### 4.4 Agentic multi-step retrieval, opt-in (I58)

Add `read.mode: agentic` beside the existing planner and bridge hop, never as default. Loop (cap
`max_steps=3`, hard limit 5, plus a token cap):

1. step: the model returns a structured action (Cognee's `AgentStep`, `agentic_retriever.py:1-11`, works
   without native tool calling, which suits local Qwen): `search(query, filters)`, `expand(record_id)` (neighbours,
   thread context, existing graph hop), or `done`.
2. after each search: fuse new hits with RRF into the running evidence set; compute novelty
   (new ids / total); stop when novelty is zero, when a sufficiency check says `sufficient` (MemOS
   `Reflector`, `deepsearch_agent.py:85-120`; EverOS `is_sufficient`, `agentic.py:239`), or at the budget.
3. return the evidence set; the existing assemble and reader stages run unchanged.

Run it as four arms on the same split: baseline, `bridge_hop`, planner v3, agentic. Expected effect
(hypothesis): multi-hop 64.0 is the target (questions that need a second fact found from the first); the
bridge hop already tries this once, so gain is bounded by how often one hop is not enough. Open-domain
40.6 is mostly a reader problem (R2-6 refusals), so little gain expected. Cat 5 could get worse if extra
searches pull in distractors; the novelty and sufficiency stops and the relevance gate are the guard.
Risks: latency (up to 3 model calls plus 3 searches per question on the local GPU stack), cost
(+N model calls; unmeasured), non-determinism; keep temperature 0 and log every step in
`search_forensics`. Resource: no dependency; about 250 lines plus one prompt pair.

### 4.5 Structured outputs (I60)

Do not add instructor (the unwired extra is a liability: remove it, or update the docstring). Steps:
(1) measure first: count `structured.yaml_parse_failed` and `LLMError` per prompt on the Qwen stack runs;
if parse failure is below about 1% per call, stop here. (2) If not: add an optional `response_schema`
option to `structured_call` that passes `response_format={"type":"json_schema",...}` to OpenAI-compatible
endpoints (Graphiti default, `openai_generic_client.py:66`, with `json_object` fallback) or a GBNF grammar
on llama.cpp, used only on the retry after a failed parse, so the YAML token saving (E9) is kept on the
happy path; (3) one retry with the validation error appended (the trustcall idea of patching rather than
regenerating can come later). Benefit: fewer dropped extractions on small models; indirect for LoCoMo
because lost facts are lost recall. Risk: constrained decoding changes output distribution; behind a flag,
default off. Resource: S, zero dependencies.

### 4.6 Deferred write and background split (I62)

MemSpine's background story (sleep cycle) is strong; the gap is ingest. Add `write.mode: sync|deferred`.
`deferred` returns after the raw verbatim record and firewall verdict (cheap, deterministic), and queues
enrichment (cue generation, fact mining, entity linking, summaries) on a per-namespace sequential queue
(Graphiti `queue_service.py:30-50`) with an optional debounce so a burst of turns is processed once
(LangMem `reflection.py:54,328`). Tool-initiated writes (4.1) use `deferred` by default so an agent turn
never waits on an LLM. Correctness constraints: reads must see the raw record immediately; enrichment is
idempotent and replayable from the event log (already how pipelines work, `pipelines.py:1-9`); a
`flush()` barrier for evals so benchmark runs are not racy. Benefit: latency and throughput, no benchmark
score effect. Risk: eventual consistency of derived facts; test with the flush barrier. Resource: M, no dependency.

### 4.7 Optional framework adapters (I61)

Each adapter is a thin module, imported lazily, in its own extra (`[langgraph]`, `[llamaindex]`,
`[openai-agents]`), or a separate distribution in the Zep style (`zep-langgraph` is its own package,
`integrations/langgraph/python/pyproject.toml:2`). They sit on the 4.1 dispatcher, so they inherit the guard.

| Adapter | Shape | Note |
|---|---|---|
| LangGraph | `MemspineStore(BaseStore)`: `put/get/delete` map to write/correct/forget with the namespace tuple joined into a MemSpine namespace; `search` to `Engine.search`; `batch` loops ops; `list_namespaces` from the namespace table. Plus `pre_model_hook` / persist helpers like Zep's `hooks.py:55`, `persistence.py:197` | BaseStore is a subclass-required ABC, so this one must import langgraph |
| LlamaIndex | `MemspineMemoryBlock(BaseMemoryBlock)`: `_aget` runs search, `_aput` writes ejected short-term turns, inside LlamaIndex `Memory` which already does token-budgeted truncation by `priority` | better fit than `BaseMemory` |
| OpenAI Agents SDK | `MemspineSession` implementing the `Session` protocol (structural, so no import needed at runtime); `add_items` calls `write_messages`, `get_items` returns recent turns; long-term memory is exposed as function tools from 4.1 | Session is history, not long-term memory |

Benefit: adoption; none for the benchmarks. Risks: version drift (pin ranges per extra and test with stubs),
semantic mismatch (BaseStore values are arbitrary JSON; MemSpine records are text plus structure, so store
the JSON under a typed field and index its text). Resource: about 150-250 lines each, tests with fake
framework stubs, no core dependency.

## 5. Out of scope and rejected

- Building on LangChain or LangGraph: only LangMem does, and its extraction quality depends on trustcall and
  a heavy dependency tree; it would break the light-core rule.
- Letta-style heartbeat agent loop as the memory manager: a model-run memory editor on every turn costs more
  than the deterministic pipelines and has weaker safeguards.
- Exposing destructive bulk verbs to the model (Agno `clear_memory`, MemOS `delete_all_memories`).
- Mem0 v1 per-fact ADD/UPDATE/DELETE/NOOP tool-call round trip: Mem0 itself moved to additive extraction
  in v2 (`main.py:944-951`); MemSpine already has a rule/decider-based update path.
- Swapping litellm for a native client now: possible later behind the same Protocol; separate decision.

## 6. Open questions

1. Which model and prompt format handles tool or action JSON reliably on the Qwen stack? (Needs a GPU
   screen; none was run for this study.)
2. Does `relevance_gate: store_calibrated` already close the OP-Bench irrelevance gap? If yes, 4.3 reduces to
   a documentation item.
3. Is MCP-origin trust capping (same as REST) correct for a single-user desktop where the user is the only
   caller? A `trusted_local` profile may be wanted; default stays capped.
