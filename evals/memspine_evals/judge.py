"""Judges, and the scale rule that this project learned the hard way.

MAGMA reports 0.700 on LoCoMo. Mem0 reports 92.5. BMAM reports 78.45. Those
three numbers sit under one column header in every comparison table in the
field, and they are not the same measurement: MAGMA's is a *graded partial-
credit mean* from ``gpt-4o-mini`` on a continuous 0-1 scale with 0.2 rungs,
the others are accuracies. Hippocampus grades 1-5. Eywa scores retrieval
sufficiency. MemPalace's 96.6 is R@5 recall and is not an answer metric at all.

So: ``JudgeSpec`` has **no default scale**, ``Judge.scale`` is carried on every
row, and ``to_unit_interval`` refuses to convert a scale it was not told about.
Pooling across scales is therefore something a caller has to do on purpose, in
the open, rather than something the harness does by omission.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .contracts import sha256_text


class JudgeScale(str, Enum):
    """The score space a judge emits. Never inferred, never defaulted."""

    BINARY = "binary"  # {0, 1} correctness
    GRADED_01 = "graded_01"  # continuous [0, 1] partial credit (the MAGMA shape)
    GRADED_15 = "graded_15"  # ordinal 1-5 (the Hippocampus shape)
    RETRIEVAL_RECALL = "retrieval_recall"  # R@k over gold units (the MemPalace shape)

    @property
    def is_answer_metric(self) -> bool:
        """Retrieval recall measures the R stage, not the answer. A system can
        hold 100% R@5 and 40% QA accuracy; the two never share a column."""
        return self is not JudgeScale.RETRIEVAL_RECALL


def to_unit_interval(score: float, scale: JudgeScale) -> float:
    """Map a score onto [0, 1] for *aggregation within one scale only*."""
    if scale in (JudgeScale.BINARY, JudgeScale.GRADED_01, JudgeScale.RETRIEVAL_RECALL):
        return float(score)
    if scale is JudgeScale.GRADED_15:
        return (float(score) - 1.0) / 4.0
    raise ValueError(f"no unit-interval mapping declared for scale {scale!r}")


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    """Identity of the judge, for the manifest and for D16 admissibility."""

    judge_id: str
    scale: JudgeScale
    model: str | None = None
    prompt_id: str | None = None
    prompt_hash: str | None = None
    makes_model_calls: bool = False
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.scale, JudgeScale):
            raise TypeError(
                "JudgeSpec.scale must be a JudgeScale — an implied scale is not a scale"
            )
        if self.makes_model_calls and not self.model:
            raise ValueError(f"judge {self.judge_id!r} calls a model but records no model id")
        if self.makes_model_calls and not self.prompt_hash:
            raise ValueError(
                f"judge {self.judge_id!r} calls a model but records no prompt hash — "
                "an LLM judge without its prompt is not reproducible"
            )


@dataclass(frozen=True, slots=True)
class Verdict:
    score: float
    scale: JudgeScale
    raw: str = ""
    latency_ms: float = 0.0
    model_calls: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def unit_score(self) -> float:
        return to_unit_interval(self.score, self.scale)


@runtime_checkable
class Judge(Protocol):
    spec: JudgeSpec

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict: ...


# -- deterministic judges (no model calls) -----------------------------------

_NORMALISE = re.compile(r"[^a-z0-9 ]+")


def normalise_answer(text: str) -> str:
    text = text.strip().lower()
    text = _NORMALISE.sub(" ", text)
    return " ".join(text.split())


class ExactMatchJudge:
    """Binary, deterministic, zero model calls.

    Its purpose is plumbing verification and regression tests, not headline
    numbers: exact match under-reports every system equally, which is useful
    for proving the pipeline runs and useless for ranking. Runs judged this way
    are marked as such in the manifest.
    """

    def __init__(self) -> None:
        self.spec = JudgeSpec(judge_id="exact-match", scale=JudgeScale.BINARY)

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        if gold is None:
            return Verdict(score=0.0, scale=JudgeScale.BINARY, meta={"skipped": "no gold"})
        hit = normalise_answer(answer) == normalise_answer(gold)
        return Verdict(score=1.0 if hit else 0.0, scale=JudgeScale.BINARY)


class ContainsJudge:
    """Binary, deterministic: gold appears in the answer. Lenient twin of exact
    match, and the common no-LLM baseline in LoCoMo re-runs."""

    def __init__(self) -> None:
        self.spec = JudgeSpec(judge_id="contains", scale=JudgeScale.BINARY)

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        if gold is None:
            return Verdict(score=0.0, scale=JudgeScale.BINARY, meta={"skipped": "no gold"})
        g, a = normalise_answer(gold), normalise_answer(answer)
        return Verdict(score=1.0 if g and g in a else 0.0, scale=JudgeScale.BINARY)


# -- LLM judge ---------------------------------------------------------------

DEFAULT_BINARY_PROMPT = (
    "You are grading a question-answering system against a reference answer.\n"
    "Question: {question}\n"
    "Reference answer: {gold}\n"
    "System answer: {answer}\n\n"
    "Reply with exactly one token: CORRECT or INCORRECT."
)

#: Rubric judge for the D23 Qwen3 protocol. The one-token DEFAULT_BINARY_PROMPT
#: let Qwen3-32B (thinking off) label "I do not know." CORRECT against a dated
#: gold answer, so a no-memory control scored 72% on a LoCoMo pilot. This prompt
#: states the abstention rule explicitly and asks for the Mem0/LoCoMo JSON label
#: format; it agreed with hand labels on an 8-case calibration set (8/8).
RUBRIC_BINARY_PROMPT = (
    "Your task is to label an answer to a question as CORRECT or WRONG, given a gold (reference) "
    "answer.\n"
    "Rules:\n"
    "- CORRECT only if the answer states the same fact as the gold answer. Wording may differ, "
    "and a longer answer is fine if it contains the gold fact. For dates, the same day, month "
    "or year as "
    "the gold (at the gold's precision) is CORRECT, including relative forms that resolve to it.\n"
    "- WRONG if the answer gives a different fact, or says it does not know, cannot tell, or that "
    "the information is not available, unless the gold answer itself says the information is not "
    "available.\n\n"
    "Question: {question}\nGold answer: {gold}\nAnswer to label: {answer}\n\n"
    'Respond with JSON only: {{"label": "CORRECT"}} or {{"label": "WRONG"}}'
)

#: LoCoMo-Plus binary judge (T-Mem-style: the question is included). ``gold`` carries the
#: earlier cue dialogue; the reply is correct only if it explicitly acknowledges or adapts to
#: the constraint the cue established.
CONSTRAINT_BINARY_PROMPT = (
    "Earlier in a conversation, this exchange took place:\n{gold}\n\n"
    "Much later, the user wrote: {question}\n\n"
    "The assistant replied: {answer}\n\n"
    "Label the reply CORRECT only if it explicitly acknowledges or adapts to what the earlier "
    "exchange established (a cause, state, goal or value that matters for the later message). "
    "A generic reply that ignores it is WRONG.\n"
    'Respond with JSON only: {{"label": "CORRECT"}} or {{"label": "WRONG"}}'
)

DEFAULT_GRADED_PROMPT = (
    "You are grading a question-answering system against a reference answer.\n"
    "Question: {question}\n"
    "Reference answer: {gold}\n"
    "System answer: {answer}\n\n"
    "Reply with a single number between 0.0 and 1.0 giving partial credit."
)


class LLMJudge:
    """Judge backed by a chat model. Requires an explicit scale and prompt.

    The ``chat`` callable is injected rather than constructed here so the
    harness core stays dependency-free and so tests can supply a recorded
    transcript instead of a live endpoint.
    """

    def __init__(
        self,
        chat: Any,
        model: str,
        scale: JudgeScale,
        prompt: str | None = None,
        judge_id: str | None = None,
    ) -> None:
        if not isinstance(scale, JudgeScale):
            raise TypeError("LLMJudge needs an explicit JudgeScale")
        if scale is JudgeScale.RETRIEVAL_RECALL:
            raise ValueError("retrieval recall is computed from gold ids, not from a judge model")
        self._chat = chat
        self.prompt = prompt or (
            DEFAULT_BINARY_PROMPT if scale is JudgeScale.BINARY else DEFAULT_GRADED_PROMPT
        )
        self.spec = JudgeSpec(
            judge_id=judge_id or f"llm-{model}-{scale.value}",
            scale=scale,
            model=model,
            prompt_id="inline",
            prompt_hash=sha256_text(self.prompt),
            makes_model_calls=True,
        )

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        if gold is None:
            return Verdict(score=0.0, scale=self.spec.scale, meta={"skipped": "no gold"})
        started = time.perf_counter()
        raw = await self._chat(self.prompt.format(question=question, gold=gold, answer=answer))
        latency = (time.perf_counter() - started) * 1000
        return Verdict(
            score=self._parse(raw),
            scale=self.spec.scale,
            raw=raw,
            latency_ms=latency,
            model_calls=1,
        )

    def _parse(self, raw: str) -> float:
        text = raw.strip()
        if self.spec.scale is JudgeScale.BINARY:
            return parse_binary_verdict(raw)
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match is None:
            raise ValueError(f"graded judge returned no number: {raw!r}")
        value = float(match.group())
        if self.spec.scale is JudgeScale.GRADED_01 and not 0.0 <= value <= 1.0:
            raise ValueError(f"graded_01 judge returned {value} outside [0, 1]")
        if self.spec.scale is JudgeScale.GRADED_15 and not 1.0 <= value <= 5.0:
            raise ValueError(f"graded_15 judge returned {value} outside [1, 5]")
        return value


_LABEL_JSON = re.compile(r'"label"\s*:\s*"(CORRECT|WRONG|INCORRECT)"', re.I)
_VERDICT = re.compile(r"\b(INCORRECT|CORRECT|WRONG)\b", re.I)


def parse_binary_verdict(raw: str) -> float:
    """Binary judge reply -> 1.0 / 0.0.

    Accepts the Mem0/T-Mem LoCoMo judge format (``{"label": "CORRECT"|"WRONG"}``)
    and bare ``CORRECT``/``INCORRECT``/``WRONG``. A structured label wins; else
    the LAST verdict word does, so an explanation that mentions "correct"
    before concluding WRONG is not misread (the pre-fix parser scored it 1).
    """
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    labelled = _LABEL_JSON.findall(text)
    if labelled:
        return 1.0 if labelled[-1].upper() == "CORRECT" else 0.0
    words = _VERDICT.findall(text)
    if not words:
        raise ValueError(f"binary judge returned an ungradable reply: {raw!r}")
    return 1.0 if words[-1].upper() == "CORRECT" else 0.0


def recall_at_k(retrieved_ids: tuple[str, ...], gold_ids: tuple[str, ...], k: int) -> float | None:
    """R@k over gold units. ``None`` when the dataset carries no gold ids —
    the metric is unavailable, which is not the same as zero."""
    if not gold_ids:
        return None
    top = set(retrieved_ids[:k])
    return 1.0 if top & set(gold_ids) else 0.0
