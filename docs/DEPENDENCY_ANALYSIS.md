# Dependency Analysis — code-level scan of `D:\mem` for `memspine` adoption

**Status:** Evidence document · Generated 2026-07-07 from a code-level scan of every dependency manifest (`pyproject.toml`, `requirements*.txt`, `package.json`) across the repos in `D:\mem`. **Last manifest refresh:** 2026-07-07 · **Flow/taxonomy comparison refresh:** 2026-07-10 pass #3–#4 — see [`ECOSYSTEM_COMPARISON.md`](./ECOSYSTEM_COMPARISON.md) §3.10–§3.14 (**§3.12 = complete package union**) and [`exports/ECOSYSTEM_PACKAGE_ADOPTION.csv`](./exports/ECOSYSTEM_PACKAGE_ADOPTION.csv). **LadybugDB note (D-26):** ladybug v0.18.0 published PyPI 2026-07-01; memspine ships `services/graph/ladybug.py` adapter; config default remains `sqlite_adjacency`. **Vector note (ADR-021):** LanceDB is core-only; SQLite brute-force vector fallback removed.
**Purpose:** (1) inventory what every repo actually depends on, (2) say what each notable package *does*, (3) map it to *where it slots into `memspine`* (§ references are to `memspine-structure-plan.md`), and (4) flag **advanced packages worth adopting** that the current blueprint does not yet name.
**How to read the "memspine slot" column:** ✅ already the blueprint default · ➕ candidate to adopt (new) · 🔁 prod swap-in stub already reserved · 🧪 research/eval only · ➖ out of scope.

---

## 0. Executive summary — what the scan changes

The scan **validates most memspine defaults by independent adoption** and surfaces **10 advanced packages** worth an explicit decision:

| # | Package | Does what | Ecosystem adoption | memspine slot | Tier |
|---|---------|-----------|--------------------|-----------------|------|
| A1 | **kuzu** | Embedded, columnar **Cypher graph DB** (no server) | graphiti, cognee, mofsl-graphiti | `services/graph/` — candidate default **vs** the pinned LadybugDB fork (D-09) | DF |
| A2 | **datasketch** | MinHash / LSH near-duplicate detection at scale | MemOS (`pref-mem`) | `policies/dedup.py` (M5) — LSH pre-filter before cosine | QW |
| A3 | **gliner2** | Local **zero-shot NER** (entity/relation), CPU ONNX | graphiti (`gliner2` extra) | `memories/semantic/entities.py` (M13.3) — entity resolution without an LLM call | DF |
| A4 | **chonkie** | Fast, pluggable **text chunking** library | MemOS (`mem-reader`) | write-path `extract→chunk` (P1) — vs hand-rolled chunker | QW |
| A5 | **markitdown** | Multi-format → Markdown (docx/pdf/pptx/xls/html) | MemOS (`mem-reader`) | `memories/resource/extraction.py` (M13.9) — multi-format ingest | QW |
| A6 | **python-frontmatter + mistletoe + watchfiles** | Markdown-frontmatter parse + CommonMark AST + FS watch | ReMe, EverMemOS | a **FileStore / file-native profile** (the 2026 trend) | DF |
| A7 | **instructor** / **trustcall** / **baml** | **Structured LLM output** with validation + retries/patching | cognee / langmem / cognee | `services/llm` extract+judge roles (E9) — reliable JSON, fewer tokens | QW→DF |
| A8 | **zstandard** | High-ratio streaming compression | ReMe | `policies/compression.py` cold-tier (M6 / MemoryBear Dormancy→Decay) | QW |
| A9 | **json-repair** | Repairs malformed LLM JSON | honcho | infer/extract robustness — cheapest reliability win | QW |
| A10 | **llmlingua** | Prompt/context **compression ~20×** | LightMem | already reserved as `[compress]` (E5) — **confirmed by real use** | DF |

