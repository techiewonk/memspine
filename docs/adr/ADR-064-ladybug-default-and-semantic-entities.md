# ADR-064: LadybugDB as the default graph engine; embedded entity nodes

- **Status:** accepted
- **Date:** 2026-10-08
- **Decision id:** D-88. Research repo: `paper_spine/evaluation/GRAPH_ENGINE_PLAN_2026-10-08.md`, items GR-1, GR-3, GR-5, GR-6. Owner request: "make sure we are using ladybug" and Graphiti-style semantic graph search.

## Context

ADR-034 named LadybugDB (the maintained Kùzu fork) the graph engine, but `graph.provider` stayed `sqlite_adjacency`. Graph features therefore ran on SQL adjacency lists unless a deployment opted in. Graph nodes carried no embeddings, so graph search could only start from entity names written in the question.

Graphiti (`getzep/graphiti`, Kùzu driver) embeds entity names and fact text, keeps FTS indexes, and searches nodes by cosine + BM25 + BFS.

A probe of LadybugDB 0.21.2 on Windows (2026-10-08) found:

| Feature | Result |
|---|---|
| FTS index | stable |
| `array_cosine_similarity` in a query | stable |
| HNSW vector index (`CREATE_VECTOR_INDEX`) | **crashes the process at random** (2 of 5 runs) |
| Update of a row covered by an FTS index | **crashes** |
| `QUERY_FTS_INDEX` with any parameter | **crashes** |

## Decision

1. **GR-1:** `graph.provider` defaults to `auto`: LadybugDB when the `ladybug` package is installed, else `sqlite_adjacency`. On a postgres backend, `auto` picks LadybugDB, because SQL adjacency needs SQLite. Explicit values still pin a provider.
2. **GR-3 / GR-5:** `graph.entity_embeddings` (off) embeds entity-node names with the engine embedder:
   - LadybugDB stores them in an `EntityVec` table (`node_id`, `namespace`, `text`, `emb FLOAT[d]`) with its native FTS index;
   - sqlite_adjacency stores them in node properties.
3. **Working around the three crashes:**
   - no HNSW index: ranking uses exact in-engine cosine filtered to the namespace;
   - rows are inserted once, never updated: an entity id derives from its canonical name, so its text and vector are fixed;
   - FTS queries are fully literal, with the text reduced to `[a-z0-9]` tokens and the namespace grammar admitting no quotes.
4. **GR-6:** `read.graph_node_search` (off) adds an RRF leg named `graph_nodes`: entity nodes ranked by cosine + text match (RRF), then the records that mention them.

## Consequences

- Graph features use LadybugDB wherever it is installed. The LadybugDB/SQLite parity tests and the wiring tests cover both providers.
- Entity rows are deleted with their node and cleared on rebuild (`clear()`), so rebuild == incremental and erasure leaves no entity vector behind.
- A future LadybugDB release that fixes the three crashes can enable the HNSW index and parameterised FTS. The probe scripts are reproducible from the notes in this ADR.
