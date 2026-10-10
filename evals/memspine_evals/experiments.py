"""Named experiments. First one: **C0-1**, the verbatim-baseline gate.

Plan C §1.1 states the problem plainly. MemSpine's framework says the deposit
stage is the field's principal design axis, and the engine invests there: nine
memory types, extraction, consolidation, a sleep cycle, a firewall. MemPalace's
claim is that on the standard benchmark, *doing none of that* beats most of it.

Three things have to be true of any test that settles it, and they are encoded
here rather than described:

1. **Same protocol for every arm.** One budget, one reader, one judge, one
   seed — the arms differ only in the system.
2. **Both measurement modes, kept apart.** ``retrieval`` reports R@k and
   retrieval sufficiency (MemPalace's actual metric, no generation, no model
   calls). ``qa`` reports answer accuracy with a declared backbone. The harness
   will not let a retrieval number be published as a QA number.
3. **Both datasets.** The whole finding is that the verbatim advantage did
   *not* transfer between LongMemEval (96.6% R@5) and LoCoMo (60.3% R@10). One
   dataset cannot settle a transfer question.

If verbatim wins on our own harness too, MemSpine's contribution is the
*reconciliation* layer rather than the extraction layer, and the framework
paper's emphasis moves before drafting rather than after review.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .bedrock import QWEN3_32B
from .contracts import DatasetAdapter, Reader, SystemAdapter, sha256_text
from .judge import DEFAULT_BINARY_PROMPT, ContainsJudge, Judge
from .metrics import CostModel, Price
from .provenance import RunProtocol
from .readers import (
    ContextOnlyReader,
    CtxGuard,
    OpenAICompatReader,
    SamplerConfig,
    openai_compat_chat,
)
from .results import RunSummary
from .runner import RunConfig, run_matrix
from .systems import (
    BM25Retriever,
    FullContextSystem,
    NaiveRAGSystem,
    NoMemorySystem,
    VerbatimSystem,
)
from .systems.baselines import BudgetCappedSystem

#: Recorded in every C0-1 manifest from this revision on (R3 fixes of 2 Oct 2026): cat-5
#: abstention gold, unit-level R@k with R_all, cut evidence dropped, per-arm call budgets,
#: per-question judge routing, the question date passed to the reader.
HARNESS_PROTOCOL_REVISION = "harness-protocol=r3-2026-10-02"

#: judge choices: the alias judge (no model) plus every routed suite.
JUDGE_CHOICES = (
    "rubric",
    "rubric-guarded",
    "constraint",
    "alias",
    "locomo-plus-v2",
    "longmemeval",
    "omnimemeval",
    "mem0-official",
    "opbench",
)

Mode = Literal["retrieval", "qa"]

# The budget is the protocol's, not a system's. 4,096 is the initial retrieved-
# token cap set by `paper_spine/EVALUATION_PLAN_2026-09.md` §4, with 2,048 and
# 8,192 reserved as *later* ablations — never mid-comparison. (LongMemEval-V2's
# own precedent is 200k, for trajectories.) Whatever it is, it is declared and
# identical across arms.
DEFAULT_BUDGET = 4096


@dataclass(frozen=True)
class C01Config:
    mode: Mode = "retrieval"
    budget_tokens: int = DEFAULT_BUDGET
    top_k: int = 10
    seed: int = 11
    dense: bool = False
    hybrid: bool = False
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    reader_model: str = "qwen3:4b"
    judge_model: str = "qwen3:4b"
    base_url: str = "http://127.0.0.1:11434/v1"
    include_memspine: bool = False
    max_items: int | None = None
    max_model_calls: int | None = None
    #: D23 "Qwen3 protocol": reader AND judge are Bedrock Qwen3 via LiteLLM,
    #: sharing one CallBudget capped at ``max_model_calls``.
    bedrock: bool = False
    #: Engine overrides for the memspine arm (e.g. Cohere embed-v4 at 1024-d).
    memspine_config: dict[str, Any] | None = None
    #: memspine arm read path: None = assemble, else an Engine.read mode (C7').
    memspine_read_mode: str | None = None
    #: N34: memspine reads as of each question's date (LongMemEval ``question_date``).
    memspine_as_of_question_date: bool = False
    #: C1: mark final search hits in the rendered context (off | star | rank)
    memspine_mark_hits: str = "off"
    #: R2-4: context line order (chrono | hits_first | hit_blocks)
    memspine_context_order: str = "chrono"
    #: run the engine's sleep cycle after ingestion (write-time stages: H2, H8, H14)
    memspine_build_sleep: bool = False
    #: G9: turns of one session per write_messages call (1 = one call per turn)
    memspine_batch_turns: int = 1
    #: H7/H12: QA prompt variant for EVERY arm (default | dated | abstain), recorded
    #: in the reader manifest via its prompt hash.
    qa_prompt: str = "default"
    #: judge for QA mode, every endpoint (R3-4/R3-5): rubric (default) | constraint
    #: (LoCoMo-Plus) | alias | locomo-plus-v2 | longmemeval | omnimemeval (official, ported)
    judge_prompt: str = "rubric"
    #: LoCoMo categories the run loads (R3-1); None = every category. Recorded in labels.
    categories: tuple[int, ...] | None = None
    #: R4-6: add a naive-RAG arm whose dense retriever is memspine's own embedder
    naive_dense_same_embedder: bool = False
    #: R4-6: add a naive-RAG arm capped at this many context tokens (match memspine's mean)
    matched_budget_tokens: int | None = None
    #: free-text protocol note recorded in every manifest (set by a preset, H25)
    protocol_notes: str = ""
    #: Resume support: run only these system ids / item ids (None = all). A
    #: partial run is completed into a separate run id and merged by item, which
    #: is valid because items are independent (each resets the system).
    only_systems: tuple[str, ...] | None = None
    item_ids: tuple[str, ...] | None = None
    #: G2b: bind every engine LLM role of the memspine arm to this model family
    #: (``none`` | ``bedrock-qwen3``). Explicit ``llm.roles`` in ``memspine_config`` win.
    memspine_llm: str = "none"
    #: G3: USD per 1M tokens, ``(model, in, out)`` triples; override the built-in table.
    prices_per_mtok: tuple[tuple[str, float, float], ...] = ()
    #: G3: dollar cap per arm (reader + judge + engine). None = no dollar cap.
    max_usd: float | None = None
    #: C-6: ``(kind, model, price)`` for paid engine services: ``embed`` in USD per 1M
    #: input tokens, ``rerank`` in USD per 1,000 searches (``--price embed:M=X``).
    service_prices: tuple[tuple[str, str, float], ...] = ()
    #: questions per item (rehearsals: the first N of a conversation). None = all.
    max_queries_per_item: int | None = None
    #: #39 (SM-18): check each QA answer against its context with memspine's
    #: ``verify_answer`` prompt on the judge's backend (+1 call per question); an
    #: unsupported answer the context contradicts is replaced. Off: readers unchanged.
    verify_answer: bool = False
    #: reader-gap fix: re-ask once, firmer, when the reader refuses (``refusal.py``).
    retry_refusal: bool = False
    #: C10: with ``retry_refusal``, skip the retry when the question names an entity the
    #: retrieved context never mentions (``refusal.names_absent_entity``). Off: unchanged.
    retry_guard: bool = False
    #: I32: tell the reader no memory matches an asserted past event (``no_record.py``).
    no_record_hint: bool = False
    #: I28: ``opendecider`` replaces the ``is_refusal`` regex in the refusal retry (needs
    #: ``retry_refusal``); question and answer text only. ``heuristic`` (default): unchanged.
    decider: str = "heuristic"
    decider_model: str = "manjunathshiva/opendecider-nano"
    decider_device: str = "cpu"
    decider_min_confidence: float = 0.5
    decider_threads: int = 0  # 0 = physical cores
    decider_backend: str = "torch"
    decider_dtype: str = "float32"
    decider_workers: int = 1
    #: reader-gap fix: empty answers score wrong without a judge call, and ``rubric`` becomes
    #: ``rubric-guarded`` (``judge.GuardedJudge``). Off: judges unchanged.
    judge_guards: bool = False
    #: gap A2: deterministic single-day date equivalence before the LLM judge
    judge_date_check: bool = False
    #: screening: ingest and read every question as the QA run would, skip the reader
    #: and the judge, record evidence coverage per question (``screen.py``)
    retrieval_only: bool = False
    #: screening: a directory for the disk cache of paid embedding and engine-role
    #: completion calls (``call_cache.py``). None = no cache.
    cache_dir: str | None = None
    #: screening: also cache reader and judge completions (temperature 0 only)
    cache_reader: bool = False
    #: A8 [SRV-2]: sampler sent explicitly with every reader and judge request. The server's
    #: own defaults (Ollama/Qwen3.5: presence_penalty 1.5) no longer apply. ``sampler_seed``
    #: None = the run ``seed``.
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    top_p: float = 1.0
    sampler_seed: int | None = None
    #: D2 [HAR-3]: the server's context window; a call with prompt + completion tokens
    #: within 8 of it is flagged ``server_truncation_suspected`` (``strict_ctx``: raise).
    server_ctx: int = 8192
    strict_ctx: bool = False
    #: D1 [HAR-1]: ``heuristic`` (chars/4, the default, old runs stay comparable) or
    #: ``reader`` (the reader's own tokenizer; over-budget contexts drop their lowest-ranked
    #: lines instead of cutting the tail).
    token_count: str = "heuristic"
    tokenizer_id: str | None = None
    #: D4 [HAR-5]: record the ``runtime`` block (versions, argv, env, Ollama state) in the
    #: manifest. ``probe_server`` also asks the local Ollama for /api/version and /api/ps.
    capture_runtime: bool = False
    probe_server: bool = False
    #: OP-Bench: the local checkout whose ``src/opbench/prompts.py`` holds the official judge
    #: prompts (read at run time, never vendored); set only for ``--dataset op_bench``.
    opbench_root: str | None = None


def sampler_for(config: C01Config) -> SamplerConfig:
    """A8: the run's explicit sampler (seed defaults to the run seed)."""
    return SamplerConfig(
        presence_penalty=config.presence_penalty,
        frequency_penalty=config.frequency_penalty,
        top_p=config.top_p,
        seed=config.seed if config.sampler_seed is None else config.sampler_seed,
    )


