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

from .contracts import DatasetAdapter, Reader, SystemAdapter
from .judge import RUBRIC_BINARY_PROMPT, ContainsJudge, Judge, JudgeScale, LLMJudge
from .metrics import CostModel
from .provenance import RunProtocol
from .readers import ContextOnlyReader, OpenAICompatReader, openai_compat_chat
from .results import RunSummary
from .runner import RunConfig, run_matrix
from .systems import (
    BM25Retriever,
    FullContextSystem,
    NaiveRAGSystem,
    NoMemorySystem,
    VerbatimSystem,
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
    base_url: str = "http://localhost:11434/v1"
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
    if config.include_memspine:
        from .systems.memspine_system import MemspineSystem

        systems.append(
            MemspineSystem(config=config.memspine_config, read_mode=config.memspine_read_mode)
        )
    return systems


def build_reader_and_judge(config: C01Config) -> tuple[Reader, Judge, bool]:
    """Returns ``(reader, judge, makes_model_calls)`` for the chosen mode."""
    if config.mode == "retrieval":
        # No generation: "was the answer retrievable at all". This is what R@k
        # and MemPalace's 96.6 measure, and it costs nothing to run.
        return ContextOnlyReader(), ContainsJudge(), False
    if config.bedrock:
        from .bedrock import QWEN3_32B, CallBudget, LiteLLMReader, litellm_chat

        if config.max_model_calls is None:
            raise ValueError("bedrock qa needs max_model_calls (the D23 budget cap)")
        budget = CallBudget(
            max_calls=config.max_model_calls, prices_per_mtok={QWEN3_32B: (0.16, 0.62)}
        )
        bedrock_reader = LiteLLMReader(budget, model=QWEN3_32B, temperature=0.0, max_tokens=256)
        bedrock_judge = LLMJudge(
            litellm_chat(budget, model=QWEN3_32B),
            model=QWEN3_32B,
            scale=JudgeScale.BINARY,
            prompt=RUBRIC_BINARY_PROMPT,
            judge_id="qwen3-32b-rubric-binary",
        )
        return bedrock_reader, bedrock_judge, True
    reader = OpenAICompatReader(model=config.reader_model, base_url=config.base_url)
    judge = LLMJudge(
        openai_compat_chat(config.judge_model, base_url=config.base_url),
        model=config.judge_model,
        scale=JudgeScale.BINARY,
    )
    return reader, judge, True


async def run_c0_1(
    dataset: DatasetAdapter,
    config: C01Config,
    out_dir: Path,
    run_id: str | None = None,
) -> list[RunSummary]:
    reader, judge, calls = build_reader_and_judge(config)
    protocol = RunProtocol(
        protocol_id=f"c0-1-{config.mode}",
        budget_tokens=config.budget_tokens,
        top_k=config.top_k,
        seed=config.seed,
        notes="C0-1 verbatim-baseline gate; identical protocol across arms",
    )
    info = dataset.info()
    run_config = RunConfig(
        run_id=run_id or f"c0-1-{config.mode}-{info.dataset_id}",
        protocol=protocol,
        out_dir=out_dir,
        cost_model=CostModel(),
        max_items=config.max_items,
        max_model_calls=config.max_model_calls,
        expect_model_calls=calls,
        labels={
            "experiment": "C0-1",
            "question": "does verbatim storage beat extraction on our own harness",
            "mode": config.mode,
            "dense": config.dense,
            "hybrid": config.hybrid,
        },
    )
    return await run_matrix(dataset, build_systems(config), reader, judge, run_config)


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
