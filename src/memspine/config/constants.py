"""Design constants. Every magic number lives here and cites its design source.

Do not tune these inline elsewhere — policies bind them via config so templates
can override per profile (D-11/D-14).
"""

from __future__ import annotations

# Hybrid retrieval fusion (D-25): reciprocal-rank-fusion constant.
RRF_K = 60

# Two-stage dedup (D-27 / M5).
DEDUP_COSINE_THRESHOLD = 0.92
MINHASH_NUM_PERM = 128
LSH_THRESHOLD = 0.6

# Associative memory (M13.6): max outgoing links per node (bounded A-MEM).
LINK_BUDGET = 12

# Reflective memory (M13.7): maximum reflection-on-reflection depth.
REFLECTION_DEPTH_CAP = 2

# Assembly (M12): abstain when the best candidate scores below this.
THETA_ABSTAIN = 0.25

# Event-log retention default for rolling mode (D-45).
EVENT_LOG_RETENTION_DAYS = 30

# Decay tiers (M3, Ebbinghaus-informed): days-without-access before transition.
DECAY_HOT_TO_WARM_DAYS = 7
DECAY_WARM_TO_COLD_DAYS = 30
DECAY_COLD_TO_DORMANT_DAYS = 90

# Memory Firewall (E1): trust assigned at write per source class; retrieved
# content is capped low so it can never masquerade as operator input.
TRUST_DEFAULT = 0.5
TRUST_RETRIEVED_CAP = 0.3
QUARANTINE_PROMOTION_CORROBORATIONS = 2
# Writes below this trust are quarantined outright (E1/M17).
QUARANTINE_TRUST_THRESHOLD = 0.25
# Embedding-outlier gate: cosine similarity to the namespace centroid below
# this (with enough neighbours to trust the centroid) is anomalous.
ANOMALY_CENTROID_MIN_SIMILARITY = 0.05
ANOMALY_MIN_NEIGHBOURS = 8
# MINJA bridging heuristic: a shared prefix this long with a recent record
# marks progressive-injection shaping.
MINJA_BRIDGE_PREFIX_CHARS = 96

# Scoring (M1): recency half-life + composite weights.
SCORING_RECENCY_HALF_LIFE_DAYS = 7.0
SCORING_IMPORTANCE_WEIGHT = 1.0
SCORING_RELEVANCE_WEIGHT = 1.0
SCORING_UTILITY_WEIGHT = 0.5

# Assembly (M12): MMR diversity/relevance balance.
MMR_LAMBDA = 0.7

# Consolidation (M2): heat trigger threshold (writes per namespace per cycle).
CONSOLIDATION_HEAT_THRESHOLD = 50

# Episodic sessions (M13.2): minutes of silence that close a session boundary.
SESSION_GAP_MINUTES = 30

# Consolidation (M2): sessions smaller than this are not worth a summary; the
# deterministic extractive fallback caps summaries at this many characters.
CONSOLIDATION_MIN_SESSION_RECORDS = 3
CONSOLIDATION_SUMMARY_MAX_CHARS = 600

# Ingest (D-29): fallback chunker target size when chonkie is not installed.
INGEST_CHUNK_MAX_CHARS = 1200

# Compression (D-32/D-45): one zstd level for cold-tier records and event
# payloads at rest, so the two never drift without an ADR.
ZSTD_LEVEL = 3

# Replay (D0.1): events per catch-up batch — bounds catch-up memory footprint.
REPLAY_BATCH_SIZE = 1000

# Working memory (M13.1): default hot-window size when a profile sets none.
WORKING_PAGE_SIZE = 16

# Plan recall (E6): a cached plan is only reused when its task's embedding
# similarity to the incoming task clears this floor — below it, no plan.
PLAN_RECALL_MIN_SIMILARITY = 0.6

# Retrieval defaults (M12): candidates fetched and context token budget.
SEARCH_TOP_K = 8
ASSEMBLE_TOP_K = 16
ASSEMBLE_BUDGET_TOKENS = 2048

# In-process KV cache (E3): entry cap for the zero-dep default backend.
MEMORY_KV_MAX_ENTRIES = 65536

# Hash test embedder: vector width (tests/CI only, never production).
HASH_EMBEDDING_DIM = 64