Validated-by-adoption (keep as-is): **lancedb** (EverMemOS, cognee, honcho), **fastembed/onnxruntime** (mem0, cognee — ONNX CPU embeddings), **rank_bm25 / FTS** (A-mem, powermem, SimpleMem, MemOS, ReMe), **structlog** (EverMemOS, cognee), **typer** (EverMemOS), **prometheus-client** (EverMemOS, MemOS, honcho), **pydantic-settings** (powermem, EverMemOS, cognee), **langfuse** (honcho, cognee — matches `observability/exporters.py`).

---

## 1. Cross-ecosystem adoption signal (what the field converges on)

Counting independent adoptions is the strongest evidence for a default choice.

| Capability | Package (count) | Reading |
|------------|-----------------|---------|
| **Graph DB** | neo4j (7: MemMachine, MemOS, ReMe, cognee, graphiti, mofsl-graphiti, unimem) · **kuzu (3: graphiti, cognee, mofsl-graphiti)** · NebulaGraph (MemMachine, runtime) | neo4j is the *server* reference; **kuzu is the emerging *embedded* default** — the closest analog to memspine's "LadybugDB embedded" intent, but mature and unforked |
| **Vector store** | faiss-cpu (5: Memori, ReMe, SimpleMem, telemem, LightMem) · **lancedb (3: EverMemOS, cognee, honcho)** · qdrant (2: LightMem, mem0) · pgvector (5: mem0, memU, powermem, honcho, cognee) · chromadb (4) · milvus (MemOS) · turbopuffer (honcho) · nano-vectordb (telemem) | faiss = in-proc index; **lancedb = disk-native store+FTS in one** (memspine default ✅); pgvector = the prod swap-in everyone ships 🔁 |
| **Lexical / BM25** | rank_bm25 (5) · jieba/rjieba (ReMe, powermem, EverMemOS, MemOS) · Tantivy (via LanceDB) | BM25 is universal; **jieba/rjieba = CJK tokenization** — needed only if multilingual is a goal |
| **Embedder** | sentence-transformers (6) · **fastembed (mem0)** · onnxruntime (cognee) · Titan/Cohere via Bedrock | sentence-transformers is heavy (torch); **fastembed/ONNX is the CPU-light default** (memspine D-08 ✅) |
| **LLM gateway** | openai (all) · **litellm (3: A-mem, cognee, mem0)** · ollama (4) · anthropic (4) · google-genai (4) · bedrock/boto3 | litellm is the popular *unified* provider layer — an alternative to memspine's own `services/llm` role abstraction |
| **Structured output** | instructor (cognee) · trustcall (langmem) · baml (cognee) · json-repair (honcho) | the field no longer hand-parses JSON — memspine should pick one (E9) |
| **Chunking / ingest** | chonkie + markitdown (MemOS) · langchain-text-splitters (MemOS, cognee) · pdfplumber (honcho) · unstructured (cognee) · docling (cognee) | mature multi-format ingest exists off-the-shelf |
| **Scheduler (sleep cycle)** | apscheduler (EverMemOS) · croniter (ReMe) · schedule (MemOS) · celery+flower (MemoryBear) | all alternatives to memspine's DBOS-cron / inline-timer / taskiq-beat (D-16) |
| **Config / logging / CLI** | pydantic-settings, structlog, typer, python-dotenv (EverMemOS, cognee, powermem…) | exactly memspine's core stack — **strong validation** ✅ |
| **Observability** | langfuse (honcho, cognee) · prometheus-client (3) · sentry-sdk (honcho, cognee) · opentelemetry (graphiti) | matches `observability/` (Langfuse export + OTel bridge) ✅ |
| **File-native memory** | python-frontmatter, mistletoe, watchfiles, portalocker, zstandard (ReMe, EverMemOS) | the toolchain for the Markdown/Git-native profile (new) |

---

## 2. Per-repo dependency inventory

Format: **repo** (name, py) — core deps → *notable extras*. "→ slot" notes where a dep informs memspine.

### Cognitive engines / platforms