#: H25: declared protocol presets. OmniMemEval (MemTensor/OmniMemEval @ 0b1ea8d) is the
#: largest uniform memory-QA table (14 engines). Its protocol, as reported by Mnemon
#: (arXiv 2609.36059, Table 1): gpt-4.1-mini answers at T=0, gpt-4o-mini grades, LoCoMo
#: categories 1-4 (1,540 questions), context tokens reported per question. The grading
#: prompt is OmniMemEval's own LoCoMo judge (``JUDGE_PROMPT`` + ``JUDGE_SYSTEM_PROMPT`` from
#: ``scripts/utils/prompts.py`` @ 0b1ea8d, ported verbatim on 2026-10-03), so the preset
#: sets ``judge_prompt="omnimemeval"``. OmniMemEval may average several judge runs per
#: question; this harness grades once.
PROTOCOL_PRESETS: dict[str, dict[str, Any]] = {
    "omnimemeval": {
        "reader_model": "gpt-4.1-mini",
        "judge_model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "categories": (1, 2, 3, 4),
        "judge_prompt": "omnimemeval",
        "notes": (
            "OmniMemEval-protocol preset (reader gpt-4.1-mini T=0, judge gpt-4o-mini, "
            "cats 1-4); judge prompt = OmniMemEval LoCoMo judge, verbatim @ 0b1ea8d"
        ),
    },
}


