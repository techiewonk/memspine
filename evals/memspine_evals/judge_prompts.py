"""Judge prompt registry and per-question routing (R3-5, R3-1).

Every judge prompt a run can use is an entry here, with its text, its provenance and a status:

* ``official-verbatim``: copied byte for byte from the benchmark's own repository
  (LoCoMo-Plus @ 059f4e3, ``evaluation_framework/task_eval/prompt.py``;
  LongMemEval @ 9e0b455, ``src/evaluation/evaluate_qa.py``; OmniMemEval @ 0b1ea8d,
  ``scripts/utils/prompts.py``);
* ``vendored-copy``: copied byte for byte from a third-party copy of the benchmark's code and
  not yet verified against upstream (none at present: LongMemEval's ``get_anscheck_prompt``,
  first taken from LightMem @ 4a9f1d6, was verified byte-identical to upstream on 2026-10-03);
* ``memspine``: written for this harness (the rubric, constraint and abstention judges);
* ``placeholder``: the official text is not on disk. The entry has no text and **refuses to
  run** (``OfficialPromptMissing``) until the text is pasted in from the source it names.

A *suite* maps each question to one entry (by benchmark category or question type), so one
run can grade LongMemEval's temporal questions with the temporal template and its abstention
questions with the abstention template. The manifest records the suite, every route's prompt
id, status, source and SHA-256, and a hash over all of them.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .contracts import Query, sha256_mapping, sha256_text
from .judge import (
    ABSTENTION_BINARY_PROMPT,
    CONSTRAINT_BINARY_PROMPT,
    RUBRIC_BINARY_PROMPT,
    RUBRIC_GUARDED_BINARY_PROMPT,
    JudgeScale,
    JudgeSpec,
    Verdict,
    parse_binary_verdict,
)
from .official_prompts import (
    LOCOMO_PLUS_TEMPLATES,
    LONGMEMEVAL_ANSCHECK,
    OMNIMEMEVAL_JUDGE,
    OMNIMEMEVAL_JUDGE_SYSTEM,
)
from .vendor_judges import (
    EVERMEMOS_JUDGE,
    EVERMEMOS_JUDGE_SYSTEM,
    MEM0_GENEROUS_JUDGE,
    MEM0_GENEROUS_JUDGE_SYSTEM,
    MEM0_UNIFIED_JUDGE,
    MEM0_UNIFIED_JUDGE_SYSTEM,
)

__all__ = [
    "JUDGE_PROMPTS",
    "JUDGE_SUITES",
    "JudgePrompt",
    "JudgeSuite",
    "OfficialPromptMissing",
    "PromptStatus",
    "RoutedLLMJudge",
]

LOCOMO_PLUS_SOURCE = (
    "github.com/xjtuleeyf/Locomo-Plus@059f4e3:evaluation_framework/task_eval/prompt.py"
    ":PROMPT_TEMPLATES"
)
LONGMEMEVAL_SOURCE = (
    "github.com/xiaowu0162/LongMemEval@9e0b455:src/evaluation/evaluate_qa.py"
    ":get_anscheck_prompt (verified byte-identical 2026-10-03; first vendored from "
    "github.com/zjunlp/LightMem@4a9f1d6)"
)
OMNIMEMEVAL_SOURCE = (
    "github.com/MemTensor/OmniMemEval@0b1ea8d:scripts/utils/prompts.py"
    ":JUDGE_PROMPT + JUDGE_SYSTEM_PROMPT (LoCoMo judge, scripts/locomo/locomo_eval.py)"
)

#: LoCoMo category ids -> LoCoMo-Plus judge category names (``data/unified_input.py``).
LOCOMO_CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal",
    3: "common-sense",
    4: "single-hop",
    5: "adversarial",
}


class PromptStatus(str, Enum):  # noqa: UP042 - matches the other str enums in the harness
    OFFICIAL = "official-verbatim"
    VENDORED = "vendored-copy"
    MEMSPINE = "memspine"
    PLACEHOLDER = "placeholder"


class OfficialPromptMissing(RuntimeError):
    """A placeholder prompt was asked to run before its official text was pasted in."""


_THINK = re.compile(r"<think>.*?</think>", re.S)
_LABEL3_JSON = re.compile(r'"label"\s*:\s*["\'](correct|partial|wrong)["\']', re.I)
_LABEL3_WORD = re.compile(r"\b(correct|partial|wrong)\b", re.I)
_LABEL3_SCORE = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}


def parse_label3(raw: str) -> float:
    """LoCoMo-Plus labels: correct = 1, partial = 0.5, wrong = 0 (``llm_as_judge.py``).

    Unlike the official parser, an unparseable reply raises (an ERROR row) instead of
    scoring 0: an ungradable reply is a missing measurement here.
    """
    text = _THINK.sub("", raw)
    found = _LABEL3_JSON.findall(text) or _LABEL3_WORD.findall(text)
    if not found:
        raise ValueError(f"LoCoMo-Plus judge returned no label: {raw!r}")
    return _LABEL3_SCORE[found[-1].lower()]


def parse_yes_no(raw: str) -> float:
    """LongMemEval anscheck reply -> 1/0, from the first line (as the vendored scorer reads it).

    The reply's first token decides; failing that, a ``yes``/``no`` word on the first line.
    No verdict at all raises instead of defaulting to 0.
    """
    text = _THINK.sub("", raw).strip().lower()
    first = text.splitlines()[0].strip() if text else ""
    tokens = re.sub(r"[.!:;]", "", first).split()
    if tokens and tokens[0] in ("yes", "y"):
        return 1.0
    if tokens and tokens[0] in ("no", "n"):
        return 0.0
    if re.search(r"\byes\b", first):
        return 1.0
    if re.search(r"\bno\b", first):
        return 0.0
    raise ValueError(f"yes/no judge returned no verdict: {raw!r}")


@dataclass(frozen=True, slots=True)
class JudgePrompt:
    """One judge template and where it came from.

    ``fields`` names the template's placeholder style: ``named`` ({question}, {gold},
    {answer}), ``positional`` (question, gold, answer in order: LongMemEval),
    ``locomo_plus`` ({gold}, {pred}, {evidence}) or ``omnimemeval`` ({question},
    {golden_answer}, {response}); the N57 vendor styles are ``mem0_generous`` ({question},
    {expected_answer}, {ai_response}), ``mem0_unified`` ({question}, {answer} = gold,
    {response}) and ``evermemos`` ({question}, {golden_answer}, {generated_answer}).
    ``parse`` names the reply format. ``system`` is the
    official system message sent with the template, when the benchmark sends one.
    """

    prompt_id: str
    text: str | None
    status: PromptStatus
    source: str
    fields: str = "named"
    parse: str = "label"
    system: str | None = None

    def require_text(self) -> str:
        if self.text is None:
            raise OfficialPromptMissing(
                f"judge prompt {self.prompt_id!r} is a placeholder: its official text is not "
                f"on disk. Paste it verbatim from {self.source} into the registry before running."
            )
        return self.text

    @property
    def sha256(self) -> str:
        return sha256_text(self.require_text())

    def render(self, question: str, gold: str, answer: str, evidence: str = "") -> str:
        text = self.require_text()
        if self.fields == "positional":
            return text.format(question, gold, answer)
        if self.fields == "locomo_plus":
            return text.format(gold=gold, pred=answer, evidence=evidence)
        if self.fields == "omnimemeval":
            return text.format(question=question, golden_answer=gold, response=answer)
        if self.fields == "mem0_generous":
            return text.format(question=question, expected_answer=gold, ai_response=answer)
        if self.fields == "mem0_unified":
            return text.format(question=question, answer=gold, response=answer)
        if self.fields == "evermemos":
            return text.format(question=question, golden_answer=gold, generated_answer=answer)
        return text.format(question=question, gold=gold, answer=answer)

    def parse_reply(self, raw: str) -> float:
        if self.parse == "yes_no":
            return parse_yes_no(raw)
        if self.parse == "label3":
            return parse_label3(raw)
        return parse_binary_verdict(raw)

    def describe(self) -> dict[str, Any]:
        info = {
            "prompt_id": self.prompt_id,
            "status": self.status.value,
            "source": self.source,
            "sha256": self.sha256,
        }
        if self.system is not None:
            info["system_sha256"] = sha256_text(self.system)
        return info


def _registry() -> dict[str, JudgePrompt]:
    prompts = [
        JudgePrompt("memspine/rubric", RUBRIC_BINARY_PROMPT, PromptStatus.MEMSPINE, "judge.py"),
        JudgePrompt(
            "memspine/rubric-guarded",
            RUBRIC_GUARDED_BINARY_PROMPT,
            PromptStatus.MEMSPINE,
            "judge.py",
        ),
        JudgePrompt(
            "memspine/constraint", CONSTRAINT_BINARY_PROMPT, PromptStatus.MEMSPINE, "judge.py"
        ),
        JudgePrompt(
            "memspine/abstention", ABSTENTION_BINARY_PROMPT, PromptStatus.MEMSPINE, "judge.py"
        ),
    ]
    prompts += [
        JudgePrompt(
            f"locomo_plus/{name}",
            text,
            PromptStatus.OFFICIAL,
            f"{LOCOMO_PLUS_SOURCE}[{name!r}]",
            fields="locomo_plus",
            parse="label3",
        )
        for name, text in LOCOMO_PLUS_TEMPLATES.items()
    ]
    prompts += [
        JudgePrompt(
            f"longmemeval/{name}",
            text,
            PromptStatus.OFFICIAL,
            f"{LONGMEMEVAL_SOURCE} [{name}]",
            fields="positional",
            parse="yes_no",
        )
        for name, text in LONGMEMEVAL_ANSCHECK.items()
    ]
    # H25's preset judge, ported verbatim with its system message.
    prompts.append(
        JudgePrompt(
            "omnimemeval/judge",
            OMNIMEMEVAL_JUDGE,
            PromptStatus.OFFICIAL,
            OMNIMEMEVAL_SOURCE,
            fields="omnimemeval",
            system=OMNIMEMEVAL_JUDGE_SYSTEM,
        )
    )
    # N57: vendor LoCoMo judges, to re-grade saved answers with each vendor's own judge.
    prompts += [
        JudgePrompt(
            "vendor/mem0-generous",
            MEM0_GENEROUS_JUDGE,
            PromptStatus.VENDORED,
            "github.com/Backboard-io/Backboard-Locomo-Benchmark@164d45c:locomo_ingest_eval.py"
            ":ACCURACY_PROMPT (Mem0 paper judge)",
            fields="mem0_generous",
            system=MEM0_GENEROUS_JUDGE_SYSTEM,
        ),
        JudgePrompt(
            "vendor/mem0-unified",
            MEM0_UNIFIED_JUDGE,
            PromptStatus.VENDORED,
            "github.com/mem0ai/memory-benchmarks@4b61c5d:benchmarks/locomo/prompts.py"
            ":JUDGE_PROMPT (no evidence)",
            fields="mem0_unified",
            system=MEM0_UNIFIED_JUDGE_SYSTEM,
        ),
        JudgePrompt(
            "vendor/evermemos",
            EVERMEMOS_JUDGE,
            PromptStatus.VENDORED,
            "EverMemOS benchmarks/run.py:JUDGE_USER_PROMPT + JUDGE_SYSTEM_PROMPT "
            "(code-traced notes, docs/survey/_staging/EverMemOS/PROMPTS.md)",
            fields="evermemos",
            system=EVERMEMOS_JUDGE_SYSTEM,
        ),
    ]
    return {p.prompt_id: p for p in prompts}


JUDGE_PROMPTS: dict[str, JudgePrompt] = _registry()


# -- routing ---------------------------------------------------------------------


def _is_abstention(query: Query) -> bool:
    return bool(query.meta.get("abstention"))


def route_rubric(query: Query) -> str:
    return "abstention" if _is_abstention(query) else "default"


def route_constant(query: Query) -> str:
    return "default"


def route_locomo_plus_v2(query: Query) -> str:
    """LoCoMo questions by category name; LoCoMo-Plus triggers are ``Cognitive``."""
    if query.meta.get("benchmark") == "locomo_plus":
        return "Cognitive"
    category = query.meta.get("category")
    if category in LOCOMO_CATEGORY_NAMES:
        return LOCOMO_CATEGORY_NAMES[category]
    return "default"


def route_longmemeval(query: Query) -> str:
    """``get_anscheck_prompt``'s dispatch: abstention first, then by question type."""
    if _is_abstention(query):
        return "abstention"
    q_type = query.type_label or ""
    if q_type in ("single-session-user", "single-session-assistant", "multi-session"):
        return "default"
    if q_type in ("temporal-reasoning", "knowledge-update", "single-session-preference"):
        return q_type
    raise ValueError(f"LongMemEval judge has no template for question type {q_type!r}")


