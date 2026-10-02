"""Audit: every planned memspine feature -> is its identifier present in src/?"""

from pathlib import Path

SRC = Path(r"D:\mem\memory research\memspine\src\memspine")
EV = Path(r"D:\mem\memory research\memspine\evals\memspine_evals")
code = "\n".join(p.read_text("utf-8") for p in SRC.rglob("*.py"))
code += "\n".join(p.read_text("utf-8") for p in SRC.rglob("*.yaml"))
evals = "\n".join(p.read_text("utf-8") for p in EV.rglob("*.py"))

FEATURES = [
    # id, description, needle(s) in engine code (all must be present), where
    ("B0", "implicit parents from the read ledger", ["implicit_parents"], code),
    ("B1", "weighted lineage", ["parent_weights"], code),
    ("B2", "tamper-evident log + offline verification", ["verify_integrity"], code),
    ("B3", "counterfactual repair", ["repair_taint"], code),
    ("B4", "revocation/quarantine propagation to descendants", ["live_reevaluation"], code),
    ("B5", "message mediation", ["async def send("], code),
    (
        "B6",
        "action gate + untrusted wrapper",
        ["async def authorize(", "untrusted_wrap_below"],
        code,
    ),
    ("B7", "per-principal reputation", ["principal_reputation"], code),
    ("B8", "secret/PII redaction", ["redact_secrets"], code),
    ("B9", "sanitise-before-summarise", ["instruction_flag"], code),
    ("C-1", "event time on write", ["valid_from"], code),
    ("C2", "asymmetric query embedding", ["query_input_type"], code),
    ("C3'", "temporal + metadata legs", ["temporal_leg", "metadata_leg"], code),
    ("C4'", "current-state view + retraction", ["current_state_view", "async def retract("], code),
    ("C5", "full-context mode", ['"full"'], code),
    ("C6'", "atomic fact mining", ["mine_facts"], code),
    ("C7'", "mode-routed read", ["async def read("], code),
    ("C8'", "firewall-governed cues", ["async def add_cues("], code),
    ("C9'", "cached-token reporting", ["cached_prompt_tokens"], evals),
    ("RR", "cross-encoder rerank", ["_rerank_provider"], code),
    ("H1", "relative-date resolution", ["resolve_relative_dates"], code),
    ("H2", "session-mining prompt", ["extract@session"], code),
    ("H3", "compose read", ["async def _compose("], code),
    ("H4", "relative floor", ["relative_floor"], code),
    ("H5", "dated render", ['render: Literal["plain", "dated"]'], code),
    ("H6", "fact -> source replay", ["_best_source_turn"], code),
    ("H7/H12", "QA prompt variants", ["ABSTAIN_QA_PROMPT", "DATED_QA_PROMPT"], evals),
    ("H8", "anticipation stage", ["async def anticipate("], code),
    ("H9", "contest + overlap", ["contest_ties"], code),
    ("H10", "relevance-first scoring", ["relevance_first"], code),
    ("H11", "candidate pool", ["candidate_pool"], code),
    ("H13", "core-terms leg", ["core_terms_leg"], code),
    (
        "H14",
        "profile / persona cards from reflection",
        ["async def reflect_profile(", "reflect_profile"],
        code,
    ),
    ("H15", "topic segments", ["topic_segments"], code),
    ("H16", "time order for ordering questions", ["order_by_time_for_ordering"], code),
    ("H17", "3-way relevance filter", ["relevance_filter"], code),
    ("H18", "gated rerank", ["rerank_max_top_k"], code),
    ("H19", "latest-dated slots", ["latest_slots"], code),
    ("H20", "hard exclusion of superseded", ["RecordStatus.ARCHIVED"], code),
    ("H21", "deposit filters", ["skip_injected_recall"], code),
    ("H22", "topic timeline / gap markers in render", ["gap_markers"], code),
    ("H23", "near-duplicate removal", ["dedupe_jaccard"], code),
    (
        "H24",
        "learned/encoder query planner (decision port)",
        ["services/decision", "DecisionProvider"],
        code,
    ),
    ("H25", "OmniMemEval protocol profile", ["omnimemeval"], evals),
    (
        "P4-llm",
        "LLM query planner / rewrites for COMPOSE (query_rewrite role wired)",
        ["query_rewrite_probes"],
        code,
    ),
    ("RERANK-DATE", "date prefix in rerank input (Hindsight)", ["[Date:"], code),
    (
        "ORDER-NORERANK",
        "skip rerank for ordering queries (Agent Zero)",
        ["skip_rerank_for_ordering"],
        code,
    ),
    ("RESERVE", "reply reserve in the budget (ContextPipe)", ["reply_reserve_tokens"], code),
    ("RRFK", "RRF constant as config (ablation)", ["rrf_k"], code),
]
print(f"{'id':14s} {'present':8s} description")
missing = []
for fid, desc, needles, hay in FEATURES:
    ok = all(n in hay for n in needles)
    if not ok:
        missing.append(fid)
    print(f"{fid:14s} {'yes' if ok else 'NO':8s} {desc}")
print("\nmissing:", ", ".join(missing))