def apply_protocol_preset(config: C01Config, name: str | None) -> C01Config:
    """Return ``config`` with a named protocol preset's reader/judge/endpoint applied."""
    if not name:
        return config
    if name not in PROTOCOL_PRESETS:
        raise ValueError(f"unknown protocol preset {name!r}; known: {sorted(PROTOCOL_PRESETS)}")
    from dataclasses import replace

    preset = PROTOCOL_PRESETS[name]
    categories = preset.get("categories")
    if (
        config.categories is not None
        and categories is not None
        and tuple(config.categories) != tuple(categories)
    ):
        raise ValueError(f"preset {name!r} fixes categories {categories}; got {config.categories}")
    judge_prompt = preset.get("judge_prompt", config.judge_prompt)
    if config.judge_prompt not in ("rubric", judge_prompt):
        raise ValueError(
            f"preset {name!r} fixes judge prompt {judge_prompt!r}; got {config.judge_prompt!r}"
        )
    return replace(
        config,
        reader_model=preset["reader_model"],
        judge_model=preset["judge_model"],
        base_url=preset["base_url"],
        protocol_notes=preset["notes"],
        categories=tuple(categories) if categories is not None else config.categories,
        judge_prompt=judge_prompt,
    )


def _retriever(config: C01Config) -> Any:
    """Lexical, dense or their RRF fusion — the evaluation plan's conditions 2
    and 3. Which one ran is recorded in the manifest; a hash-vector control is
    never labelled dense, because the retriever reports its own model id."""
    if config.hybrid:
        from .systems.retrievers import FastEmbedRetriever, HybridRetriever

        return HybridRetriever(BM25Retriever(), FastEmbedRetriever(model=config.embedding_model))
    if not config.dense:
        return BM25Retriever()
    from .systems.retrievers import FastEmbedRetriever

    return FastEmbedRetriever(model=config.embedding_model)