@dataclass(frozen=True, slots=True)
class JudgeSuite:
    name: str
    scale: JudgeScale
    routes: Mapping[str, str]  # route -> prompt id
    router: Callable[[Query], str]
    handles_abstention: bool
    notes: str = ""
    needs_evidence: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)


JUDGE_SUITES: dict[str, JudgeSuite] = {
    "rubric": JudgeSuite(
        "rubric",
        JudgeScale.BINARY,
        {"default": "memspine/rubric", "abstention": "memspine/abstention"},
        route_rubric,
        handles_abstention=True,
        notes="memspine rubric; abstention questions use the abstention judge",
    ),
    "rubric-guarded": JudgeSuite(
        "rubric-guarded",
        JudgeScale.BINARY,
        {"default": "memspine/rubric-guarded", "abstention": "memspine/abstention"},
        route_rubric,
        handles_abstention=True,
        notes="rubric plus equivalent relative-date and hedged-answer rules (--judge-guards)",
    ),
    "constraint": JudgeSuite(
        "constraint",
        JudgeScale.BINARY,
        {"default": "memspine/constraint"},
        route_constant,
        handles_abstention=False,
        notes="memspine LoCoMo-Plus constraint judge (question included)",
    ),
    "locomo-plus-v2": JudgeSuite(
        "locomo-plus-v2",
        JudgeScale.GRADED_01,
        {name: f"locomo_plus/{name}" for name in LOCOMO_PLUS_TEMPLATES},
        route_locomo_plus_v2,
        handles_abstention=True,
        notes="official LoCoMo-Plus judge; correct=1, partial=0.5, wrong=0",
        needs_evidence=True,
    ),
    "longmemeval": JudgeSuite(
        "longmemeval",
        JudgeScale.BINARY,
        {name: f"longmemeval/{name}" for name in LONGMEMEVAL_ANSCHECK},
        route_longmemeval,
        handles_abstention=True,
        notes="LongMemEval anscheck templates by question type (verified against upstream)",
    ),
    "omnimemeval": JudgeSuite(
        "omnimemeval",
        JudgeScale.BINARY,
        {"default": "omnimemeval/judge"},
        route_constant,
        handles_abstention=False,
        notes="official OmniMemEval LoCoMo judge (CORRECT/WRONG JSON label)",
    ),
    "mem0-generous": JudgeSuite(
        "mem0-generous",
        JudgeScale.BINARY,
        {"default": "vendor/mem0-generous"},
        route_constant,
        handles_abstention=False,
        notes="N57 vendor judge: Mem0 paper 'be generous' judge (Backboard, Hindsight harness)",
    ),
    "mem0-unified": JudgeSuite(
        "mem0-unified",
        JudgeScale.BINARY,
        {"default": "vendor/mem0-unified"},
        route_constant,
        handles_abstention=False,
        notes="N57 vendor judge: Mem0 memory-benchmarks; partial credit, 14-day dates",
    ),
    "evermemos": JudgeSuite(
        "evermemos",
        JudgeScale.BINARY,
        {"default": "vendor/evermemos"},
        route_constant,
        handles_abstention=False,
        notes="N57 vendor judge: EverMemOS judge (generous text, label-only JSON)",
    ),
}


