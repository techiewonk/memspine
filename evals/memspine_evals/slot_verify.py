"""E06: an answer-slot verifier, a reader post-step (``--verify-slots``).

After the reader answers, code checks the answer against the question's typed contract
(``memspine.core.query_contract`` / ``memspine.core.answer_check``): does a "when" answer hold a
date, a "how many" answer a number, a "which city" answer a city and not a country, a yes/no
answer a polarity, a count equal the items listed, and (when the reader kept a list or the
caller has an evidence table) every listed item appear in the answer. Code decides; the model
is not asked whether the answer is fine, so the verifier cannot be correlated with the reader.

Only an answer with a named defect gets ONE bounded repair call: the inner reader is asked the
same question again with the previous answer and the specific defect appended, over the same
context (the same memories, no new evidence). The repair is kept only when it is not an
abstention and the code check no longer finds the defect; otherwise the first answer stands.
An answer that already abstains ("not mentioned", "cannot be determined") is never repaired:
an evidence-based unknown must not become a guess.

G01 (``diagnosed`` mode): a retry only for a DIAGNOSED defect. On top of the strict checks the
code diagnoses an unsupported claim (a name no memory line contains), an operand / result
inconsistency (``a - b = c`` that does not hold, a duration its own two dates do not give) and an
unjustified refusal (an abstention although a context line names every subject and the relation;
see ``defect_diagnosis.py``). The retry uses the same context, names the defect (and quotes the
evidence line for a refusal), is kept only when the defect is gone, and a retry that answers a
refusal must be supported by the cited line, so an evidence-based unknown (no such line) is never
turned into a guess. Cost (calls and tokens) is recorded in ``meta["slot_verify"]["retry_cost"]``.

Reader-agnostic: it needs only ``question``, ``context`` and the inner reader's ``answer``;
nothing is read from the gold, the dataset or the category (rule I37). Off (the default) no
wrapper is built, so rows are byte-identical. Cost: one extra reader call per flagged answer,
recorded in the row's ``meta["slot_verify"]`` and added to the answer's token counts and
``model_calls``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from .contracts import ReaderAnswer
from .defect_diagnosis import DIAGNOSIS_VERSION, supported_by

__all__ = [
    "SLOT_VERIFY_MODES",
    "SLOT_VERIFY_VERSION",
    "SlotVerifyReader",
    "repair_question",
]

SLOT_VERIFY_VERSION = "v1"
#: ``strict`` repairs only the strict defects; ``soft`` also acts on the noisier ``soft_*``
#: signals (an unnamed place / person / title); ``diagnosed`` (G01) is ``strict`` plus the
#: diagnoses of ``defect_diagnosis.py`` (unsupported claim, operand / result mismatch,
#: unjustified refusal).
SLOT_VERIFY_MODES = ("strict", "soft", "diagnosed")

REPAIR_TEMPLATE = (
    "{question}\n\n"
    "A previous answer to this question was: {previous}\n"
    "It has this problem: {defects}\n"
    "{type_line}"
    "Answer the question again from the same memories. Fix only that problem and keep what "
    "was right. If the memories do not contain what is asked, reply exactly: Not mentioned in "
    "the conversation."
)


def repair_question(question: str, previous: str, defects_text: str, type_line: str) -> str:
    """The question text of the repair call (the inner reader wraps it in its own prompt)."""
    return REPAIR_TEMPLATE.format(
        question=question.strip(),
        previous=previous.strip()[:600],
        defects=defects_text.strip(),
        type_line=(type_line + "\n") if type_line else "",
    )


_NUMBER_WORDS = {
    w: i
    for i, w in enumerate(
        ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
    )
}
_STATED = re.compile(rf"\b(\d+|{'|'.join(_NUMBER_WORDS)})\b", re.I)


def _count_defects(table: Any, text: str) -> list[Any]:
    """A04/A06: the number a count answer states against the evidence table's. A stated number
    below the table's count (or, for a ``range``, below its low end), or different from a
    ``resolved`` table's count, is a defect that names the table's items; a number above a
    range's low end stays (the lines state a larger total). An answer with no number is not
    judged here."""
    from memspine.core.answer_check import Defect

    m = _STATED.search(text)
    if not m:
        return []
    tok = m.group(1).lower()
    stated = int(tok) if tok.isdigit() else _NUMBER_WORDS[tok]
    ok = stated >= table.low if table.status == "range" else stated == table.count
    if ok:
        return []
    names = "; ".join(table.item_names()[:10])
    return [
        Defect(
            "count_table_mismatch",
            f"the answer says {stated} but the memories support {table.count} distinct "
            f"item(s): {names}",
        )
    ]


def _is_abstention(text: str) -> bool:
    from memspine.core.answer_check import is_abstention

    from .refusal import is_refusal

    return is_refusal(text) or is_abstention(text)


class SlotVerifyReader:
    """Wraps a reader (see the module docstring)."""

    def __init__(
        self,
        inner: Any,
        mode: str = "strict",
        *,
        contract_fn: Callable[[str], Any] | None = None,
        table: Any = None,
    ) -> None:
        if mode not in SLOT_VERIFY_MODES:
            raise ValueError(f"slot-verify mode must be one of {SLOT_VERIFY_MODES}, got {mode!r}")
        if contract_fn is None:
            from memspine.core.query_contract import build_contract

            contract_fn = build_contract
        self.inner = inner
        self.mode = mode
        self._contract = contract_fn
        #: E06 + A04: an ``evidence_table.EvidenceTableBuilder``; for a many/count question its
        #: supported items are the evidence the answer's list is checked against.
        self.table = table
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+slots-{mode}"
        self.model = inner.model
        self.makes_model_calls = True
        #: counters for the run log
        self.seen = 0
        self.flagged = 0
        self.repaired = 0
        self.rejected = 0
        self.extra_calls = 0
        #: G01: defects diagnosed, by kind, and the tokens the retries cost
        self.diagnosed: dict[str, int] = {}
        self.retry_prompt_tokens = 0
        self.retry_completion_tokens = 0

    def describe(self) -> Mapping[str, Any]:
        return {
            **self.inner.describe(),
            "slot_verify": self.mode,
            "slot_verify_version": SLOT_VERIFY_VERSION,
            **({"diagnosis_version": DIAGNOSIS_VERSION} if self.mode == "diagnosed" else {}),
            **(dict(self.table.describe()) if self.table is not None else {}),
        }

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        self.seen += 1
        meta: dict[str, Any] = {"version": SLOT_VERIFY_VERSION, "mode": self.mode}
        cost = {"p": 0, "c": 0, "n": 0}  # the evidence-table call, when this row paid for it
        try:
            result = await self._verify(question, context, question_date, first, meta, cost)
        except Exception as exc:  # an enhancer, never a gate: the reader's answer stands
            meta.update({"outcome": "error", "error": str(exc)[:200]})
            result = first
        if cost["n"]:
            result = replace(
                result,
                prompt_tokens=result.prompt_tokens + cost["p"],
                completion_tokens=result.completion_tokens + cost["c"],
                model_calls=result.model_calls + cost["n"],
            )
        return replace(result, extra_meta={**dict(result.extra_meta), "slot_verify": meta})

    async def _verify(
        self,
        question: str,
        context: str,
        question_date: str | None,
        first: ReaderAnswer,
        meta: dict[str, Any],
        cost: dict[str, int] | None = None,
    ) -> ReaderAnswer:
        from memspine.core.answer_check import check_answer, defect_instruction

        contract = self._contract(question)
        meta["contract"] = contract.as_meta()
        diagnosed = self.mode == "diagnosed"
        if not contract.known and not diagnosed:  # the G01 diagnoses need no answer type
            meta["outcome"] = "skipped_unknown_type"
            return first
        refusal_evidence: list[str] = []
        refusal_defects: list[Any] = []
        if not first.text.strip() or _is_abstention(first.text):
            if not (diagnosed and first.text.strip()):
                meta["outcome"] = "skipped_abstention"  # an unknown stays unknown
                return first
            from .defect_diagnosis import diagnose

            found = diagnose(
                question,
                context,
                first.text,
                subjects=contract.subjects,
                relation=contract.relation,
                abstained=True,
            )
            if not found.defects:
                meta["outcome"] = "skipped_abstention"  # no evidence line: an evidence-based unknown
                meta["refusal_diagnosis"] = "no_evidence"
                return first
            refusal_defects, refusal_evidence = found.defects, found.evidence
        soft = self.mode == "soft"
        evidence: list[str] = []
        count_table: Any = None
        if self.table is not None and contract.cardinality in ("many", "count"):
            table, p_tok, c_tok, calls = await self.table.build(question, context)
            if cost is not None:
                cost["p"] += p_tok
                cost["c"] += c_tok
                cost["n"] += calls
            if table is not None:
                meta["evidence_table"] = {
                    "status": table.status,
                    "low": table.low,
                    "high": table.high,
                    "items": [r.as_meta() for r in table.items],
                    "note": table.note,
                }
                # only a table without conflict is evidence; an unresolved one is not
                if table.usable:
                    if contract.cardinality == "many":
                        evidence = table.evidence_keys()
                    else:
                        count_table = table

        def table_defects(text: str) -> list[Any]:
            return _count_defects(count_table, text) if count_table is not None else []

        def diagnosed_defects(text: str, raw: str | None) -> list[Any]:
            if not diagnosed:
                return []
            from .defect_diagnosis import diagnose

            return diagnose(
                question,
                context,
                text,
                subjects=contract.subjects,
                relation=contract.relation,
                abstained=False,
                explanation=raw or "",
            ).defects

        if refusal_defects:  # G01: the abstention itself is the diagnosed defect
            defects = list(refusal_defects)
        else:
            defects = [
                *check_answer(
                    contract,
                    first.text,
                    explanation=first.raw_text or "",
                    evidence_items=evidence,
                    abstained=False,
                    soft=soft,
                ),
                *table_defects(first.text),
                *diagnosed_defects(first.text, first.raw_text),
            ]
        if not defects:
            meta["outcome"] = "clean"
            return first
        self.flagged += 1
        meta["defects"] = [d.as_meta() for d in defects]
        for d in defects:
            if diagnosed:
                self.diagnosed[d.kind] = self.diagnosed.get(d.kind, 0) + 1
        meta["previous_answer"] = first.text
        type_line = f"The answer should be a {contract.type_label.replace(':', ' ')}."
        asked = repair_question(question, first.text, defect_instruction(defects), type_line)
        fixed: ReaderAnswer = await self.inner.answer(asked, context, question_date)
        self.extra_calls += max(fixed.model_calls, 1)
        if diagnosed:
            self.retry_prompt_tokens += fixed.prompt_tokens
            self.retry_completion_tokens += fixed.completion_tokens
            meta["diagnosis"] = {"version": DIAGNOSIS_VERSION, "kinds": [d.kind for d in defects]}
            meta["retry_cost"] = {
                "model_calls": max(fixed.model_calls, 1),
                "prompt_tokens": fixed.prompt_tokens,
                "completion_tokens": fixed.completion_tokens,
            }
        meta["repair"] = {
            "answer": fixed.text,
            "model_calls": fixed.model_calls,
            "prompt_tokens": fixed.prompt_tokens,
            "completion_tokens": fixed.completion_tokens,
            "latency_ms": fixed.latency_ms,
        }
        kept = first
        if not fixed.text.strip():
            meta["outcome"] = "rejected_empty"
        elif _is_abstention(fixed.text):
            meta["outcome"] = "rejected_abstention"
        else:
            again = {
                d.kind
                for d in (
                    *check_answer(
                        contract,
                        fixed.text,
                        explanation=fixed.raw_text or "",
                        evidence_items=evidence,
                        abstained=False,
                        soft=soft,
                    ),
                    *table_defects(fixed.text),
                    *diagnosed_defects(fixed.text, fixed.raw_text),
                )
            }
            if again & {d.kind for d in defects}:
                meta["outcome"] = "rejected_still_defective"
            elif refusal_evidence and not supported_by(fixed.text, refusal_evidence, question):
                meta["outcome"] = "rejected_unsupported"  # a guess never replaces an unknown
            else:
                meta["outcome"] = "repaired"
                kept = fixed
        if kept is first:
            self.rejected += 1
        else:
            self.repaired += 1
        return ReaderAnswer(
            text=kept.text,
            prompt_tokens=first.prompt_tokens + fixed.prompt_tokens,
            completion_tokens=first.completion_tokens + fixed.completion_tokens,
            latency_ms=first.latency_ms + fixed.latency_ms,
            model_calls=first.model_calls + fixed.model_calls,
            truncated=kept.truncated,
            cached_prompt_tokens=first.cached_prompt_tokens + fixed.cached_prompt_tokens,
            finish_reason=kept.finish_reason,
            raw_text=kept.raw_text,
            prompt_variant=first.prompt_variant,
            extra_meta=dict(first.extra_meta),
        )