def memspine_embedding_model(config: C01Config) -> str:
    """The embedder the memspine arm uses, for the same-embedder naive arm (R4-6).

    Only a local fastembed model can be shared: a cloud embedder would make the baseline
    pay for calls the harness does not budget, so that combination is refused.
    """
    embedding = dict((config.memspine_config or {}).get("embedding") or {})
    try:
        from memspine.config.schema import EmbeddingConfig

        defaults = EmbeddingConfig()
        provider = embedding.get("provider", defaults.provider)
        model = embedding.get("model", defaults.model)
    except ImportError:  # the engine is not installed: its documented default
        provider = embedding.get("provider", "fastembed")
        model = embedding.get("model", "BAAI/bge-small-en-v1.5")
    if provider != "fastembed":
        raise ValueError(
            f"the same-embedder naive arm needs a fastembed embedder; memspine uses {provider!r}"
        )
    return str(model)


#: Built-in Bedrock price table (USD per 1M input / output tokens), used when no
#: ``--price`` is given. Verify against the AWS Bedrock pricing page for the region.
DEFAULT_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    QWEN3_32B: (0.16, 0.62),
}


def price_table(config: C01Config) -> dict[str, tuple[float, float]]:
    """The run's price table: the built-in defaults, overridden by ``--price``."""
    table = dict(DEFAULT_PRICES_PER_MTOK)
    for model, p_in, p_out in config.prices_per_mtok:
        table[model] = (p_in, p_out)
    return table


def memspine_engine_config(config: C01Config) -> dict[str, Any] | None:
    """The memspine arm's engine overrides, with ``--memspine-llm`` roles merged in (G2b)."""
    from .bedrock import MEMSPINE_LLM_CHOICES, aws_region_from_env, merge_engine_llm_roles

    if config.memspine_llm not in MEMSPINE_LLM_CHOICES:
        raise ValueError(
            f"unknown memspine llm {config.memspine_llm!r}; known: {sorted(MEMSPINE_LLM_CHOICES)}"
        )
    model = MEMSPINE_LLM_CHOICES[config.memspine_llm]
    if model is None:
        return config.memspine_config
    return merge_engine_llm_roles(config.memspine_config, model, aws_region_from_env())


def engine_llm_models(config: C01Config) -> set[str]:
    """Model ids bound to the memspine arm's engine roles (empty without the arm)."""
    if not config.include_memspine:
        return set()
    roles = ((memspine_engine_config(config) or {}).get("llm") or {}).get("roles") or {}
    return {str(binding.get("model", "")) for binding in roles.values() if binding.get("model")}


def paid_services(config: C01Config) -> list[tuple[str, str]]:
    """C-6: the memspine arm's paid cloud services, as ``(kind, model)``: a LiteLLM
    embedder (``embed``) and a LiteLLM reranker (``rerank``). Local ones are free."""
    if not config.include_memspine:
        return []
    engine = memspine_engine_config(config) or {}
    out: list[tuple[str, str]] = []
    embedding = dict(engine.get("embedding") or {})
    if embedding.get("provider") == "litellm":
        out.append(("embed", str(embedding.get("model") or "")))
    read = dict(engine.get("read") or {})
    if read.get("rerank") == "litellm":
        out.append(("rerank", str(read.get("rerank_model") or "")))
    return out


def service_price_table(config: C01Config) -> dict[str, float]:
    """``"embed:<model>"`` / ``"rerank:<model>"`` -> price, from ``--price`` (C-6)."""
    return {f"{kind}:{model}": price for kind, model, price in config.service_prices}


def unpriced_services(config: C01Config) -> list[tuple[str, str]]:
    """Paid services of the run with no ``--price`` (C-6)."""
    table = service_price_table(config)
    return [(k, m) for k, m in paid_services(config) if f"{k}:{m}" not in table]


def check_dollar_cap(config: C01Config) -> None:
    """A dollar cap is only a cap if every paid model in the run has a price (G3), the
    engine's paid embedder and reranker included (C-6)."""
    if config.max_usd is None:
        return
    if config.max_usd <= 0:
        raise ValueError(f"max_usd must be > 0, got {config.max_usd}")
    paid = set(engine_llm_models(config))
    if config.mode == "qa" and not config.retrieval_only:
        if not config.bedrock:
            raise ValueError(
                "--max-usd checks reader and judge calls before they are made only on the "
                "Bedrock endpoint (--bedrock); other endpoints are not metered"
            )
        paid.add(QWEN3_32B)
    if not paid:
        raise ValueError("--max-usd was given but this run makes no paid model calls")
    table = price_table(config)
    missing = sorted(m for m in paid if m not in table)
    if missing:
        raise ValueError(f"--max-usd needs a --price for every paid model; missing: {missing}")
    services = unpriced_services(config)
    if services:
        flags = ", ".join(f"--price {kind}:{model}=..." for kind, model in services)
        raise ValueError(
            "--max-usd needs a price for every paid cloud service of the memspine arm "
            f"(embed: USD per 1M tokens, rerank: USD per 1,000 searches); missing: {flags}"
        )


