"""Audit: every planned memspine feature -> is its identifier present in src/, and do the key
features *behave* (R3-12)?

The identifier pass only proves a name exists. The behavioural pass runs each key feature on a
tiny offline input (hash embedder, in-memory store, no model calls) and checks the effect the
feature promises, so a renamed-but-dead or present-but-unwired feature fails here.

    python evals/feature_audit.py            # both passes, from any checkout
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "memspine"
EV = ROOT / "evals" / "memspine_evals"


def _haystacks() -> tuple[str, str]:
    code = "\n".join(p.read_text("utf-8") for p in SRC.rglob("*.py"))
    code += "\n".join(p.read_text("utf-8") for p in SRC.rglob("*.yaml"))
    evals = "\n".join(p.read_text("utf-8") for p in EV.rglob("*.py"))
    return code, evals


code, evals = _haystacks()

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


def identifier_audit() -> list[str]:
    missing = []
    print(f"{'id':14s} {'present':8s} description")
    for fid, desc, needles, hay in FEATURES:
        ok = all(n in hay for n in needles)
        if not ok:
            missing.append(fid)
        print(f"{fid:14s} {'yes' if ok else 'NO':8s} {desc}")
    return missing


# -- behavioural checks --------------------------------------------------------------


async def _engine(**read: Any) -> Any:
    from memspine.engine import Engine

    engine = Engine(
        template="base",
        storage={"path": ":memory:"},
        embedding={"provider": "hash", "dim": 64},
        read={"record_access": False, **read},
        dotenv_path=None,
    )
    await engine.start()
    return engine


async def _event_time() -> bool:
    engine = await _engine()
    try:
        when = datetime(2023, 5, 8, tzinfo=UTC)
        records = await engine.write_messages(
            [{"role": "user", "content": "Ana: I moved to Lyon."}],
            namespace="audit",
            session_id="s1",
            valid_from=when,
        )
        return bool(records) and all(r.valid_from == when for r in records)
    finally:
        await engine.stop()


async def _dated_render() -> bool:
    engine = await _engine(render="dated")
    try:
        await engine.write_messages(
            [{"role": "user", "content": "Ana: I moved to Lyon."}],
            namespace="audit",
            session_id="s1",
            valid_from=datetime(2023, 5, 8, tzinfo=UTC),
        )
        ctx = await engine.assemble(
            "where does Ana live", namespace="audit", budget_tokens=200, top_k=3
        )
        return any("2023" in r.content for r in ctx.records)
    finally:
        await engine.stop()


def check_event_time() -> bool:
    return asyncio.run(_event_time())


def check_dated_render() -> bool:
    return asyncio.run(_dated_render())


def check_relative_dates() -> bool:
    from memspine.core.temporal_resolve import resolve

    hits = resolve("We went last Friday.", date(2023, 7, 15))
    return any(r.first == r.last == date(2023, 7, 14) for r in hits)


def check_read_config_wiring() -> bool:
    """The read options the feature list names are real, validated config fields."""
    from memspine.config.schema import ReadConfig

    cfg = ReadConfig(
        rrf_k=1, reply_reserve_tokens=500, render="dated", gap_markers=True, candidate_pool=3
    )
    if (cfg.rrf_k, cfg.reply_reserve_tokens, cfg.render) != (1, 500, "dated"):
        return False
    try:
        ReadConfig(render="not-a-mode")
    except ValueError:
        return True
    return False


def check_cached_tokens() -> bool:
    from memspine_evals.bedrock import cached_prompt_tokens

    usage = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=7))
    return cached_prompt_tokens(usage) == 7 and cached_prompt_tokens(None) == 0


def check_qa_prompts() -> bool:
    from memspine_evals.readers import QA_PROMPTS

    abstain = QA_PROMPTS["abstain"].format(context="x", question="q")
    dated = QA_PROMPTS["question_dated"].format(
        context="x", question="q", question_date="2023/05/30"
    )
    return "Not mentioned" in abstain and "2023/05/30" in dated


def check_omnimemeval_preset() -> bool:
    from memspine_evals.experiments import C01Config, apply_protocol_preset

    cfg = apply_protocol_preset(C01Config(mode="qa"), "omnimemeval")
    return cfg.categories == (1, 2, 3, 4) and cfg.judge_model == "gpt-4o-mini"


def check_cat5_abstention_gold() -> bool:
    from memspine_evals.datasets import LoCoMoDataset
    from memspine_evals.judge import ABSTENTION_GOLD

    sample = [
        {
            "sample_id": "c",
            "conversation": {
                "speaker_a": "A",
                "speaker_b": "B",
                "session_1_date_time": "2:00 pm on 8 May, 2023",
                "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "hi"}],
            },
            "qa": [{"question": "q", "adversarial_answer": "x", "category": 5}],
        }
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "l.json"
        path.write_text(json.dumps(sample), encoding="utf-8")
        query = next(LoCoMoDataset(path, revision_id="t").items()).queries[0]
    return query.gold == ABSTENTION_GOLD


#: (id, description, check). Each check returns True when the feature behaves.
BEHAVIOURAL: list[tuple[str, str, Callable[[], bool]]] = [
    ("C-1", "event time on write lands on the record", check_event_time),
    ("H1", "relative dates resolve against the anchor", check_relative_dates),
    ("H5", "dated render puts the event date on the line", check_dated_render),
    ("RRFK/RESERVE", "read options are validated config", check_read_config_wiring),
    ("C9'", "cached-token reporting reads usage", check_cached_tokens),
    ("H7/H12", "QA prompt variants render (abstain, question date)", check_qa_prompts),
    ("H25", "OmniMemEval preset sets reader/judge/categories", check_omnimemeval_preset),
    ("R3-1", "LoCoMo cat-5 gold is the refusal", check_cat5_abstention_gold),
]


def behavioural_audit() -> list[str]:
    failed = []
    print(f"\n{'id':14s} {'behaves':8s} description")
    for fid, desc, check in BEHAVIOURAL:
        note = desc
        try:
            ok = bool(check())
        except Exception as exc:  # a crash is a failure, reported, not raised
            ok = False
            note = f"{desc} [{type(exc).__name__}: {exc}]"
        if not ok:
            failed.append(fid)
        print(f"{fid:14s} {'yes' if ok else 'NO':8s} {note}")
    return failed


def main() -> int:
    missing = identifier_audit()
    print("\nmissing:", ", ".join(missing))
    failed = behavioural_audit()
    print("\nbehaviour failures:", ", ".join(failed) or "none")
    return 1 if missing or failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
