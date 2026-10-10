"""Task-level decider port (I28), ``services/decision``.

A *decider* answers one yes/no decision of the pipeline ("is this answer a refusal?", "does
this question ask for a set?", "does the read need a second hop?", "are the retrieved memories
relevant to the message?") and returns a :class:`Decision` (label, confidence, raw output).

Two adapters implement it:

* :class:`HeuristicDecider` wraps the regexes and rules the pipeline already has. It is the
  default and changes nothing.
* :class:`OpenDeciderDecider` asks OpenDecider-nano (``manjunathshiva/opendecider-nano``,
  Apache-2.0, ~400M parameters, ModernBERT encoder). One forward pass returns a calibrated
  probability per option; nothing is generated. Needs the optional ``[decider]`` extra.

The decider is an enhancer, never a gate: callers fall back to the heuristic when it is below
their confidence threshold or fails. It never receives gold labels or benchmark categories:
only the question, and the answer or the retrieved text where the task needs it.

Inference is memspine's own port of the OpenDecider-nano forward pass (``opendecider_nano``,
Apache-2.0, see NOTICE); the ``opendecider`` package is not used. Dependencies are the ones the
``[decider]`` extra names: torch, transformers, safetensors and huggingface_hub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from memspine.services.decision.opendecider_nano import (
    DEFAULT_MODEL,
    load_nano,
    noul_options,
    physical_cores,
)

__all__ = [
    "DEFAULT_MODEL",
    "TASKS",
    "Decider",
    "Decision",
    "HeuristicDecider",
    "OpenDeciderDecider",
    "TaskSpec",
    "build_decider",
    "default_rules",
]

#: The model reads at most 2,048 tokens; this keeps the text it is handed to a sane size.
_MAX_STATE_CHARS = 7000


@dataclass(frozen=True)
class Decision:
    """One decision. ``confidence`` is the probability of ``label`` (None = unknown)."""

    label: str
    confidence: float | None = None
    raw: Any = None
    task: str = ""
    adapter: str = "heuristic"

    def as_meta(self, *, used: bool | None = None) -> dict[str, Any]:
        """The JSON-safe record kept in forensics / row meta."""
        out: dict[str, Any] = {
            "task": self.task,
            "label": self.label,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "adapter": self.adapter,
        }
        if used is not None:
            out["used"] = used
        return out


@dataclass(frozen=True)
class TaskSpec:
    """A yes/no decision point: the question put to the model and the label of each answer."""

    instructions: str
    yes: str
    no: str
    #: how ``question`` and ``context`` are laid out as the model's state
    layout: str = "question"


#: The decision points. ``question`` is always the user's question (or message); ``context`` is
#: the first answer for ``refusal`` / ``abstention`` and the retrieved memories for ``relevance``
#: and ``bridge_hop``.
TASKS: dict[str, TaskSpec] = {
    "refusal": TaskSpec(
        "Does the answer decline to answer, say it does not know, or say the information is "
        "not mentioned, instead of giving a concrete answer?",
        "refusal",
        "answer",
        "question_answer",
    ),
    "list_mode": TaskSpec(
        "Does the question ask for a list or set of several items (activities, books, places, "
        "people), rather than one single fact?",
        "set",
        "single",
    ),
    "bridge_hop": TaskSpec(
        "Does the question describe the thing it asks about instead of naming it, so that a "
        "second lookup is needed to find that thing first?",
        "hop",
        "no_hop",
        "question_memories",
    ),
    "relevance": TaskSpec(
        "Do the memories contain information that is relevant and useful for answering the "
        "message?",
        "relevant",
        "irrelevant",
        "message_memories",
    ),
    "temporal_intent": TaskSpec(
        "Does the question ask when something happened, or ask about a date, a time or an order "
        "in time?",
        "temporal",
        "not_temporal",
    ),
    # I39 perspective axes (write time, one turn of text; see core/perspective.py)
    "is_fact": TaskSpec(
        "Does the text state something as a fact that holds, rather than a plan, a wish, a "
        "hypothetical, an opinion, a question or a request?",
        "fact",
        "not_fact",
    ),
    "is_negated": TaskSpec(
        "Does the text negate or deny what it describes (not, never, no longer, don't)?",
        "negated",
        "affirmed",
    ),
    "is_standing": TaskSpec(
        "Does the text state a lasting trait, preference or habit of a person, rather than a "
        "one-off event?",
        "standing",
        "one_off",
    ),
    "is_hedged": TaskSpec(
        "Does the speaker hedge or express uncertainty about what they say?",
        "hedged",
        "certain",
    ),
    "sensitivity": TaskSpec(
        "Does the text disclose a sensitive personal detail about a person, such as health, "
        "sexuality or gender identity, religion, politics, finances, legal trouble, a home "
        "address or a credential?",
        "sensitive",
        "not_sensitive",
    ),
    "sensitivity_scope": TaskSpec(
        "Is the message asking about the user's own sensitive personal details, such as "
        "health, sexuality or gender identity, religion, politics, finances, legal matters, "
        "a home address or credentials?",
        "about",
        "not_about",
    ),
    # I47: the question slot is the user's reply, the context the assistant turn before it
    "acknowledges": TaskSpec(
        "Does the reply agree with, confirm or accept what the context says, rather than "
        "deny it, correct it or change the subject?",
        "ack",
        "no_ack",
        "question_context",
    ),
    # I59: question = the question, context = one memory line
    "about_target": TaskSpec(
        "Does the memory line state something about the person the question asks about, "
        "rather than about someone else?",
        "about",
        "not_about",
        "question_context",
    ),
    "abstention": TaskSpec(
        "Can the question be answered from the context?",
        "answerable",
        "unanswerable",
        "question_context",
    ),
}


@runtime_checkable
class Decider(Protocol):
    """``decide`` returns the :class:`Decision` for ``task`` on ``question`` (+ ``context``)."""

    decider_id: str

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision: ...


def _state(spec: TaskSpec, question: str, context: str | None) -> str:
    ctx = (context or "").strip()[:_MAX_STATE_CHARS]
    if spec.layout == "question" or not ctx:
        return f"Question: {question.strip()}"
    if spec.layout == "question_answer":
        return f"Question: {question.strip()}\nAnswer: {ctx}"
    if spec.layout == "message_memories":
        return f"Message: {question.strip()}\nMemories:\n{ctx}"
    if spec.layout == "question_memories":
        return f"Question: {question.strip()}\nFirst-pass memories:\n{ctx}"
    return f"Question: {question.strip()}\nContext:\n{ctx}"


# --- heuristic adapter --------------------------------------------------------------------

#: ``rule(question, context) -> (label, confidence)``
Rule = Callable[[str, str | None], tuple[str, float | None]]


def default_rules() -> dict[str, Rule]:
    """The pipeline's existing rules, per task. ``refusal`` is added by the evals harness
    (its regex lives there). ``relevance`` and ``abstention`` have no rule today: the pipeline
    always injects, so the heuristic answer is "relevant" / "answerable"."""
    from memspine.core.perspective import (
        acknowledges,
        is_hedged,
        is_negated,
        scope_of,
        sentence_modality,
    )
    from memspine.core.query_shape import is_set_question, is_temporal
    from memspine.core.sensitivity import grade_text, query_topics
    from memspine.core.temporal_query import has_bridge_cue

    return {
        "is_fact": lambda q, _c: (
            "fact" if sentence_modality(q) in ("fact", "opinion") else "not_fact",
            None,
        ),
        "is_negated": lambda q, _c: ("negated" if is_negated(q) else "affirmed", None),
        "is_standing": lambda q, _c: (
            "standing" if "standing" in scope_of(q) or "habit" in scope_of(q) else "one_off",
            None,
        ),
        "is_hedged": lambda q, _c: ("hedged" if is_hedged(q) else "certain", None),
        "acknowledges": lambda q, _c: ("ack" if acknowledges(q) else "no_ack", None),
        "list_mode": lambda q, _c: ("set" if is_set_question(q) else "single", None),
        "bridge_hop": lambda q, _c: ("hop" if has_bridge_cue(q) else "no_hop", None),
        "temporal_intent": lambda q, _c: (
            "temporal" if is_temporal(q) else "not_temporal",
            None,
        ),
        "relevance": lambda _q, _c: ("relevant", None),
        "abstention": lambda _q, _c: ("answerable", None),
        "about_target": lambda _q, _c: ("about", None),
        "sensitivity": lambda q, _c: (
            "sensitive" if grade_text(q).grade != "none" else "not_sensitive",
            None,
        ),
        "sensitivity_scope": lambda q, _c: (
            "about" if query_topics(q) else "not_about",
            None,
        ),
    }


class HeuristicDecider:
    """The existing regexes and rules behind the :class:`Decider` port. Deterministic."""

    decider_id = "heuristic"

    def __init__(self, rules: Mapping[str, Rule] | None = None) -> None:
        self.rules: dict[str, Rule] = {**default_rules(), **(rules or {})}

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision:
        return self.decide_sync(task, question, context)

    def decide_sync(self, task: str, question: str, context: str | None = None) -> Decision:
        if task not in self.rules:
            raise KeyError(f"no heuristic rule for decision task {task!r}")
        label, confidence = self.rules[task](question, context)
        return Decision(label, confidence, None, task, self.decider_id)


# --- OpenDecider adapter ------------------------------------------------------------------


class OpenDeciderDecider:
    """OpenDecider-nano behind the :class:`Decider` port, through memspine's own inference
    code (:mod:`memspine.services.decision.opendecider_nano`; no ``opendecider`` package).

    Every task is a ``noul`` (yes/no) question; the label is the task's ``yes`` label when
    P(yes) >= 0.5, else its ``no`` label, and the confidence is max(P(yes), 1 - P(yes)).
    The model is a process-wide singleton per (name, device), loaded on first use.
    """

    decider_id = "opendecider"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str = "cpu",
        *,
        threads: int = 0,
        backend: str = "torch",
        workers: int = 1,
        dtype: str = "float32",
    ) -> None:
        self.model = model
        self.dtype = dtype
        self.device = device
        self.backend = backend
        self.workers = max(1, workers)
        #: intra-op threads per worker: the physical cores split between the workers
        self.threads = threads or max(1, physical_cores() // self.workers)
        self._pool: ThreadPoolExecutor | None = None

    def _loaded(self) -> Any:
        return load_nano(
            self.model, self.device, self.dtype, backend=self.backend, threads=self.threads
        )

    @staticmethod
    def _parse(task: str, p_yes: float, info: Mapping[str, Any] | None = None) -> Decision:
        spec = TASKS[task]
        return Decision(
            spec.yes if p_yes >= 0.5 else spec.no,
            max(p_yes, 1.0 - p_yes),
            {"p_yes": p_yes, **(info or {})},
            task,
            OpenDeciderDecider.decider_id,
        )

    def decide_sync(self, task: str, question: str, context: str | None = None) -> Decision:
        return self.decide_batch_sync(task, [(question, context)])[0]

    def decide_batch_sync(
        self, task: str, items: Sequence[tuple[str, str | None]]
    ) -> list[Decision]:
        """The same task for many ``(question, context)`` pairs in one padded batch."""
        if not items:
            return []
        if task not in TASKS:
            raise KeyError(f"unknown decision task {task!r}")
        spec = TASKS[task]
        states = [_state(spec, q, c) for q, c in items]
        probs = self._loaded().yes_probability(states, spec.instructions)
        return [self._parse(task, p) for p in probs]

    def decide_many_sync(self, items: Sequence[tuple[str, str, str | None]]) -> list[Decision]:
        """``(task, question, context)`` triples of any tasks in ONE batched call: the inputs
        are sorted by length and bucketed to keep padding small."""
        if not items:
            return []
        for task, _, _ in items:
            if task not in TASKS:
                raise KeyError(f"unknown decision task {task!r}")
        options = noul_options()
        batch = [(_state(TASKS[t], q, c), TASKS[t].instructions, options) for t, q, c in items]
        probs = self._loaded().decide_many(batch)
        return [self._parse(t, p["yes"]) for (t, _, _), p in zip(items, probs, strict=True)]

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self.workers == 1:
            return await asyncio.to_thread(fn, *args)
        if self._pool is None:
            self._pool = ThreadPoolExecutor(self.workers, thread_name_prefix="decider")
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision:
        return await self._run(self.decide_sync, task, question, context)

    async def decide_batch(
        self, task: str, items: Sequence[tuple[str, str | None]]
    ) -> list[Decision]:
        return await self._run(self.decide_batch_sync, task, list(items))

    async def decide_many(self, items: Sequence[tuple[str, str, str | None]]) -> list[Decision]:
        return await self._run(self.decide_many_sync, list(items))


def build_decider(
    kind: str,
    model: str = DEFAULT_MODEL,
    device: str = "cpu",
    *,
    threads: int = 0,
    backend: str = "torch",
    workers: int = 1,
    dtype: str = "float32",
) -> Decider:
    """``heuristic`` | ``opendecider`` -> the adapter (the model loads on first use)."""
    if kind == "heuristic":
        return HeuristicDecider()
    if kind == "opendecider":
        return OpenDeciderDecider(
            model, device, threads=threads, backend=backend, workers=workers, dtype=dtype
        )
    raise ValueError(f"decider must be 'heuristic' or 'opendecider', got {kind!r}")