- **cognee** (`cognee`, 3.10–3.13) — openai, litellm, **instructor** (structured output ➕E9), pydantic(-settings ✅), sqlalchemy, aiosqlite, tiktoken, **lancedb + pylance** (✅ vector), **kuzu==0.11.3** (➕graph default candidate A1), networkx, rdflib, **fastembed + onnxruntime** (✅ embedder), pypdf, structlog, diskcache (�add E3 cache), fakeredis. Extras: `[neo4j] [postgres:psycopg2+pgvector+asyncpg 🔁] [graphiti:graphiti-core] [huggingface] [chromadb] [deepeval 🧪] [docling]`. → The most complete graph+vector reference; its store choices (lancedb+kuzu) are the headline adoption signal.
- **graphiti** (`graphiti-core`, 3.10–3.13) — pydantic, neo4j, openai, tenacity, numpy, posthog. Extras: `[kuzu] [falkordb] [neptune] [gliner2] (➕local NER A3) [voyageai] [sentence-transformers] [tracing:opentelemetry]`. → Reference for bitemporal KG + hybrid search (RRF/MMR/cross-encoder); slim core, providers all optional — a packaging model to copy (D-03).
- **MemOS** (`MemoryOS`, ≥3.10, poetry) — openai, ollama, transformers, tenacity, fastapi[all], sqlalchemy, pymysql, scikit-learn, fastmcp, prometheus-client (✅). Extras: `[tree-mem:neo4j+schedule] [mem-scheduler:redis+pika] [mem-reader: chonkie (➕A4) + markitdown (➕A5) + langchain-text-splitters] [pref-mem: pymilvus + datasketch (➕A2)] [skill-mem]`. → Richest *extract/chunk/dedup* toolbox in the whole set.
- **EverMemOS→EverOS** (`everos`, ≥3.12) — pydantic(-settings ✅), **lancedb** (✅), aiosqlite, sqlmodel, alembic, openai, PyYAML, **watchdog/watchfiles** (➕A6 file-native), structlog (✅), prometheus-client (✅), typer (✅), textual, jieba, **apscheduler** (scheduler), **portalocker** (file lock ➕A6), everalgo-* (their own closed algo packages). → Proof the Markdown-SoT + SQLite + LanceDB stack works; note the core memory logic is vendored as `everalgo-*` wheels (not inspectable).
- **MemMachine** (uv workspace) — fastapi, sqlalchemy, openai; neo4j + qdrant + postgres via testcontainers; NebulaGraph runtime. → Episodic(graph)/Profile(SQL)/Working taxonomy; benchmark toolset.
- **MemoryBear** (`api`, celery stack) — fastapi, sqlalchemy, psycopg2, **celery + flower** (worker alt), redis, rdflib, cryptography/jose (auth). → Forgetting-lifecycle engine; celery is the worker they chose (memspine prefers DBOS/taskiq, D-16).

### Retrieval / lifecycle specialists

- **LightMem** (`lightmem`, 3.10–3.12) — torch, transformers, sentence-transformers, **llmlingua==0.2.2** (✅E5 compress A10), tiktoken, qdrant-client, scikit-learn, nltk, networkx, rank-bm25. Extras: `[llms: ollama, vllm] [mcp: fastmcp] [fluxmem: faiss-cpu]`. → Confirms LLMLingua compression as a real, pinned dependency, not just a paper.
- **ReMe** (`reme-ai`, ≥3.11) — aiofiles, croniter (sched), fastapi, fastmcp, **mistletoe** (Markdown AST ➕A6), **python-frontmatter** (➕A6), **zstandard** (cold compress ➕A8), loguru, pydantic, watchfiles (➕A6), **claude-agent-sdk**. Extras: `[core: agentscope, faiss-cpu, jieba, rjieba (CJK BM25), neo4j, networkx]`. → The exact toolchain for the file-native profile; also shows a background-jobs design (auto_dream) on croniter.
- **SimpleMem / EvolveMem / Omni** — openai, pyyaml, sentence-transformers, numpy; Omni adds faiss-cpu, rank_bm25, nltk, torch, transformers, **soundfile + librosa** (audio), tiktoken. → Multi-view retrieval + self-evolving policy; Omni = multimodal ingest.
- **Memori** (`memori`, ≥3.10) — aiohttp, botocore, **faiss-cpu**, numpy, pyfiglet, requests. Extras: `[sqlalchemy] [tidb-zero] [cockroachdb]`. → ~11 SQL backends via SQLAlchemy; light core; LoCoMo 81.95% @ ~1,294 tok bench.
- **powermem** (`powermem`, ≥3.11) — pydantic(-settings ✅), sqlalchemy, fastapi, slowapi (rate limit), **rank-bm25**, **pyobvector** (OceanBase single-stack vector+FTS+graph), jieba, pgvector, psycopg, many LLM SDKs (together, anthropic, ollama, vertexai, dashscope, zai, seekdb). Extras: `[extras: sentence-transformers]`. → Ebbinghaus decay + hybrid + query-rewrite; OceanBase is their "one store does all" bet.
- **hindsight** (`hindsight-all`, ≥3.11) — meta-package over hindsight-api-slim[all] + client + embed. → Consolidation with bank/mission scoping; packaged as slim+all (packaging model like graphiti/memspine).
- **telemem** (`telemem`, ≥3.10) — openai, pydantic, chromadb, opencv-headless, **nano-vectordb**, mem0ai, azure-identity, **yt_dlp** (video ingest), faiss-cpu. → Telemetry + multimodal; builds on mem0.