def _retry_decider_kwargs(config: C01Config) -> dict[str, Any]:
    """I28: ``RefusalRetryReader`` kwargs for ``--decider`` (none for the default heuristic)."""
    guard = {"guard_absent_entity": True} if config.retry_guard else {}
    if config.decider == "heuristic":
        return guard
    from memspine.services.decision.decider import build_decider

    return {
        **guard,
        "decider": build_decider(
            config.decider,
            config.decider_model,
            config.decider_device,
            threads=config.decider_threads,
            backend=config.decider_backend,
            workers=config.decider_workers,
            dtype=config.decider_dtype,
        ),
        "decider_min_confidence": config.decider_min_confidence,
    }


def build_systems(config: C01Config) -> list[SystemAdapter]:
    """The arms, in the order they should be read.

    Conditions 1-4 of `EVALUATION_PLAN_2026-09.md` §3: no persistent memory,
    BM25 over timestamped raw turns, dense-or-hybrid RAG over the same turns,
    and the engine. Full-context replay joins them as the separately costed
    reference the plan allows when the history fits.
    """
    systems: list[SystemAdapter] = [
        NoMemorySystem(),
        FullContextSystem(),
        NaiveRAGSystem(retriever=_retriever(config)),
        VerbatimSystem(retriever=_retriever(config)),
    ]
    if config.naive_dense_same_embedder:
        from .systems.retrievers import FastEmbedRetriever

        systems.append(
            NaiveRAGSystem(
                retriever=FastEmbedRetriever(model=memspine_embedding_model(config)),
                system_id="naive-rag-dense-memspine-embedder",
            )
        )
    if config.matched_budget_tokens is not None:
        systems.append(
            BudgetCappedSystem(
                NaiveRAGSystem(retriever=_retriever(config)), config.matched_budget_tokens
            )
        )
    if config.include_memspine:
        from .systems.memspine_system import MemspineSystem

        systems.append(
            MemspineSystem(
                config=memspine_engine_config(config),
                read_mode=config.memspine_read_mode,
                build_sleep=config.memspine_build_sleep,
                batch_turns=config.memspine_batch_turns,
                as_of_question_date=config.memspine_as_of_question_date,
                mark_hits=config.memspine_mark_hits,
                context_order=config.memspine_context_order,
            )
        )
    if config.only_systems:
        systems = [s for s in systems if s.system_id in config.only_systems]
    return systems


class _ItemFilter:
    """A dataset view restricted to some item ids (resume of a partial run)."""

    def __init__(self, inner: DatasetAdapter, item_ids: tuple[str, ...]) -> None:
        self._inner = inner
        self._ids = set(item_ids)

    def info(self) -> Any:
        return self._inner.info()

    def items(self) -> Any:
        return (item for item in self._inner.items() if item.item_id in self._ids)


def build_judge(config: C01Config, chat: Any, model: str, judge_id: str | None = None) -> Judge:
    """The QA-mode judge, the same choice on every endpoint (R3-4).

    ``alias`` is deterministic; every other choice is a routed suite (``judge_prompts``).
    The one-token default prompt is never used in QA: it graded "I do not know" CORRECT.
    """
    if config.judge_prompt == "alias":
        from .judge import AliasContainsJudge, GuardedJudge

        return (
            GuardedJudge(
                AliasContainsJudge(),
                date_check=config.judge_date_check,
                empty_guard=config.judge_guards,
            )
            if (config.judge_guards or config.judge_date_check)
            else AliasContainsJudge()
        )
    if config.judge_prompt == "opbench":
        from .opbench import build_opbench_judge

        if config.opbench_root is None:
            raise ValueError("the opbench judge needs the OP-Bench checkout (opbench_root)")
        return build_opbench_judge(chat, model, config.opbench_root, config.embedding_model)
    from .judge import GuardedJudge
    from .judge_prompts import RoutedLLMJudge

    suite = config.judge_prompt
    if config.judge_guards and suite == "rubric":
        suite = "rubric-guarded"
    judge = RoutedLLMJudge(chat, model=model, suite=suite, judge_id=judge_id)
    if judge.spec.prompt_hash == sha256_text(DEFAULT_BINARY_PROMPT):  # pragma: no cover
        raise ValueError("the default binary judge prompt is not allowed in QA mode")
    if config.judge_guards or config.judge_date_check:
        return GuardedJudge(
            judge, date_check=config.judge_date_check, empty_guard=config.judge_guards
        )
    return judge


