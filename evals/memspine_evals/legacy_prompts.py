"""Legacy (contaminated) reader prompts, kept ONLY to reproduce runs made before 2026-10-11.

``grounded`` and ``grounded_detail`` printed a LoCoMo gold answer ("the week before 9 June 2023")
and a LoCoMo date as examples. They are reachable only as ``--qa-prompt grounded_legacy`` and
``grounded_detail_legacy``. Never use them for a new result: absolute LoCoMo numbers from them
are contaminated. ``test_prompt_gold_lint`` exempts this one file and checks that nothing else
imports it except ``readers.py`` (which registers only the ``*_legacy`` names).
"""

from __future__ import annotations

GROUNDED_LEGACY_QA_PROMPT = (
    "Answer the question from the memories below. Each memory is one line: [YYYY-MM-DD] is "
    'the date it was said, and a bracket like "last Saturday [= 2023-05-20]" gives the '
    "absolute date of that relative phrase. Resolve other relative times (yesterday, last "
    "week, two days ago) against the date of the line they appear in, not today's date. "
    "Give a short, direct answer. Give the best-supported answer from the memories, even if "
    "it is indirect; say it is not mentioned only when nothing in the memories bears on the "
    "question. When a date is asked, answer in the wording the memories use (for example "
    '"the week before 9 June 2023" or "2022"), at the precision asked.\n\n'
    "Memories:\n{context}\n\nQuestion: {question}\nAnswer:"
)

GROUNDED_DETAIL_LEGACY_QA_PROMPT = (
    "Answer the question from the memories below. Each memory is one line: [YYYY-MM-DD] is "
    'the date it was said, and a bracket like "last Saturday [= 2023-05-20]" gives the '
    "absolute date of that relative phrase. Resolve other relative times (yesterday, last "
    "week, two days ago) against the date of the line they appear in, not today's date. "
    "If lines start with * or [hit k], those are the memories retrieved as most relevant "
    "(k = rank); unmarked lines are surrounding conversation. Answer in one sentence that "
    "includes the specific detail from the memory (names, objects, places, numbers). When the "
    'question asks for several items or "how many", list every matching item found across '
    "the memories, then count them. Give the best-supported answer from the memories, even "
    "if it is indirect; say it is not mentioned only when nothing in the memories bears on "
    "the question. When a date is asked, answer in the wording the memories use (for example "
    '"the week before 9 June 2023" or "2022"), at the precision asked.\n\n'
    "Memories:\n{context}\n\nQuestion: {question}\nAnswer:"
)

#: I79: ``grounded`` and ``grounded_detail`` print two concrete dates as examples ("last
#: Saturday [= 2023-05-20]", "the week before 9 June 2023"); full-persp-loc q 3-54 answered
#: "The week before 9 June 2023" for a question whose evidence carries no such date. These
#: variants keep every other word and put a placeholder where the example date was. The
#: originals stay byte-identical (prompt ids of earlier runs).
_EXAMPLE_ANNOTATION = '"last Saturday [= 2023-05-20]"'
_EXAMPLE_ANSWER = '(for example "the week before 9 June 2023" or "2022")'
_NODATE_ANNOTATION = '"last Saturday [= <resolved date>]"'
_NODATE_ANSWER = (
    '(for example "the week before <the line\'s date>", or only the year when a year is asked)'
)


def without_example_dates(prompt: str) -> str:
    for old, new in ((_EXAMPLE_ANNOTATION, _NODATE_ANNOTATION), (_EXAMPLE_ANSWER, _NODATE_ANSWER)):
        assert old in prompt, old
        prompt = prompt.replace(old, new)
    return prompt
