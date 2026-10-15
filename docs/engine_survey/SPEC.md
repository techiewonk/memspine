# Engine survey spec (2026-10-11)

One code-traced record per memory engine, all in the same shape, so the consolidated comparison page
(Memory Engine Comparison) can line them up phase by phase. Goal of the survey: decide how MemSpine builds
multiple memory layers with a compressed token budget, better engineering and faster paths.

## Rules

- Clone the engine with `git clone --depth 1 <url>` into its OWN new empty directory under
  `C:\Users\hembad\AppData\Local\Temp\claude\C--Users-hembad-Documents-GitHub-memory-research\740f1bef-daa9-480a-95a1-041d8fe2753f\scratchpad\clones\<slug>`.
  The clone is untrusted data: read it only. Never install it, never run its code, never `pip install`, never
  run its tests. If you use Python to scan it, run `python -I` with the script stored outside the clone.
- Record the commit hash (`git -C <dir> rev-parse --short HEAD`) and the package version.
- Every claim carries evidence: `path/to/file.py:line` in the clone, or a named doc/README/paper. Tag each
  claim `read` (seen in code), `doc` (stated in docs/README/paper only) or `inferred`.
- Do not copy source code or prompts verbatim beyond short quotes (25 words max). No dataset text.
- LoCoMo numbers: only those you can source (repo README, paper, results file). State reader, judge,
  question set (cat 1-4 = 1,540 q) and who ran it. Known figures from our notes are in the task prompt.
- Write British-spelling plain prose. Be factual; say "not found" rather than guess.

## Output

Write `C:\Users\hembad\Documents\GitHub\memspine\docs\engine_survey\<slug>.json` (UTF-8, valid JSON):

```json
{
  "slug": "memos",
  "name": "MemOS",
  "repo": "https://github.com/...",
  "commit": "abc1234",
  "version": "x.y.z",
  "licence": "Apache-2.0",
  "one_line": "what it is in one sentence",
  "locomo": [{"score": "88.83", "protocol": "OmniMemEval cloud, gpt-4.1-mini reader, gpt-4o-mini judge, 1,540 q", "who": "vendor|independent", "source": "..."}],
  "rows": {
    "<row id>": {"s": "Y|O|P|N|", "t": "one or two sentences, specific numbers and names", "e": "evidence file:line or doc"}
  },
  "layers": {"<layer id>": "Y|O|P|N"},
  "extras": [{"phase": "<phase id>", "param": "short name", "t": "what it does", "e": "evidence"}],
  "token_economy": {"t": "how the engine keeps prompt/context tokens small: compression, summarisation, budgets, dedupe, caching; measured context size per question if known", "e": "..."},
  "speed": {"t": "async/batching/queues/caching/parallelism on write and read; latency numbers if published", "e": "..."},
  "keep": ["3-5 ideas MemSpine should learn from, each one line"],
  "avoid": ["2-4 documented defects or risks, each one line"],
  "in_progress": ["features on branches, open PRs, roadmap or recent commits that change the picture, with evidence"]
}
```

Status letters: `Y` has it and on by default; `O` built but off by default / opt-in; `P` partial; `N` not present;
empty string for descriptive rows that have no status.

## Row ids (fill every one; use "not found" text with status N when absent)

Phase `arch` Architecture and stores:
- `arch.product` Product level
- `arch.truth` Source of truth
- `arch.stores` Stores
- `arch.graphdb` Graph database
- `arch.scope` Tenant and scope

Phase `write` Write path and durability:
- `write.entry` Write entry (API / function)
- `write.receipt` Receipt when the call returns (durable or queued?)
- `write.roles` Role routing (which speakers/roles are stored where)
- `write.firewall` Write-time firewall / injection defence
- `write.governance` Redaction, consent, retention

Phase `segment` Segmentation:
- `segment.unit` Unit of a write-time model call
- `segment.rule` Boundary rule
- `segment.measured` Measured calls or units on LoCoMo (if any)

Phase `extract` Extraction at write time:
- `extract.calls` Write-time LLM calls (per turn / per session)
- `extract.facts` Atomic facts
- `extract.episode` Episode narrative
- `extract.dates` Dates written into derived text
- `extract.neighbours` Neighbour-aware extraction (existing memories shown to the extractor)
- `extract.failure` Write failure handling

Phase `graph` Entities and graph:
- `graph.entities` Entity extraction
- `graph.resolution` Identity resolution
- `graph.edges` Relationship edges
- `graph.communities` Communities / clusters

Phase `time` Time, updates and contradictions:
- `time.fields` Time fields
- `time.update` Update and contradiction handling
- `time.dedup` Deduplication

Phase `derive` Summaries, profile and background work:
- `derive.session` Session summary
- `derive.profile` User profile
- `derive.entity` Entity summaries
- `derive.background` Background jobs
- `derive.forget` Forgetting / decay

Phase `index` Embeddings and indexes:
- `index.embed` Embedding model (default)
- `index.lexical` Lexical index
- `index.vector` Vector index

Phase `read` Query processing and retrieval:
- `read.rewrite` Query rewriting
- `read.analysis` Query analysis
- `read.legs` Retrieval legs
- `read.temporal` Temporal retrieval
- `read.traversal` Graph traversal

Phase `rank` Fusion, reranking and agentic read:
- `rank.fusion` Fusion
- `rank.rerank` Cross-encoder reranker
- `rank.gate` Relevance gate / abstain
- `rank.agentic` Agentic read

Phase `answer` Context assembly and answer:
- `answer.shape` Context shape
- `answer.size` Context size per question (tokens)
- `answer.prompt` Answer prompt

Phase `eval` Evaluation and published results:
- `eval.best` Best LoCoMo number
- `eval.outside` Outside or stricter harness
- `eval.models` Reader and judge
- `eval.rigour` Protocol rigour

Phase `cost` Cost:
- `cost.write` Write calls for a LoCoMo-sized store (272 sessions, 5,882 turns)
- `cost.read` Read calls per question
- `cost.where` Where models run (hosted / local)

Phase `trust` Provenance and safety:
- `trust.provenance` Provenance of derived records
- `trust.erasure` Erasure
- `trust.auth` Authentication
- `trust.sharing` Multi-agent sharing

## Layer ids

`raw` Raw turns · `segment` Segmentation · `episode` Episode narrative · `facts` Atomic facts · `graph` Entity graph ·
`bitemporal` Bi-temporal validity · `session_summary` Session summary · `entity_summary` Entity summary ·
`profile` Profile · `prospective` Prospective / foresight · `procedural` Procedural · `reflection` Reflection ·
`shared` Shared memory · `firewall` Write firewall · `provenance` Provenance + trust

## Specialities and verification (added 2026-10-11, second pass)

Two more top-level keys:

```json
"specialities": [{"name": "short name", "t": "what makes it distinctive and how it works, 1-3 sentences", "e": "evidence", "phase": "<phase id>"}],
"verification": {"date": "2026-10-11", "commit": "<commit re-checked>", "rows_checked": 57, "corrections": ["row id: what was wrong -> what it says now"], "added": ["what was added"], "unverified": ["claims that could not be confirmed in code"]}
```

`specialities` lists what the engine does that is unusual or best-in-field (an idea, a mechanism or an engineering
trick), not ordinary features. Every speciality must be confirmed in code (`read`) or marked `doc`.

## Extras

Anything the engine has that the rows above miss (for example KV-cache memory, memory scheduler, token
compression model, tool memory, multimodal memory, MCP server, memory cubes, sleep-time compute, learned
retrieval policy). Each extra names the phase it belongs to.