def build_reader_and_judge(config: C01Config) -> tuple[Reader, Judge, bool]:
    """Returns ``(reader, judge, makes_model_calls)`` for the chosen mode.

    Every call builds a fresh reader and judge, and on Bedrock a fresh ``CallBudget``:
    ``run_c0_1`` calls it once per arm, so one arm cannot spend another's budget (R3-3).
    """
    if config.retrieval_only:
        # Screening: the runner stops after the read; neither of these is ever called.
        from .screen import SkippedJudge, SkippedReader

        return SkippedReader(), SkippedJudge(), False  # type: ignore[return-value]
    if config.mode == "retrieval":
        # No generation: "was the answer retrievable at all". This is what R@k
        # and MemPalace's 96.6 measure, and it costs nothing to run.
        return ContextOnlyReader(), ContainsJudge(), False
    from .readers import (
        QA_PROMPTS,
        REASONING_QA_PROMPTS,
        ROUTED_QA_PROMPTS,
        SYSTEM_QA_PROMPTS,
        RoutedQAPrompt,
        SystemQAPrompt,
        reasoning_max_tokens,
    )

    qa_prompt: str | RoutedQAPrompt | SystemQAPrompt
    if config.qa_prompt in SYSTEM_QA_PROMPTS:  # OP-Bench: a system message + user message
        if config.bedrock:
            raise ValueError(f"qa prompt {config.qa_prompt!r} has a system message; the Bedrock "
                             "reader does not send one (use the local OpenAI-compatible reader)")
        qa_prompt = SYSTEM_QA_PROMPTS[config.qa_prompt]
    elif config.qa_prompt in QA_PROMPTS:
        qa_prompt = QA_PROMPTS[config.qa_prompt]
    elif config.qa_prompt in ROUTED_QA_PROMPTS:  # C1: one variant per question
        qa_prompt = ROUTED_QA_PROMPTS[config.qa_prompt]
    else:
        known = sorted([*QA_PROMPTS, *ROUTED_QA_PROMPTS, *SYSTEM_QA_PROMPTS])
        raise ValueError(f"unknown qa prompt {config.qa_prompt!r}; known: {known}")
    # #34: a reasoning prompt's reader keeps the final answer and gets room to reason.
    reasoning = config.qa_prompt in REASONING_QA_PROMPTS
    if config.bedrock:
        from .bedrock import CallBudget, LiteLLMReader, litellm_chat

        if config.max_model_calls is None:
            raise ValueError("bedrock qa needs max_model_calls (the D23 budget cap)")
        budget = CallBudget(
            max_calls=config.max_model_calls,
            prices_per_mtok=price_table(config),
            max_usd=config.max_usd,
            service_prices=service_price_table(config),
        )
        bedrock_reader = LiteLLMReader(
            budget,
            model=QWEN3_32B,
            temperature=0.0,
            max_tokens=reasoning_max_tokens(config.qa_prompt) if reasoning else 256,
            prompt=qa_prompt,
            extract_answer=reasoning,
        )
        if config.retry_refusal:
            from .refusal import RefusalRetryReader

            bedrock_reader = RefusalRetryReader(  # type: ignore[assignment]
                bedrock_reader, **_retry_decider_kwargs(config)
            )
        judge = build_judge(
            config,
            litellm_chat(budget, model=QWEN3_32B),
            QWEN3_32B,
            judge_id=f"qwen3-32b-{config.judge_prompt}",
        )
        if config.verify_answer:
            return (
                with_verifier(
                    bedrock_reader, litellm_chat(budget, model=QWEN3_32B, max_tokens=192)
                ),
                judge,
                True,
            )
        return bedrock_reader, judge, True
    import os

    # Local OpenAI-compatible servers ignore the key; hosted endpoints (e.g. the
    # OmniMemEval preset) read it from the process environment, never from .env.
    api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
    sampler = sampler_for(config)
    guard = CtxGuard(config.server_ctx, config.strict_ctx)
    reader = OpenAICompatReader(
        model=config.reader_model,
        base_url=config.base_url,
        api_key=api_key,
        prompt=qa_prompt,
        extract_answer=reasoning,
        sampler=sampler,
        guard=guard,
        **({"max_tokens": reasoning_max_tokens(config.qa_prompt)} if reasoning else {}),
    )
    if config.retry_refusal:
        from .refusal import RefusalRetryReader

        reader = RefusalRetryReader(  # type: ignore[assignment]
            reader, **_retry_decider_kwargs(config)
        )
    if config.no_record_hint:
        from .no_record import NoRecordHintReader

        reader = NoRecordHintReader(reader)  # type: ignore[assignment]
    judge = build_judge(
        config,
        openai_compat_chat(
            config.judge_model,
            base_url=config.base_url,
            api_key=api_key,
            sampler=sampler,
            guard=guard,
        ),
        config.judge_model,
    )
    if config.verify_answer:
        chat = openai_compat_chat(
            config.judge_model,
            base_url=config.base_url,
            api_key=api_key,
            sampler=sampler,
            guard=guard,
        )
        return with_verifier(reader, chat), judge, True
    return reader, judge, True


