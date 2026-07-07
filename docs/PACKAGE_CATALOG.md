# Package Catalog — every dependency across `D:\mem`, with "does what"

**Status:** Reference · Generated 2026-07-07 from a code-level parse of all `pyproject.toml` (core + optional + poetry groups) and `requirements*.txt` across the 27 repos.
**Scale:** **564 unique packages.** Several repos (LightMem, SimpleMem, telemem, MemoryBear, MemOS, mofsl-simplememory) ship *lockfile-style* requirements that pin transitive deps, so the raw count is inflated by infrastructure packages — these are grouped under §Z so the signal (direct, meaningful deps) stays readable.
**How to read:** Part A = **master glossary** (every package appears once, grouped by function, with a one-line "does what" and a ⭐ where it is relevant to `memspine`). Part B = **per-repo listing** (each repo's full package set; look up any name in Part A). Counts in `()` = how many repos use it.

---

# PART A — Master glossary (grouped by function)

## A1. LLM provider SDKs & gateways
- **openai** (22) — OpenAI (and OpenAI-compatible) API client; the near-universal LLM/embedding client. ⭐ `services/llm/local.py`
- **anthropic** (9) — Anthropic Claude API client.
- **litellm** (8) — unified gateway over 100+ LLM providers behind one API. ⭐ D-33 (optional adapter only)
- **ollama** (6) — client for locally-served open models. ⭐ `services/llm/local.py`
- **google-genai** (6) / **google-generativeai** (3) — Google Gemini API clients.
- **groq** (4) — Groq LPU inference API client.
- **together** (2) — Together.ai hosted open-model inference.
- **vertexai** (2) — Google Vertex AI SDK.
- **dashscope** (2) — Alibaba Qwen/DashScope inference API.
- **cohere** (2) — Cohere LLM + rerank API.
- **voyageai** (2) — Voyage AI embeddings/rerank.
- **mistralai** (1) / **mistral-common** (1) — Mistral API client + shared tokenizer utils.
- **zai-sdk** (1) — Zhipu AI (GLM) SDK.
- **volcengine-python-sdk** (3) — ByteDance Volcano Engine (Doubao) SDK.
- **lazyllm** (1) — LLM-application framework (memU).
- **fnllm** (1) — function-oriented LLM wrapper (Second-Me).
- **llama-cpp-python** (1) — Python bindings for llama.cpp local inference.
- **xinference-client** (1) — Xorbits Inference server client.
- **zep-cloud** (1) — Zep memory cloud client.

## A2. LLM orchestration / agent frameworks
- **langchain-core** (11), **langchain** (7), **langchain-community** (3), **langchain-openai** (9), **langchain-anthropic** (5), **langchain-aws** (5), **langchain-text-splitters** (3), **langchain-ollama** (1), **langchain-mcp-adapters** (1) — LangChain framework + provider/integration packages (splitters = chunking). ⭐ unimem integrations
- **langgraph** (10), **langgraph-checkpoint** (3), **langgraph-prebuilt** (2), **langgraph-sdk** (2) — graph-based agent state machines + persistence.
- **langmem** (2) — LangChain-native long-term memory primitives.
- **langsmith** (8) — LangChain tracing/eval SaaS client.
- **trustcall** (3) — reliable structured extraction/patching for LangGraph (tenacious JSON). ⭐ D-31 alt
- **mcp** (11), **fastmcp** (7) — Model Context Protocol SDK + fast server framework. ⭐ D-06 (MCP reserved)
- **claude-agent-sdk** (3) — Anthropic agent SDK (hooks/skills).
- **agentscope** (1), **agno** (2), **crewai** (2), **autogen-core** (1), **ag2** (1), **strands-agents** (2), **dify_plugin** (2), **pydantic-ai-slim** (1) — multi-agent / agent-app frameworks.
- **instructor** (2) — structured LLM output via pydantic schemas + retries. ⭐ **D-31 default**
- **json-repair** (3) — repairs malformed LLM JSON. ⭐ **D-31 safety net**
- **openapi-pydantic** (1) — OpenAPI schema models.
- **guidance/dydantic** (2) — `dydantic` builds dynamic pydantic models from schemas (SimpleMem).

## A3. Embeddings & rerankers
- **sentence-transformers** (14) / **sentence_transformers** (1) — SBERT embeddings + cross-encoder rerankers (torch). ⭐ optional `[st]` extra
- **fastembed** (2) — ONNX CPU-light embeddings (no torch). ⭐ **D-08 default embedder**
- **onnxruntime** (4) — ONNX inference runtime (fastembed's engine). ⭐ under fastembed
- **flashrank** (1) — lightweight cross-encoder reranker (ONNX). ⭐ **ADOPTED as the `[rerank]` extra** (E8/D-51/ADR-017): fastembed's `TextCrossEncoder` rides the core dep when the installed version ships it; flashrank is the fallback when it does not (or upgrade fastembed).
- **voyageai/cohere** — hosted embeddings/rerank (see A1).
- **model2vec** — (referenced in analysis, not a pinned dep) static embeddings.
- **igraph** (1), **graspologic** (1) — graph embedding / spectral methods.

## A4. Vector stores & ANN indexes
- **lancedb** (5) — disk-native columnar vector store **with built-in Tantivy FTS**. ⭐ **D-09 default vector**
- **pylance** (3) / **lance-namespace** (2) / **lance-namespace-urllib3-client** (2) — Lance columnar format runtime + namespace client (LanceDB substrate). ⭐
- **faiss-cpu** (6) / **faiss-gpu** (1) — Facebook AI similarity search (in-process ANN index).
- **qdrant-client** (7) — Qdrant vector DB client.
- **chromadb** (6) — embedded/served vector DB.
- **pgvector** (9) — Postgres vector extension bindings. ⭐ prod swap-in 🔁
- **pymilvus** (2) — Milvus vector DB client.
- **hnswlib** (2) — HNSW graph ANN index.
- **usearch** (1) — compact SIMD vector search.
- **sqlite-vec** (1) — SQLite vector extension.
- **nano-vectordb** (2) — minimal file-based vector DB.
- **weaviate-client** (2) — Weaviate vector DB client. ⭐ prod swap-in 🔁 (unimem)
- **pinecone** (1) / **pinecone-text** (1) — Pinecone managed vector DB + sparse encoders.
- **upstash-vector** (1) — serverless vector DB.
- **turbopuffer** (1) — serverless vector DB (honcho).
- **vecs** (1) — Supabase pgvector helper.
- **azure-search-documents** (1) — Azure Cognitive Search.
- **redisvl** (1) — Redis vector library.
- **pymochow** (1) — Baidu Mochow vector DB.
- **pyseekdb** (1) / **pyobvector** (1) — SeekDB / OceanBase vector clients (powermem single-stack). 
- **obstore** (1) — object-store-backed storage (hindsight).
- **pg0-embedded** (1) — embedded Postgres (hindsight tests).

## A5. Graph databases & graph libs
- **neo4j** (8) — Neo4j graph DB driver (Cypher). ⭐ prod swap-in 🔁
- **kuzu** (3) — embedded columnar Cypher graph DB. ⭐ **D-26 supported alt** (graphiti, cognee)
- **falkordb** (2) — Redis-based graph DB.
- **nebula5-python** (1) — NebulaGraph client (MemMachine).
- **graphiti-core** (3) — bitemporal knowledge-graph engine (getzep). ⭐ reference bitemporal model
- **lightrag-hku** (1) — lightweight graph-RAG (cognee).
- **networkx** (7) — in-memory graph algorithms. ⭐ associative fallback math
- **rdflib** (3) — RDF triplestore + SPARQL (ontology memory).
- **sparqlwrapper** (1) — SPARQL endpoint client (memonto).
- **owlready2** (1) — OWL ontology reasoning (MemoryBear).
- **graphviz** (1) — graph rendering (memonto).
- *(LadybugDB — the memspine default graph, D-26 — is not present in any surveyed repo; it's the out-of-box-supported choice with kuzu as the mature alt.)*

## A6. Lexical / full-text / tokenization
- **rank-bm25** (8) / **rank_bm25** (2) — pure-Python BM25 ranking. ⭐ D-25 lexical
- **tantivy** (2) — Rust Lucene-class full-text index (Python bindings). ⭐ `services/lexical/tantivy_index.py`
- **jieba** (5) / **rjieba** (1) — Chinese word segmentation (Rust variant = rjieba). ⭐ **D-34 config-gated**
- **rake-nltk** (1) — keyword extraction.
- **nltk** (9) — classic NLP toolkit (tokenize/stem/stopwords).
- **xpinyin** (1), **hanziconv** (1), **cn2an** (1), **pypinyin** — CJK text normalization (MemoryBear).
- **editdistance** (1) / **datrie** (1) — string distance / trie (MemoryBear fuzzy match).

## A7. Ingest, parsing & document extraction
- **markitdown** (2) — any-format → Markdown (docx/pdf/pptx/xls/html). ⭐ **D-29 ingest**
- **chonkie** (2) — fast pluggable text chunking. ⭐ **D-29 chunking**
- **docling** (1) — advanced document conversion/layout (cognee).
- **unstructured** (1) — multi-format document partitioning.
- **markdownify** (3) — HTML → Markdown.
- **mammoth** (3) — .docx → HTML/text.
- **mistletoe** (1) — fast CommonMark parser/AST (ReMe).
- **python-frontmatter** (1) — YAML-frontmatter Markdown parsing (ReMe).
- **beautifulsoup4** (4) / **soupsieve** (1) / **lxml** (3) / **html5lib** (1) — HTML/XML parsing.
- **pypdf** (3) / **pypdf2** (1) / **pdfplumber** (3) / **pdfminer-six** (1) / **pymupdf** (1) — PDF text/table extraction.
- **python-docx** (1) / **python-pptx** (2) / **openpyxl** (2) / **xlrd** (2) / **xlsxwriter** (1) / **python-calamine** (1) / **et-xmlfile** (1) / **olefile** (1) — Office file read/write.
- **pytesseract** (1) — OCR wrapper (Second-Me).
- **magika** (1) — ML file-type detection (MemOS).
- **filetype** (1) — file-type sniffing by magic bytes.
- **python-magic-bin** (1) — libmagic bindings (Windows) ⭐ note Windows-guarded.
- **markdown** (1) / **markdown-to-json** (1) / **markdown-it-py** (2) / **mdurl** (2) — Markdown render/convert.
- **defusedxml** (2) — safe XML parsing.
- **tika** (1) — Apache Tika document extraction (MemoryBear).
- **decord** (1) / **pysrt** (1) — video frame / subtitle parsing (LightMem multimodal).
- **soundfile** (1) / **librosa** (1) — audio I/O + features (SimpleMem/Omni).
- **opencv-python** (2) / **opencv-contrib-python** (1) / **opencv-python-headless** (1) — computer vision.
- **yt-dlp** (1) / **yt_dlp** (1) — video download (telemem).
- **pillow** (6) — image processing.
- **gdown** (1) — Google Drive download (cognee evals).
- **tavily-python** (1), **protego** (1), **playwright** (2) — web search / robots.txt / headless browser (scraping).
- **tweepy** (1) — Twitter API (cognee).
- **notion-client** (1) / **pygithub** (1) / **google-api-python-client** (1) / **msal** (2) / **msal-extensions** (1) — connector clients (OpenMemory dashboard).
- **wxpy** (1) — WeChat bot (Second-Me).

## A8. Dedup, hashing & near-duplicate
- **datasketch** (1) — MinHash / LSH near-duplicate detection at scale. ⭐ **D-27 dedup stage-1**
- **xxhash** (3) / **mmh3** (1) — fast non-crypto hashing (fingerprints/simhash).
- **fastuuid** (2) — fast UUID generation.
- **nanoid** (1) — compact unique IDs (honcho).

## A9. NER / entity & ML extraction
- **gliner2** (1) — zero-shot NER (entities+relations), CPU ONNX. ⭐ **D-28 local NER**
- **spacy** (1) — industrial NLP pipeline / NER (mem0).
- **word2number** (1) / **roman-numbers** (1) / **simpleeval** (1) — value normalization (MemoryBear).
- **langdetect** (1) — language detection.

## A10. Storage / relational / ORM / migrations
- **sqlalchemy** (15) — the dominant Python ORM/SQL toolkit. ⭐ (memspine uses raw numbered SQL by choice)
- **sqlmodel** (2) — pydantic-on-SQLAlchemy models. ⭐ D-anti (rejected for core)
- **alembic** (8) — SQLAlchemy schema migrations.
- **aiosqlite** (3) — async SQLite. ⭐ storage/sqlite candidate
- **psycopg** (4) / **psycopg2-binary** (10) / **psycopg2** (1) / **psycopg-pool** (2) / **asyncpg** (3) — Postgres drivers/pools. 🔁
- **pymysql** (3) / **aiomysql** (1) — MySQL drivers.
- **pymongo** (2) — MongoDB driver.
- **cassandra-driver** (1) — Cassandra driver.
- **sqlalchemy-cockroachdb** (1) — CockroachDB dialect.
- **sqlglot** (1) — SQL parser/transpiler (powermem).
- **dbutils** (1) — DB connection pooling.
- **databricks-sdk** (1) — Databricks SQL.
- **dlt** (1) — data-load pipelines (cognee).
- **s3fs** (1) / **oss2** (1) / **alibabacloud-oss-v2** (1) — object storage filesystems (S3/Alibaba OSS).

## A11. Caching & KV
- **redis** (8) / **valkey** (2) — Redis / Valkey clients. ⭐ prod cache swap-in 🔁
- **cashews** (1) — async cache framework w/ Redis (honcho). ⭐ E3 reference
- **diskcache** (3) / **diskcache-stubs** (1) — disk-backed cache. ⭐ E3 candidate
- **fakeredis** (1) — in-memory Redis for tests.
- **py-key-value-aio** (1) / **py-key-value-shared** (1) — KV abstraction (MemOS).
- *(LMDB — memspine D-09 cache default — is not used by surveyed repos; it's the chosen embedded KV.)*

## A12. Web frameworks / API / transport
- **fastapi** (18), **fastapi-cli** (3), **fastapi-cloud-cli** (1), **fastapi-pagination** (2), **fastapi-users** (1) — ASGI web framework + extensions. ⭐ **D-06 REST (protocols/rest)**
- **uvicorn** (16) / **uvloop** (4) / **gunicorn** (1) — ASGI servers / event loop / WSGI server.
- **starlette** (3), **sse-starlette** (1) — ASGI toolkit + server-sent events.
- **flask** (2), **flask-pydantic** (1), **flask-sock** (1), **python-fasthtml** (1) — Flask + FastHTML web stacks.
- **httpx** — async HTTP client (transitive/direct). ⭐ `clients/http.py`
- **aiohttp** (6) + **aiohappyeyeballs/aiosignal/frozenlist/multidict/yarl/propcache** — async HTTP client stack.
- **requests** (11) / **requests-toolbelt** (3) / **requests-oauthlib** (1) — sync HTTP.
- **websockets** (4) / **websocket-client** (1) / **wsproto** (1) / **cloudevents** (1) — WS + event transport.
- **slowapi** (2) — FastAPI rate limiting.
- **livekit-agents** (1) / **livekit-plugins-noise-cancellation** (1) — realtime voice agents (memobase).
- **tornado** (1) — async web server (hindsight).
- **streamlit** (2) — data-app UI.
- **textual** (1) / **rich** (8) / **rich-rst** (1) / **rich-toolkit** (1) — TUI + rich terminal rendering. ⭐ typer/rich CLI
- **typer** (6) / **typer-slim** (1) / **cyclopts** (1) / **click** (7) + click-* / **shellingham** (2) — CLI frameworks. ⭐ **D-04 CLI (typer)**
- **pyfiglet** (1) — ASCII banners.

## A13. Background work / scheduling / messaging
- **celery** (1) + **kombu/amqp/billiard/vine/flower** — distributed task queue + monitor (MemoryBear). ⭐ D-anti (rejected; D-16)
- **apscheduler** (2) — in-process scheduler.
- **schedule** (1) — simple interval scheduler.
- **croniter** (1) — cron expression parsing (ReMe auto_dream).
- **pika** (1) — RabbitMQ client.
- **ray** (1) — distributed compute (LightMem).
- **modal** (1) — serverless compute (cognee).
- **taskiq / dbos** — memspine's chosen runners (D-16); not in surveyed repos.

## A14. Observability / logging / telemetry
- **structlog** (4) — structured logging. ⭐ **D-04/M11 default logger**
- **loguru** (2) — ergonomic logging (ReMe, memonto).
- **prometheus-client** (3) / **prometheus_client** (2) — Prometheus metrics. ⭐ **observability/metrics.py**
- **opentelemetry-api/sdk** (5) + exporters (otlp-http, otlp-grpc, prometheus) + instrumentation-fastapi + semantic-conventions + proto — OTel tracing/metrics. ⭐ **observability/exporters.py (OTel bridge)**
- **langfuse** (3) — LLM-trace observability. ⭐ **observability/exporters.py (Langfuse export)**
- **sentry-sdk** (3) — error tracking.
- **posthog** (7) — product analytics/telemetry.
- **wandb** (1) — experiment tracking (LightMem).
- **coloredlogs** (2) / **humanfriendly** (2) — log formatting.
- **concurrent-log-handler** (2) — safe rotating logs.

## A15. Config / settings / serialization / validation
- **pydantic** (22) / **pydantic-core** (3) / **pydantic_core** (5) / **pydantic-settings** (8) / **pydantic-extra-types** (1) / **annotated-types** (6) — data models + env-config. ⭐ **D-04/D-11 core stack**
- **python-dotenv** (19) / **dotenv** (3) — .env loading. ⭐ config layering
- **pyyaml** (13) / **ruamel.yaml** (1) / **hjson** (1) — YAML/JSON-with-comments. ⭐ template configs
- **orjson** (5) / **ujson** (1) / **ormsgpack** (2) / **msgpack** / **cbor2** (1) / **demjson3** (1) — fast (de)serialization.
- **jsonschema** (4) + **jsonschema-specifications/-path** / **referencing** (4) / **rpds-py** (4) — JSON Schema validation.
- **jsonpatch** (3) / **jsonpointer** (3) / **jsonlines** (1) — JSON manipulation.
- **json-repair** — see A2.
- **typeguard** (2) / **beartype** (2) — runtime type checking.
- **strenum** (1) / **docstring-parser** (1) / **docstring_parser** (3) — enum/docstring utils.
- **pathvalidate** (1) / **rignore** (1) — path sanitize / gitignore parsing.

## A16. Security / auth / crypto
- **cryptography** (7) / **pyjwt** (6) / **python-jose** (2) / **passlib** (2) / **bcrypt** (3) / **ecdsa** (1) / **rsa** (2) / **pyasn1** (3) / **pyasn1_modules** (1) — crypto, JWT, password hashing.
- **authlib** (2) / **oauthlib** (1) / **requests-oauthlib** (1) — OAuth.
- **azure-identity** (5) / **azure-core** (1) / **google-auth** (3) / **boto3** (5) / **boto3-stubs** (1) / **botocore** (1) — cloud auth/SDKs. ⭐ `clients/aws.py`
- **keyring** (1) — OS credential store.
- **pyseccomp** (1) — syscall sandboxing (MemoryBear).
- **defusedxml** — see A7.

## A17. ML / tensor / training (heavy)
- **torch** (8) / **torchvision** (1) / **torchcodec** (1) / **triton** (3) — PyTorch stack. ⭐ heavy — avoid in core
- **nvidia-*-cu12** (13 packages ×3 repos) — CUDA runtime libraries (transitive to torch/GPU).
- **transformers** (11) / **tokenizers** (5) / **huggingface-hub** (5) / **huggingface_hub** (1) / **hf-xet** (5) / **safetensors** (5) / **accelerate** (3) / **datasets** (4) / **peft** (2) / **trl** (1) / **timm** (1) / **einops** (2) / **sentencepiece** (2) — HuggingFace training/inference stack.
- **vllm** (1) / **flash-attn** (1) / **ninja** (1) — high-throughput serving / fused attention / build.
- **mlx** (1) / **mlx-lm** (1) — Apple-silicon inference (hindsight).
- **gguf** (1) / **modelscope** (1) — GGUF format / ModelScope hub.
- **scikit-learn** (10) / **scipy** (5) / **numpy** (17) / **pandas** (11) — classic ML + numerics + dataframes.
- **xgboost** (1) — gradient boosting (MemoryBear).
- **qwen-vl-utils** (1) / **qwen_vl_utils** (1) — Qwen-VL helpers.
- **bert-score** (4) / **bert_score** (1) / **rouge-score** (2) / **rouge_score** (3) — text-generation eval metrics.
- **deepeval** (1) — LLM-output eval framework. ⭐ **D-35 evals only**
- **ray/wandb** — see A13/A14.

## A18. Compression
- **zstandard** (4) — Zstd streaming compression. ⭐ **D-32 cold-tier**
- **llmlingua** (3) — prompt/context compression ~20× (LightMem). ⭐ **E5 `[compress]`**

## A19. Scientific / viz / notebook (mostly evals & dashboards)
- **matplotlib** (4) / **seaborn** (1) / **plotly** (1) / **contourpy/cycler/fonttools/kiwisolver/pyparsing** — plotting stacks.
- **scikit-image** (1) — image processing (LightMem).
- **jupyterlab** (3) / **ipykernel** (4) / **ipywidgets** (1) / **notebook** (1) / **nbformat** (1) / **iprogress** (1) — notebooks.
- **mkdocs** (1) / **mkdocs-material** (2) / **mkdocs-minify-plugin** (1) / **mkdocstrings** (1) — docs site. ⭐ **D-20 mkdocs**
- **build** (1) / **hatch** (1) / **uv** (1) / **setuptools** (1) / **pyproject_hooks** (1) / **deptry** (1) — packaging/build tooling. ⭐ **D-04 uv**

## A20. Testing / linting / typing / dev
- **pytest** (18) / **pytest-asyncio** (12) / **pytest-mock** (4) / **pytest-cov** (3) / **pytest-xdist** (3) / **pytest-timeout** (1) / **pytest-playwright** (1) / **unittest** (1) — test frameworks. ⭐ **D-04 pytest**
- **ruff** (9) / **black** (3) / **isort** (3) / **flake8** (2) / **mypy** (3) / **pyright** (2) / **pylint** (1) / **pre-commit** (4) — lint/format/typecheck. ⭐ **D-04 ruff**
- **coverage** (1) / **debugpy** (1) / **testcontainers** (1) / **gitpython** (1) — coverage / debug / containerized tests / git.
- **iniconfig/pluggy** — pytest internals.

## A21. Vendor / self / niche packages
- **everalgo-user-memory / -agent-memory / -rank / -knowledge / -parser** (EverMemOS) — EverOS's closed memory algorithms (vendored wheels, not inspectable).
- **memmachine-client / -common / -server** (MemMachine), **hindsight-api / -api-slim / -client / -embed** (hindsight), **honcho-ai / honcho-cli** (honcho), **reme-ai** (ReMe), **memori** (Memori), **memu** (memU), **memobase** (memobase), **mem0ai** (4), **cognee** (1), **powermem** (1), **graphiti-core** (3) — the repos' own published packages / SDKs.
- **zep-cloud** (1) — Zep client (MemOS eval baseline).
- **volcengine/dashscope/zai/pymochow/pyobvector/pyseekdb** — region-specific China-cloud SDKs.

## Z. Transitive / infrastructure (auto-pulled; pinned in lockfile-style requirements)
These add to the raw count but carry no design signal — they arrive under `httpx`, `torch`, `fastapi`, `aiohttp`, `celery`, `opentelemetry`, etc. Grouped one-liners:

- **HTTP/async plumbing:** anyio, sniffio, h11, h2, hpack, hyperframe, httptools, httpcore, certifi, idna, charset-normalizer, urllib3, pysocks, exceptiongroup, trio, durationpy.
- **Packaging/runtime:** packaging, filelock, fsspec, typing-extensions, typing_extensions, typing-inspection, typing, importlib_metadata, importlib_resources, zipp, six, setuptools, wheel, platformdirs, more-itertools, wrapt, deprecated, deprecation, overrides, attrs, cachetools, tenacity (12 — retry helper, semi-direct), backoff, tzdata, tzlocal, python-dateutil, pytz, pendulum, babel.
- **Math/tensor transitive:** mpmath, sympy, numpy-adjacent (threadpoolctl, joblib), grpcio, protobuf, flatbuffers, googleapis-common-protos, pyarrow, py-cpuinfo, psutil.
- **CUDA:** nvidia-cublas/cuda-cupti/cuda-nvrtc/cuda-runtime/cudnn/cufft/cufile/curand/cusolver/cusparse/cusparselt/nccl/nvjitlink/nvtx-cu12.
- **Terminal/format:** colorama, pygments, wcwidth, prompt-toolkit, markupsafe, jinja2 (templating — semi-direct), mako, itsdangerous, pyperclip, shellingham.
- **Serialization/schema internals:** rpds-py, referencing, jsonschema-specifications, jsonschema-path, pathable, openapi internals, cobble, et-xmlfile, jaraco.classes/context/functools, docutils, cffi, pycparser.
- **Misc transitive:** aiohappyeyeballs, aiosignal, frozenlist, multidict, yarl, propcache, jiter, distro, dnspython, email-validator, coloredlogs, humanfriendly, hf-xet, safetensors, regex, tqdm, click-didyoumean/-plugins/-repl, requests-toolbelt, ormsgpack, ujson, orjson, pybase64, kubernetes, oauthlib, websocket-client, elastic-transport, chardet, soupsieve, olefile, shapely, pyclipper, strenum, roman-numbers, word2number, xpinyin, hanziconv, cn2an, datrie, editdistance, demjson3, ruamel.yaml, html5lib, simpleeval, pathlib, asyncio, uv, rignore, magika (dup), fastuuid, mmh3.

---

# PART B — Per-repo package listing

Every repo's full declared package set (version pins stripped, de-duplicated). Look up any name in Part A for "does what". Repos marked **(lockfile-style)** pin transitive deps, so their lists include §Z infrastructure; the design-relevant packages are called out first.

### A-mem (15) — agentic memory, Zettelkasten links
sentence-transformers, chromadb, rank_bm25, nltk, litellm, numpy, scikit-learn, openai, ollama · dev: pytest, unittest, ruff, ipykernel, pre-commit, transformers

### EverMemOS → EverOS (28) — Markdown-SoT + SQLite + LanceDB
**Design:** lancedb, aiosqlite, sqlmodel, alembic, openai, structlog, prometheus-client, typer, textual, apscheduler, watchdog, watchfiles, portalocker, jieba · **vendored algo:** everalgo-user-memory, everalgo-agent-memory, everalgo-rank, everalgo-knowledge, everalgo-parser · web/util: fastapi, uvicorn, python-multipart, pydantic, pydantic-settings, python-dotenv, pyyaml, anyio, greenlet

### LightMem (90, lockfile-style) — pluggable layers + LLMLingua compression
**Design:** llmlingua, sentence-transformers, qdrant-client, faiss-cpu (fluxmem), rank-bm25, nltk, networkx, tiktoken, hnswlib, igraph, fastmcp, ollama, vllm · **ML heavy:** torch, torchvision, torchcodec, transformers, accelerate, peft, timm, einops, flash-attn, ray, wandb, opencv-python/-contrib, scikit-image, decord, pysrt, qwen-vl-utils · +§Z transitive (anyio, h2, nvidia-*, etc.)

### MemMachine (54) — episodic/profile/working; NebulaGraph
**Design:** neo4j, nebula5-python, qdrant-client, pgvector, sqlite-vec, usearch, hnswlib, rank-bm25, sentence-transformers, instructor, json-repair, litellm, langchain-aws, langchain-text-splitters, nltk · agents: strands-agents, dify_plugin · web/dev: fastapi, uvicorn, streamlit, prometheus-client, pydantic, pytest(-asyncio), ruff, mypy, hatch, boto3, cohere, google-generativeai

### MemOS (194, lockfile-style) — richest ingest/dedup toolbox
**Design ⭐:** chonkie, markitdown, datasketch, langchain-text-splitters, pymilvus, neo4j, redis, pika, qdrant-client, rank-bm25, rake-nltk, jieba, sentence-transformers, zstandard, schedule, mem0ai, zep-cloud · ingest: mammoth, markdownify, pdfminer-six, python-pptx, openpyxl, xlrd, xlsxwriter, magika, beautifulsoup4, lxml · infra: fastapi, uvicorn, sqlalchemy, psycopg2-binary, pymysql, structlog?→no; prometheus-client, sentry-sdk, typer, diskcache, keyring · +heavy ML (torch, transformers, triton, nvidia-*) +§Z

### Memori (30) — config-driven fabric, SQL backends
**Design:** faiss-cpu, sqlalchemy, sqlalchemy-cockroachdb, psycopg, psycopg2-binary, pymysql, pymongo, rank-bm25, sentence-transformers, tiktoken, botocore · agents: agno · util: aiohttp, numpy, pandas, protobuf, grpcio, tenacity, pyfiglet, requests, python-dotenv, ruff · notebooks: ipykernel, ipywidgets, jupyterlab, iprogress

### MemoryBear (137, lockfile-style) — forgetting lifecycle
**Design:** chonkie, neo4j, rdflib, owlready2, elasticsearch(-dsl), redis, valkey, celery, flower, langchain(-community/-aws/-ollama/-openai/-mcp-adapters), langfuse, json-repair, graspologic, xgboost, onnxruntime, torch, sentence-transformers?→via HF · ingest: mammoth, markdownify, pdfplumber, python-docx, python-pptx, tika, markdown-to-json, beautifulsoup4, lxml · CJK: jieba, hanziconv, cn2an, xpinyin · web/auth: fastapi, uvicorn, sqlalchemy, psycopg2-binary, cryptography, python-jose, passlib, bcrypt · +§Z

### OpenMemory (14) — CaviraOSS reference (Python engine deps here are dashboard-side)
beautifulsoup4, mammoth, markdownify, pypdf, numpy, openai, pydantic, fastapi, uvicorn · connectors: google-api-python-client, google-auth, msal, notion-client, pygithub · *(HSG engine core is largely stdlib + numpy + an embedder; see review/OpenMemory.md)*

### ReMe (27) — "Memory as File" + auto_dream
**Design ⭐:** python-frontmatter, mistletoe, zstandard, watchfiles, croniter, faiss-cpu, neo4j, networkx, jieba, rjieba, agentscope, claude-agent-sdk · web/util: fastapi, fastmcp, uvicorn, aiofiles, loguru, rich, psutil, numpy, openai, pydantic, pyyaml · dev: pytest(-asyncio), pre-commit

### Second-Me (42) — personal AI / profile modeling
**Design:** chromadb, sentence-transformers, sqlalchemy, aiomysql, langchain, tiktoken, torch, transformers, peft, trl, sentencepiece, gguf, modelscope, fnllm · ingest: pdfplumber, pymupdf, pytesseract · web: flask(-pydantic/-sock), python-socks, wxpy · util: openai, pydantic, numpy, pandas, scikit-learn, requests, ruff · docs: mkdocs(-material)

### SimpleMem / EvolveMem / Omni (156, lockfile-style) — multi-view + self-evolving policy
**Design ⭐:** lancedb, pylance, tantivy, faiss-cpu, qdrant-client, rank_bm25/rank-bm25, llmlingua, langmem, langgraph(+checkpoint/-prebuilt/-sdk), trustcall, litellm, langchain(-core/-openai/-anthropic), sentence-transformers, zstandard, dydantic · multimodal: soundfile, librosa, torch, transformers, pillow · eval: bert-score, rouge_score · +§Z (nvidia-*, etc.)

### claude-mem (0 Python) — TypeScript CLI/plugin; no Python deps
*(all deps are npm — out of scope for the Python engine)*

### cognee (113) — ECL graph+vector; kuzu+lancedb+fastembed+instructor
**Design ⭐:** kuzu, lancedb, pylance, neo4j, networkx, rdflib, fastembed, onnxruntime, instructor, graphiti-core, lightrag-hku, nano-vectordb, chromadb, pgvector, litellm, sqlalchemy, aiosqlite, diskcache, fakeredis, redis, structlog · ingest: docling, unstructured, pypdf, beautifulsoup4, lxml, tree-sitter(-python), langchain-text-splitters · llm: anthropic, groq, mistralai, llama-index-core, llama-cpp-python, baml-py · eval/docs: deepeval, mkdocs-material, plotly, matplotlib, seaborn · infra: fastapi, uvicorn, gunicorn, alembic, asyncpg, sentry-sdk, langfuse, modal, s3fs

### graphiti (41) — getzep bitemporal KG reference
**Design ⭐:** graphiti-core, neo4j, kuzu, falkordb, gliner2, opensearch-py, sentence-transformers, transformers, voyageai, numpy · llm: openai, anthropic, groq, google-genai, langchain(-openai/-anthropic/-aws), langgraph · obs: opentelemetry-api/sdk, posthog · web/dev: fastapi(-cli), uvicorn, pydantic(-settings), pytest(-asyncio/-xdist), ruff, pyright, jupyterlab, ipykernel, boto3, azure-identity

### hindsight (81) — consolidation; slim/all packaging
**Design:** markitdown, flashrank, llama-index-core, langchain(-core/-text-splitters), langgraph, litellm, pgvector, asyncpg, obstore, pg0-embedded, sentence-transformers, transformers, torch, mlx(-lm), einops, cohere · agents: ag2, agno, crewai, autogen-core, strands-agents, claude-agent-sdk, pydantic-ai-slim, dify_plugin · web/obs: fastapi, uvicorn, streamlit, tornado, python-fasthtml, opentelemetry-*, prometheus(exporter) · self: hindsight-api(-slim), hindsight-client, hindsight-embed

### honcho (38) — production-shaped; lancedb+cashews+langfuse+json-repair
**Design ⭐:** lancedb, pyarrow, turbopuffer, pgvector, psycopg, redis, cashews, langfuse, json-repair, sentry-sdk, scikit-learn, tiktoken, crewai, langgraph, langchain-core · ingest: pdfplumber · web: fastapi(-pagination), typer, rich, nanoid, cloudevents, prometheus_client · self: honcho-ai, honcho-cli

### langmem (8) — LangChain-native memory
langchain(-core/-openai/-anthropic), langgraph(-checkpoint), langsmith, trustcall

### mem0 (71) — ADD/UPDATE/DELETE/NONE infer; broadest backend matrix
**Design ⭐:** fastembed, qdrant-client, chromadb, pgvector/vecs, weaviate-client, pymilvus, faiss-cpu, pinecone(-text), upstash-vector, azure-search-documents, elasticsearch, opensearch-py, redis/redisvl/valkey, spacy, sentence-transformers, sqlalchemy · llm: openai, anthropic, groq, together, ollama, litellm, vertexai, google-genai/-generativeai, langchain(-community/-aws) · web: fastapi(-pagination), uvicorn, slowapi, typer, rich · db: psycopg(-pool)/psycopg2-binary, pymongo, pymysql, cassandra-driver, databricks-sdk, pymochow, dbutils

### memU (17) — py3.13 floor; sqlmodel
openai, pydantic, sqlmodel, sqlalchemy, alembic, pgvector, pendulum, numpy, defusedxml, langchain-core, langgraph, lazyllm, claude-agent-sdk, fastapi, uvicorn, python-dotenv, memu

### memobase (23) — profile-centric; realtime voice
**Design:** pgvector, sqlalchemy, psycopg2-binary, redis, tiktoken, structlog, openai · realtime: livekit-agents, livekit-plugins-noise-cancellation · obs: opentelemetry-api/sdk/exporter-prometheus/instrumentation-fastapi · web: fastapi, pydantic, rich, mcp, memobase, volcengine-python-sdk

### memonto (11) — ontology/RDF/SPARQL (stale 2024)
rdflib, sparqlwrapper, graphviz, openai, anthropic, chromadb, tiktoken, pydantic, loguru, black, pytest

### memory-opensource (0 Python) — Papr OSS; no parseable Python manifest here
*(deps declared elsewhere / TS surface)*

### mofsl-datascience-graphiti-repo (42) — older graphiti clone (superseded by standalone graphiti)
Same shape as graphiti: graphiti-core, neo4j, kuzu, falkordb, opensearch-py, voyageai, sentence-transformers, transformers · llm/langchain/otel/dev identical · +diskcache(-stubs)

### mofsl-datascience-simplememory-repo (151, lockfile-style) — internal SimpleMemory variant
Near-identical to SimpleMem: lancedb, pylance, tantivy, faiss-cpu, qdrant-client, rank-bm25, llmlingua, langmem, langgraph, trustcall, litellm, zstandard, sentence-transformers +§Z

### powermem (56) — Ebbinghaus decay + hybrid + OceanBase single-stack
**Design ⭐:** pyobvector (OceanBase), pyseekdb, pgvector, psycopg(-pool)/psycopg2-binary, sqlalchemy, sqlglot, rank-bm25, jieba, sentence-transformers, slowapi · llm: openai, anthropic, ollama, together, vertexai, google-genai/-generativeai, dashscope, zai-sdk, langchain(-community/-openai), langgraph · web/dev: fastapi, uvicorn, pydantic(-settings), playwright, pytest(-asyncio/-cov/-mock/-playwright), black, isort, flake8, mypy

### telemem (134, lockfile-style) — multimodal, builds on mem0
**Design:** mem0ai, memobase, chromadb, nano-vectordb, faiss-cpu/-gpu, qdrant-client, pgvector, redis, sentence-transformers, litellm, structlog · multimodal: opencv-python-headless, yt-dlp, soundfile?→no; azure-identity/-core · obs: opentelemetry-* (otlp-grpc, prometheus, instrumentation-fastapi), posthog · +§Z (kubernetes, nvidia-free)

### unimem (9) — THE REWORK TARGET (deliberately tiny)
pydantic · extras: langchain-core, langchain-openai (llm), redis, psycopg2-binary + pgvector (postgres), weaviate-client, neo4j · dev: pytest

---

## Cross-reference: which packages back which memspine decision

| memspine decision | Package(s) | Repos proving it |
|---------------------|-----------|------------------|
| D-08 embedder = fastembed/ONNX | fastembed, onnxruntime | mem0, cognee |
| D-09 vector = LanceDB | lancedb, pylance | EverMemOS, cognee, honcho, SimpleMem |
| D-09 graph = LadybugDB (**D-26**), kuzu supported | kuzu | graphiti, cognee, mofsl-graphiti |
| D-25 lexical = BM25/Tantivy | rank-bm25, tantivy | 8 repos / SimpleMem |
| D-27 dedup = datasketch | datasketch | MemOS |
| D-28 NER = gliner2 | gliner2 | graphiti |
| D-29 ingest = markitdown+chonkie | markitdown, chonkie | MemOS, hindsight |
| D-31 structured = instructor+json-repair | instructor, json-repair | cognee/MemMachine, honcho/MemoryBear |
| D-32 cold compress = zstandard | zstandard | MemOS, ReMe, SimpleMem |
| E5 compress = llmlingua | llmlingua | LightMem, SimpleMem |
| D-33 llm gateway (adapter only) | litellm | A-mem, mem0, cognee, hindsight, telemem |
| D-34 CJK lexical (gated) | jieba, rjieba | ReMe, powermem, EverMemOS, MemOS |
| D-35 eval assertions | deepeval | cognee |
| observability (Langfuse+OTel+Prometheus) | langfuse, opentelemetry-*, prometheus-client | honcho, cognee, graphiti, telemem |

*Companion docs:* `DEPENDENCY_ANALYSIS.md` (adoption reasoning + D-26…D-35), `UNIMEM_V2_REWORK_PROPOSAL.md` (architecture), `memspine-structure-plan.md` (blueprint).