class RoutedLLMJudge:
    """An LLM judge that grades each question with its suite's template for that question.

    Construction fails with ``OfficialPromptMissing`` when any route is a placeholder, so a
    run cannot start with a judge it cannot faithfully run.
    """

    def __init__(
        self,
        chat: Any,
        model: str,
        suite: str,
        judge_id: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        if suite not in JUDGE_SUITES:
            raise ValueError(f"unknown judge suite {suite!r}; known: {sorted(JUDGE_SUITES)}")
        self.suite = JUDGE_SUITES[suite]
        self._chat = chat
        self.prompts = {route: JUDGE_PROMPTS[pid] for route, pid in self.suite.routes.items()}
        for prompt in self.prompts.values():
            prompt.require_text()
        self.handles_abstention = self.suite.handles_abstention
        routes = {route: prompt.describe() for route, prompt in self.prompts.items()}
        self.spec = JudgeSpec(
            judge_id=judge_id or f"llm-{model}-{suite}",
            scale=self.suite.scale,
            model=model,
            prompt_id=f"suite:{suite}",
            prompt_hash=sha256_mapping({route: info["sha256"] for route, info in routes.items()}),
            makes_model_calls=True,
            params={
                **dict(getattr(chat, "params", {}) or {}),
                **dict(params or {}),
                "suite": suite,
                "suite_notes": self.suite.notes,
                "routes": routes,
            },
        )

    def prompt_for(self, query: Query) -> JudgePrompt:
        route = self.suite.router(query)
        if route not in self.prompts:
            route = "default"
        return self.prompts[route]

    async def _grade(
        self, prompt: JudgePrompt, question: str, gold: str, answer: str, evidence: str
    ) -> Verdict:
        started = time.perf_counter()
        rendered = prompt.render(question, gold, answer, evidence)
        if prompt.system is not None:
            raw = await self._chat(rendered, system=prompt.system)
        else:
            raw = await self._chat(rendered)
        return Verdict(
            score=prompt.parse_reply(raw),
            scale=self.spec.scale,
            raw=raw,
            latency_ms=(time.perf_counter() - started) * 1000,
            model_calls=1,
            meta={"prompt_id": prompt.prompt_id},
        )

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        if "default" not in self.prompts:
            raise ValueError(f"suite {self.suite.name!r} routes per question; use score_query")
        if gold is None:
            return Verdict(score=0.0, scale=self.spec.scale, meta={"skipped": "no gold"})
        return await self._grade(self.prompts["default"], question, gold, answer, "")

    async def score_query(self, query: Query, answer: str) -> Verdict:
        prompt = self.prompt_for(query)
        gold_free = prompt.prompt_id in (
            "memspine/abstention",
            "locomo_plus/adversarial",
            "locomo_plus/Cognitive",
        )
        if query.gold is None and not gold_free:
            return Verdict(score=0.0, scale=self.spec.scale, meta={"skipped": "no gold"})
        evidence = str(query.meta.get("judge_evidence", ""))
        return await self._grade(prompt, query.text, query.gold or "", answer, evidence)