### LangChain-native / SDKs

- **langmem** (`langmem`, ≥3.10) — langchain(-core/-openai/-anthropic), **trustcall** (structured patching ➕A7), langgraph(-checkpoint), langsmith. → LangChain-first memory; trustcall is its reliability trick.
- **honcho** (`honcho`, ≥3.10) — fastapi, sqlalchemy, pgvector, **lancedb + pyarrow** (✅), redis, **cashews[redis]** (cache ➕E3), **langfuse** (✅ observ.), sentry-sdk, tiktoken, **json-repair** (➕A9), turbopuffer (vector 🔁), pdfplumber (ingest), scikit-learn, prometheus-client (✅). → Production-shaped; its cache + observability + json-repair choices map 1:1 to memspine E3/observability.
- **mem0** (`mem0ai`, 3.10–4.0) — qdrant-client, pydantic, openai, posthog, sqlalchemy, protobuf. Extras: `[nlp: spacy (NER ➕A3-alt)] [vector_stores: 15+ incl. weaviate🔁, pgvector🔁, faiss, valkey🔁] [llms: litellm, groq, together, ollama…] [extras: fastembed (✅), sentence-transformers, langchain, elasticsearch]`. → **SDK v3 (pkg 2.0.2): ADD-only infer** + hybrid BM25+semantic search on hot path; broadest backend matrix (proof of the capability-manifest approach, M14). *(Flow re-verified 2026-07-10 — Neo4j/graph removed from OSS v3.)*
- **A-mem** (`agentic-memory`, ≥3.8) — sentence-transformers, **chromadb**, rank_bm25, nltk, **litellm**, scikit-learn, openai. → Zettelkasten link-evolution; litellm gateway.
- **memU** (`memu-py`, ≥3.13) — openai, pydantic, sqlmodel, alembic, pendulum, langchain-core, **lazyllm**. Extras: `[postgres: pgvector🔁] [langgraph] [claude: claude-agent-sdk]`. → 3.13 floor like memspine (D-02 ✅).
- **memobase** (reqs) — pydantic, httpx, openai (thin client). → Profile-centric; minimal.
- **memonto** (`memonto`, poetry) — pydantic, **rdflib**, openai, graphviz, tiktoken, **sparqlwrapper**, anthropic, chromadb, loguru. → Ontology/RDF/SPARQL memory; **stale (2024)** — reference only.
- **unimem** (`unimem`, ≥3.10) — pydantic only (core). Extras: `[langchain] [redis] [postgres:psycopg2-binary+pgvector] [weaviate] [neo4j] [llm:langchain-openai]`. → The rework target; note the deliberately tiny core — the model memspine extends.

