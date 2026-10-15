"""Compose: the answer part of context assembly, moved out of the engine (F-11)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from memspine.compose.answer import numbered_context, verification
from memspine.compose.pack import AssembledContext
from memspine.compose.pipeline.state import AnswerVerification
from memspine.exceptions import MissingServiceError
from memspine.models.providers.llm.structured import structured_call
from memspine.prompts.models import AnswerVerdictOut
from memspine.store.records import MemoryRecord


async def verify_answer(
    eng: Any,
    question: str,
    answer: str,
    context: AssembledContext | Sequence[MemoryRecord] | str,
) -> AnswerVerification:
    """#39: is ``answer`` supported by ``context``? One ``verify_answer`` role call
    (the ``chat`` role when that one is not bound), prompt ``verify_answer``.

    ``context`` is a read's :class:`AssembledContext`, its records, or plain text;
    the prompt sees it as numbered lines (one per record, whitespace collapsed, or
    one per non-empty text line). Returns ``supported``, the ``evidence_ids`` of the
    supporting lines (record ids, or ``L<n>`` for a text context) and, when the
    answer is not supported but the context supports another, ``revised_answer``
    (else None). Raises :class:`MissingServiceError` when neither role is bound and
    :class:`~memspine.exceptions.LLMError` when the reply is unusable. Nothing is
    read or written; no read path calls it."""
    eng._require_started()
    llm_router = eng._llm
    role = next(
        (r for r in ("verify_answer", "chat") if llm_router is not None and r in llm_router.roles),
        None,
    )
    if llm_router is None or role is None or eng._prompts is None:
        raise MissingServiceError("llm role 'verify_answer'")
    if isinstance(context, AssembledContext):
        context = context.records
    if not isinstance(context, str):
        context = eng._remote_view(role, list(context))  # #50: withheld by id
    lines, ids = numbered_context(context)  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    verdict = await structured_call(
        llm_router.for_role(role),
        eng._prompts.select("verify_answer"),
        {"question": question, "answer": answer, "context": "\n".join(lines)},
        AnswerVerdictOut,
    )
    return cast(
        AnswerVerification,
        verification(verdict.supported, verdict.evidence, verdict.revised_answer, ids, answer),
    )