def _openai_compat_labels(config: C01Config, calls: bool) -> dict[str, Any]:
    """A8/D2: the sampler and context guard a local-endpoint QA run used, for the manifest
    labels. Listed only for runs that call the OpenAI-compatible endpoint."""
    if not calls or config.bedrock or config.mode != "qa" or config.retrieval_only:
        return {}
    return {
        "sampler": sampler_for(config).describe(),
        "server_ctx": config.server_ctx,
        "strict_ctx": config.strict_ctx,
    }


def _capture_runtime(config: C01Config) -> dict[str, Any]:
    if not config.capture_runtime:
        return {}
    from .provenance import capture_runtime

    return capture_runtime(
        base_url=config.base_url if config.probe_server else None,
    )


def build_token_counter(config: C01Config) -> Any:
    """D1 [HAR-1]: the runner's token counter. ``heuristic`` (default): None, so the runner
    builds its chars/4 counter; ``reader``: the reader tokenizer, else the heuristic with a
    logged warning."""
    if config.token_count == "heuristic":
        return None
    if config.token_count != "reader":
        raise ValueError(f"unknown token_count {config.token_count!r}; known: heuristic, reader")
    from .tokens import HeuristicTokenCounter, load_reader_tokenizer_counter

    counter = load_reader_tokenizer_counter(config.tokenizer_id)
    return counter if counter is not None else HeuristicTokenCounter()


def with_verifier(reader: Any, chat: Any) -> Any:
    """#39: ``reader`` wrapped so each answer is verified against its context."""
    from .verify import VerifyingReader, chat_verifier

    verify, prompt_version = chat_verifier(chat)
    return VerifyingReader(reader, verify, prompt_version)


def _abstention_queries(dataset: DatasetAdapter) -> int:
    return sum(1 for item in dataset.items() for q in item.queries if q.meta.get("abstention"))