### Node / TS surfaces (not Python-relevant, noted for completeness)
- **OpenMemory/dashboard**, **claude-mem** (TS CLI/plugin), **mem0-ts**, **memori-ts**, **honcho/mcp**, **hindsight-*-npm/control-plane** — JS/TS front-ends, MCP servers, SDKs. → No bearing on the Python engine; ignore for dependency adoption.

---

## 3. Advanced packages — adopt / evaluate / reject (the decision list)

Each row is a concrete proposal with a default recommendation. These feed the enhanced proposal's decision register.

| ID | Package(s) | Where in memspine | Recommendation | Rationale |
|----|-----------|---------------------|----------------|-----------|
| **A1** | **kuzu** | `services/graph/kuzu.py` | **Adopt as default embedded graph**; keep LadybugDB as an alt, sqlite_adjacency as zero-dep fallback | Embedded, Cypher, columnar, actively maintained; chosen by both graphiti and cognee. Removes the risk of depending on a *pinned fork* (D-09) for the graph headline. |
| **A2** | **datasketch** (MinHash/LSH) | `policies/dedup.py` (M5) | **Adopt** as the scale pre-filter stage-1 (LSH) before cosine stage-2 | Two-stage detect (M5) needs an O(1) candidate generator; MinHash-LSH is the proven one (MemOS pref-mem). |
| **A3** | **gliner2** (or spaCy) | `memories/semantic/entities.py` (M13.3) | **Adopt gliner2** behind a config flag for local NER; LLM extraction stays the default fallback | Entity resolution before conflict detection is on the hot write path; a CPU NER pass avoids an LLM call per write. graphiti ships it as an extra. |
| **A4** | **chonkie** | write-path `extract→chunk` (P1) | **Adopt** as the chunker (extra `[ingest]`) | Purpose-built, fast, pluggable strategies; avoids hand-rolling root-child chunking. |
| **A5** | **markitdown** | `memories/resource/extraction.py` (M13.9) | **Adopt** for multi-format→text (extra `[ingest]`) | docx/pdf/pptx/xls/html in one lib; MemOS uses exactly this. Only the extracted text is indexed (PII tier preserved). |
| **A6** | **python-frontmatter + mistletoe + watchfiles + portalocker + zstandard** | new `services/file/` + `profile="file"` | **Adopt** to realize the file-native profile (the 2026 trend) | This is the ReMe/EverOS stack verbatim; enables Git-diffable, human-auditable memory with a background `auto_dream` consolidation. |
| **A7** | **instructor** (default) / trustcall / baml | `services/llm` extract+judge roles + E9 | **Adopt instructor** for structured output; reject baml (heavy DSL) | Reliable typed output with retries; pairs with YAML/CoD token savings (E9). trustcall only if we go LangGraph-native. |
| **A8** | **zstandard** | `policies/compression.py` cold tier (M6) | **Adopt** for cold-memory compression/fingerprinting | MemoryBear Dormancy→Decay wants byte-level compression; zstd is the streaming standard. |
| **A9** | **json-repair** | infer/extract path | **Adopt** — one-line reliability win, zero risk | Cheapest robustness improvement; honcho ships it in prod. |
| **A10** | **llmlingua** | `[compress]` (E5) | **Already reserved — confirm** | LightMem pins it; real, not aspirational. |
| **A11** | **flashrank** | new `[rerank]` extra (E8) | **Adopt as the `[rerank]` fallback** (D-51/ADR-017) | E8 cross-encoder rerank rides fastembed's `TextCrossEncoder` on the core dep where the installed version ships it; flashrank (tiny ONNX cross-encoder) is the opt-in alternative when it does not — hence a small dedicated extra rather than a core add. fastembed reranker's `MissingServiceError` names the upgrade remedy (ADR-018/CMP-2). |
| **A12** | **model2vec** | new `[static]` extra (E4) | **Adopt as the `[static]` prefilter** (D-54/ADR-020) | E4 wants a cheap static-embedding first pass to narrow candidates before the real embedder + float32 rescore. model2vec distills a sentence-transformer into a static lookup table (no transformer forward pass, no torch — pure-numpy inference), so it embeds on CPU orders of magnitude faster than fastembed at a quality cost that only has to be *directional* (the rescore restores precision). Behind an opt-in extra; default off. As a mere prefilter stage a missing extra skip-logs (retrieval degrades, never fails); chosen as the embedding *provider* it hard-fails naming `[static]` (D-10). |
| — | **litellm** | `services/llm` | **Evaluate, likely reject for core** | Convenient unified gateway (A-mem, cognee, mem0) but adds a heavy dep and hides the per-role provider control (D-07/D-22) memspine wants. Keep as an optional provider adapter only. |
| — | **jieba / rjieba** | `services/lexical` | **Config-gated** — only if multilingual (CJK) is a goal | Needed for Chinese BM25 (ReMe, powermem); dead weight otherwise. |
| — | **celery / apscheduler / schedule** | `workers/` | **Reject** — D-16 already picks inline/DBOS/taskiq | These are the alternatives the blueprint already decided against; noted for traceability. |
| — | **sqlmodel** | `services/storage` | **Reject for core** | EverMemOS/memU use it, but memspine's numbered-SQL + pydantic records (D0.1) is deliberate; sqlmodel would blur the event-log boundary. |
| — | **deepeval** | `evals/` | **Adopt in evals only** 🧪 | LLM-eval assertions for the probe harness (M15); repo-root, not shipped (D-19). |

