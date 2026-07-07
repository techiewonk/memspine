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