async def run_c0_1(
    dataset: DatasetAdapter,
    config: C01Config,
    out_dir: Path,
    run_id: str | None = None,
) -> list[RunSummary]:
    reader, judge, calls = build_reader_and_judge(config)
    if config.item_ids:
        dataset = _ItemFilter(dataset, config.item_ids)  # type: ignore[assignment]
    n_abstention = 0 if config.retrieval_only else _abstention_queries(dataset)
    if n_abstention and not getattr(judge, "handles_abstention", False):
        raise ValueError(
            f"{n_abstention} abstention question(s) (e.g. LoCoMo cat 5) but judge "
            f"{judge.spec.judge_id!r} cannot grade a refusal; pass --categories 1,2,3,4 "
            "or an abstention-aware judge"
        )
    from .screen import RETRIEVAL_ONLY_PROTOCOL

    protocol = RunProtocol(
        protocol_id=RETRIEVAL_ONLY_PROTOCOL if config.retrieval_only else f"c0-1-{config.mode}",
        budget_tokens=config.budget_tokens,
        top_k=config.top_k,
        seed=config.seed,
        notes="C0-1 verbatim-baseline gate; identical protocol across arms"
        + f"; {HARNESS_PROTOCOL_REVISION}"
        + (f"; {config.protocol_notes}" if config.protocol_notes else ""),
    )
    check_dollar_cap(config)
    systems = build_systems(config)
    engine_models = engine_llm_models(config)
    if engine_models and config.max_model_calls is None:
        raise ValueError(
            "the memspine arm's engine roles call models — set max_model_calls (a cap you "
            "have agreed to)"
        )
    prices = price_table(config)
    info = dataset.info()
    run_config = RunConfig(
        run_id=run_id or f"c0-1-{config.mode}-{info.dataset_id}",
        protocol=protocol,
        out_dir=out_dir,
        cost_model=CostModel(
            model_prices={
                m: Price(p_in / 1000, p_out / 1000) for m, (p_in, p_out) in prices.items()
            }
        ),
        max_items=config.max_items,
        max_queries_per_item=config.max_queries_per_item,
        max_model_calls=config.max_model_calls,
        expect_model_calls=calls or bool(engine_models),
        max_usd=config.max_usd,
        prices_per_mtok=prices,
        service_prices=service_price_table(config),
        labels={
            "experiment": "C0-1",
            "question": "does verbatim storage beat extraction on our own harness",
            "mode": "retrieval-only" if config.retrieval_only else config.mode,
            "dense": config.dense,
            "hybrid": config.hybrid,
            "categories": list(config.categories) if config.categories is not None else "all",
            "qa_prompt": config.qa_prompt if config.mode == "qa" else None,
            **({"verify_answer": True} if config.verify_answer else {}),
            **({"retry_refusal": True} if config.retry_refusal else {}),
            **({"retry_guard": True} if config.retry_guard else {}),
            **({"no_record_hint": True} if config.no_record_hint else {}),
            **({"decider": config.decider} if config.decider != "heuristic" else {}),
            **({"judge_guards": True} if config.judge_guards else {}),
            "judge_prompt": config.judge_prompt if config.mode == "qa" else None,
            **({"opbench_root": config.opbench_root} if config.opbench_root else {}),
            "arms": [s.system_id for s in systems],
            "naive_dense_same_embedder": config.naive_dense_same_embedder,
            "matched_budget_tokens": config.matched_budget_tokens,
            "memspine_llm": config.memspine_llm,
            **_openai_compat_labels(config, calls),
            "engine_llm_models": sorted(engine_models),
            "prices_per_mtok": {m: list(p) for m, p in sorted(prices.items())},
            "service_prices": service_price_table(config),
            "paid_services": [f"{k}:{m}" for k, m in paid_services(config)],
            # screening (listed only when on, so other runs keep their labels)
            **({"retrieval_only": True} if config.retrieval_only else {}),
            **(
                {"call_cache": {"dir": config.cache_dir, "cache_reader": config.cache_reader}}
                if config.cache_dir
                else {}
            ),
        },
        extra_limits={"item_ids": list(config.item_ids) if config.item_ids else None},
        retrieval_only=config.retrieval_only,
        runtime=_capture_runtime(config),
        token_count=config.token_count,
    )
    counter = build_token_counter(config)
    factory = (lambda: build_reader_and_judge(config)[:2]) if calls else None
    if not config.cache_dir:
        return await run_matrix(
            dataset,
            systems,
            reader,
            judge,
            run_config,
            token_counter=counter,
            reader_judge_factory=factory,
        )
    from dataclasses import replace

    from .call_cache import install_call_cache

    with install_call_cache(config.cache_dir, cache_reader=config.cache_reader) as cache:
        return await run_matrix(
            dataset,
            systems,
            reader,
            judge,
            replace(run_config, call_cache=cache),
            token_counter=counter,
            reader_judge_factory=factory,
        )


def comparison_table(summaries: Sequence[RunSummary], mode: Mode) -> str:
    """Markdown comparison. Every column that could hide a protocol difference
    is printed, including the one that says whether the row is admissible."""
    metric = "retrieval sufficiency" if mode == "retrieval" else "answer accuracy"
    lines = [
        f"| system | {metric} | R@1 | R@5 | R@10 | ctx tokens (mean) "
        "| p50 latency ms | CPC $ | D16 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for summary in summaries:
        system = summary.run_id.split("--", 1)[-1]

        def recall(k: int, s: RunSummary = summary) -> str:
            value = s.recall.get(f"R@{k}")
            return "n/a" if value is None else f"{value:.3f}"

        lines.append(
            f"| {system} | {summary.score_mean:.3f} | {recall(1)} | {recall(5)} | {recall(10)} "
            f"| {summary.context_tokens.get('mean', 0):.0f} "
            f"| {summary.latency_answer_ms.get('p50', 0):.1f} "
            f"| {summary.cost_per_cycle_usd:.6f} "
            f"| {'yes' if summary.admissible_d16 else 'no'} |"
        )
    if mode == "retrieval":
        lines.append("")
        lines.append(
            "> Retrieval sufficiency is **not** answer accuracy. These rows are inadmissible "
            "as QA numbers by construction (no backbone), which is exactly the distinction "
            "MemPalace's 96.6% needs and the field's comparison tables lose."
        )
    return "\n".join(lines)