---

## 4. Packaging & sizing lessons (from the manifests)

- **Slim-core + provider-extras is the ecosystem norm.** graphiti (7 core deps, everything else an extra), mem0 (15+ vector backends behind `[vector_stores]`), hindsight (slim vs all), cognee (30+ extras) all prove the D-03 extras-matrix model. **Keep memspine's core tiny** (pydantic + structlog + typer + fastembed) — this is validated, not risky.
- **Heavy-dep smell:** LightMem's core pins torch + vLLM (multi-GB); sentence-transformers pulls torch too. memspine's **fastembed/ONNX default (D-08) is the correct light path** — reserve torch-based embedders for an optional `[st]` extra only.
- **onnxruntime under the hood:** fastembed → onnxruntime (cognee ships it explicitly). Pin the CPU wheel; a `[gpu]` extra can add onnxruntime-gpu later (mofsl-simplememory ships a `requirements-gpu.txt` — a model for that split).
- **Windows note:** cognee guards `python-magic-bin` and `pylance` version ranges per-platform; memspine's file/ingest extras should do the same to stay Windows-clean (the user's environment is Windows).

---

## 5. Traceability — proposed new decisions (D-26 … D-35)

These are the code-level decisions the scan surfaces, written in the memspine decision-register style so they can be locked and folded into `memspine-structure-plan.md` and the unimem v2 proposal.

| # | Decision | Proposed value (pending your confirmation) |
|---|----------|--------------------------------------------|
| D-26 | Embedded graph store | **kuzu** default (`[graph]`), LadybugDB alt, sqlite_adjacency fallback |
| D-27 | Dedup engine | **datasketch MinHash-LSH** stage-1 + embedding cosine stage-2 (M5) |
| D-28 | Local entity extraction | **gliner2** behind flag; LLM extraction default fallback (M13.3) |
| D-29 | Multi-format ingest / chunk | **markitdown + chonkie** under `[ingest]` extra (P1) |
| D-30 | File-native profile | **python-frontmatter + mistletoe + watchfiles + portalocker + zstandard**, new `services/file/`, `profile="file"` |
| D-31 | Structured LLM output | **instructor** for extract/judge roles; json-repair as the always-on safety net |
| D-32 | Cold-tier compression | **zstandard** in `policies/compression.py` |
| D-33 | LLM gateway | Keep memspine `services/llm` roles; **litellm only as optional adapter**, not core |
| D-34 | Multilingual lexical | **jieba/rjieba config-gated**, off by default |
| D-35 | Eval assertions | **deepeval** in `evals/` only (D-19 boundary) |

---

*Companion documents:* `UNIMEM_V2_REWORK_PROPOSAL.md` (the architecture), `memspine-structure-plan.md` (the buildable blueprint). This file is the **evidence base for D-26…D-35**; confirm those and they fold into both.
