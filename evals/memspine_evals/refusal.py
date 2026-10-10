"""Refusal detection and the opt-in refusal-retry reader (``--retry-refusal``).

Gap analysis of a 9B reader on LoCoMo (``analysis/READER_GAPS.md``): about half of the wrong
answers with the gold evidence in context were refusals ("not mentioned"). With
``--retry-refusal`` a refusal is re-asked once. I1: the default wording is neutral and asserts
no evidence (an unanswerable question may still be refused); ``mode="assertive"`` is the old
firmer wording, kept to reproduce earlier runs. Off (the default) no wrapper is built and
readers are unchanged.

The patterns copy ``failure_buckets.REFUSAL`` / ``DENIAL`` (that module lives outside the
package); a test pins the two to the same source text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .contracts import ReaderAnswer
from .tokens import HeuristicTokenCounter

__all__ = [
    "DENIAL",
    "MAX_RETRIES",
    "REFUSAL",
    "REFUSAL_MATCH_MODES",
    "RETRY_INSTRUCTION",
    "RETRY_INSTRUCTION_ASSERTIVE",
    "RETRY_INSTRUCTION_NEUTRAL",
    "RETRY_MODES",
    "RefusalRetryReader",
    "is_refusal",
    "is_refusal_legacy",
    "is_refusal_whole",
    "named_entities",
    "names_absent_entity",
    "shares_content_word",
]

REFUSAL = re.compile(
    r"\b(?:i|you) do(?: not|n't) know\b"
    r"|\bnot (?:mentioned|specified|stated|provided|clear|known)\b"
    r"|\bno (?:mention|specific information|information|record)\b"
    r"|\b(?:does|do|did) not (?:mention|specify|contain|say|state|indicate|include|provide)\b"
    r"|\bcannot (?:be )?determine"
    r"|\bthere is no\b",
    re.IGNORECASE,
)
#: "Melanie did not ..." as the answer's opening clause: a denial of the premise.
DENIAL = re.compile(r"^[A-Z][\w'.]*(?: and [A-Z][\w']*)? (?:did|does|has|had|was) not\b")

#: I1: the DEFAULT retry wording is neutral. It claims nothing about the memories, so on an
#: unanswerable question (LoCoMo cat 5, abstention probes) the reader may still refuse, and the
#: exact refusal string is offered as the way to do it.
RETRY_INSTRUCTION_NEUTRAL = (
    "\n\nRe-read the memories carefully. If they contain information that answers the "
    "question, answer it; if they truly do not, reply exactly: Not mentioned in the "
    "conversation."
)
#: The original wording (``mode="assertive"``), kept only to reproduce earlier runs (the
#: BEST_dev runs used it). It asserts the evidence exists, which is false on unanswerable
#: questions: it can turn a correct refusal into a fabrication.
RETRY_INSTRUCTION_ASSERTIVE = (
    "\n\nNote: the memories above do contain information relevant to this question, so do not "
    "reply that it is unknown or not mentioned. Answer from the best-supported evidence in "
    "them, even if it is indirect: resolve relative times against the date of the line they "
    "appear in, and give your most likely answer in a few words."
)
#: Back-compat name: the default (neutral) wording.
RETRY_INSTRUCTION = RETRY_INSTRUCTION_NEUTRAL

RETRY_MODES: dict[str, str] = {
    "neutral": RETRY_INSTRUCTION_NEUTRAL,
    "assertive": RETRY_INSTRUCTION_ASSERTIVE,
}

_WORD = re.compile(r"[a-z0-9]{4,}")
_STOP_TEXT = (
    "that this with from have has had were was been being they them their there then than what "
    "when where which while would could should about into over also does did not mentioned "
    "conversation"
)
_STOP = frozenset(_STOP_TEXT.split())


def shares_content_word(answer: str, context: str) -> bool:
    """True when ``answer`` has a content word (4+ letters/digits, not a stopword) that also
    occurs in ``context``. A cheap check that a retry answer is tied to the retrieved text."""
    words = {w for w in _WORD.findall(answer.lower()) if w not in _STOP}
    return bool(words & set(_WORD.findall(context.lower())))


#: C10: the retry limit. A refusal is re-asked AT MOST this many times (never a loop), on a
#: non-empty context only. Measured on the 1,540-question run (``READER_GAPS_FORENSIC.md`` 3):
#: 107 fires, 31 end correct, and a second refusal is never rescued by a third ask.
MAX_RETRIES = 1

_CAP_WORD = re.compile(r"[A-Z][a-z][\w'-]*")
#: capitalised words that are not names: question openers, weekdays, months, pronoun-likes
_NOT_NAMES = frozenset(
    (
        "what who whom whose when where which why how did does do is are was were would could "
        "should can will has have had the a an in on at of to for with from about after before "
        "during and or but if it its he she they we you i monday tuesday wednesday thursday "
        "friday saturday sunday january february march april may june july august september "
        "october november december"
    ).split()  # noqa: SIM905
)


def named_entities(question: str) -> list[str]:
    """Capitalised words in ``question`` that look like names: not a question opener,
    weekday or month (an unlisted sentence opener counts as a name, which only makes the
    guard skip a retry). Rules only; ``"Caroline's"`` gives ``"Caroline"``."""
    out: list[str] = []
    for w in _CAP_WORD.findall(question):
        base = re.sub(r"'s$", "", w)
        if base.lower() in _NOT_NAMES:
            continue
        if base not in out:
            out.append(base)
    return out


def names_absent_entity(question: str, context: str) -> bool:
    """C10: True when the question names at least one entity and NONE of them appears in
    ``context``: a question about someone the memories do not mention at all. Such a question is
    unanswerable from the store, so a retry that insists on an answer can only fabricate.
    Requiring that all names be absent keeps world-knowledge questions ("Would Melanie enjoy
    Vivaldi?", Vivaldi absent, Melanie present) retryable. Generic: question and context text
    only, never the gold or the category. Limit: a swapped-speaker question (both people are in
    the store, the pairing is wrong) is not detected."""
    names = named_entities(question)
    low = context.lower()
    return bool(names) and all(name.lower() not in low for name in names)


#: I22: how ``is_refusal`` reads an answer. ``whole`` (default): only when the WHOLE answer is
#: a refusal / abstention statement. ``legacy``: the original substring regexes, which also
#: matched negative facts ("There is no school on Friday", "I did not go") and any answer that
#: merely contained "there is no"; kept to reproduce earlier runs (``--refusal-match legacy``).
REFUSAL_MATCH_MODES = ("whole", "legacy")

_TAIL = r"(?:\W.{0,100})?"
_NO_THING = (
    r"(?:mention|specific information|information|info|record|indication|evidence|details?|"
    r"data|reference|answer)"
)
_WHOLE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # "I do not know", "I don't have that information", "we have no information"
        r"(?:i|we) (?:do not|don't|dont|did not|didn't) "
        r"(?:know|have (?:any |enough |that |this |the )?"
        r"(?:information|info|details?|records?|data))" + _TAIL,
        r"(?:i|we) (?:have|has) no (?:information|info|record|memory|idea|way)" + _TAIL,
        r"(?:i|we) (?:do not|don't|dont) (?:recall|remember)" + _TAIL,
        r"(?:i am|i'm) (?:not sure|unsure|unable to \w+|not able to \w+)" + _TAIL,
        # "I cannot determine ...", "cannot be determined"
        r"(?:i )?(?:cannot|can't|can not|couldn't|could not) (?:be )?"
        r"(?:determine[d]?|tell|say|find|answer|identify|confirm|know)" + _TAIL,
        r"(?:the |this |that )?[\w'-]+(?: [\w'-]+){0,2} (?:cannot|can't|can not|could not) be "
        r"(?:determined|found|known|identified|told|answered)" + _TAIL,
        # "Not mentioned in the conversation", "It is not specified"
        r"(?:(?:it|that|this) (?:is|was) |it's )?(?:not|never) "
        r"(?:mentioned|specified|stated|provided|clear|known|available|discussed|said|"
        r"indicated|given|recorded)" + _TAIL,
        # "There is no information about ...": the absence of INFORMATION, not of a thing
        r"there (?:is|was|are|were|'s) (?:no|not any) " + _NO_THING + _TAIL,
        r"no " + _NO_THING + r"(?: (?:is|was|are|were))?" + _TAIL,
        # "The conversation does not mention ...", "Melanie did not specify ..."
        r"(?!(?:i|we|you)\b)(?:the |this |that |these |those )?[\w'-]+(?: [\w'-]+){0,2} "
        r"(?:does|do|did|has|have) not "
        r"(?:mention|specify|state|say|indicate|provide|include|contain|reveal|discuss|share|"
        r"tell|give|record|show)" + _TAIL,
        r"(?:the )?(?:answer|information|details?) (?:is|are) (?:not|unavailable|unknown)" + _TAIL,
        r"(?:unknown|unclear|unanswerable|n/a|not applicable|no answer|no data)",
    )
)
_LEAD = re.compile(
    r"^(?:(?:sorry|unfortunately|apologies|hmm|well|actually|answer|still|again|so)\b[,:.!]?\s*)+"
    r"|^(?:based on|according to|from|in|per|looking at) the (?:given |provided |available |"
    r"retrieved |above )?(?:conversation|context|memories|memory|text|passage|notes|chat|"
    r"dialogue|transcript|information|records?)[^,:]{0,40}[,:]\s*",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(?:but|however|although|though|likely|probably|maybe|perhaps|possibly|presumably)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WRAP = "*_`\"'“” "


def is_refusal_legacy(answer: str) -> bool:
    """The original ``is_refusal``: a substring match, so any answer containing "there is no"
    or opening "<Name> did not ..." counted. Kept behind ``--refusal-match legacy``."""
    text = answer.strip()
    if not text:
        return True
    return bool(REFUSAL.search(text) or DENIAL.search(text))


def is_refusal_whole(answer: str) -> bool:
    """I22: True when ``answer`` is empty or the WHOLE answer is a refusal / abstention
    statement ("I do not know", "Not mentioned in the conversation", "There is no information
    about it"). A negative fact ("There is no school on Friday", "I did not go") is an
    answer, not a refusal, and so is a refusal that carries a guess ("I do not know, but
    probably May"). Every sentence must be a refusal statement."""
    text = answer.strip().strip(_WRAP)
    if not text:
        return True
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    for sentence in sentences:
        core = sentence.strip().strip(_WRAP).rstrip(".!?;: ").strip()
        core = _LEAD.sub("", core, count=1).strip()
        core = _LEAD.sub("", core, count=1).strip()
        if not core:
            continue  # a bare "Sorry." adds nothing to the verdict
        if _HEDGE.search(core) or not any(p.fullmatch(core) for p in _WHOLE_PATTERNS):
            return False
    return True


def is_refusal(answer: str, mode: str = "whole") -> bool:
    """True when ``answer`` is empty or declines. ``mode="whole"`` (default, I22): the whole
    answer must be a refusal statement (:func:`is_refusal_whole`). ``mode="legacy"``: the
    original substring regexes (:func:`is_refusal_legacy`)."""
    if mode == "legacy":
        return is_refusal_legacy(answer)
    if mode != "whole":
        raise ValueError(f"refusal match mode must be one of {REFUSAL_MATCH_MODES}, got {mode!r}")
    return is_refusal_whole(answer)


class RefusalRetryReader:
    """Wraps a reader: a refusal on a non-empty context is re-asked once (``MAX_RETRIES``).

    Limits (C10): one retry, never a loop; none on an empty context; with
    ``guard_absent_entity`` none when the question names an entity the context never
    mentions (the cat-5-shaped, unanswerable case). A retry whose answer is again a refusal
    is discarded and the first answer kept.

    The decision to retry uses only the first answer's text and the context: never the gold,
    the category or the ``abstention`` flag of the query (the reader never sees them).

    The retry costs one more reader call (counted in ``model_calls``, so it counts against
    ``--max-model-calls``). The retry's answer replaces the first only when it is not
    itself a refusal; both answers are kept in ``ReaderAnswer.extra_meta`` (row ``meta``).
    """

    def __init__(
        self,
        inner: Any,
        instruction: str | None = None,
        *,
        mode: str = "neutral",
        require_context_overlap: bool = False,
        decider: Any = None,
        decider_min_confidence: float = 0.5,
        guard_absent_entity: bool = False,
        refusal_match: str = "whole",
    ) -> None:
        if refusal_match not in REFUSAL_MATCH_MODES:
            raise ValueError(f"refusal_match must be one of {REFUSAL_MATCH_MODES}")
        #: I22: ``whole`` (default) or ``legacy`` (the original substring regexes).
        self.refusal_match = refusal_match
        if mode not in RETRY_MODES:
            raise ValueError(f"retry mode must be one of {sorted(RETRY_MODES)}, got {mode!r}")
        self.inner = inner
        #: C10 (default off, reader ids unchanged): do not retry when the question names an
        #: entity the context never mentions (the question cannot be answered from the store).
        self.guard_absent_entity = guard_absent_entity
        self.guard = getattr(inner, "guard", None)
        self.mode = mode
        #: optional safety valve (default off): accept the retry answer only when it shares a
        #: content word with the retrieved context
        self.require_context_overlap = require_context_overlap
        self.instruction = RETRY_MODES[mode] if instruction is None else instruction
        #: I28: optional ``memspine.services.decision.decider.Decider``. When set and sure
        #: enough, it replaces the ``is_refusal`` regex on question + answer text only (never
        #: the gold, the category or the abstention flag); otherwise the regex decides.
        self.decider = decider
        self.decider_min_confidence = decider_min_confidence
        # the assertive id is the historical one, so earlier runs keep their reader identity
        self.reader_id = f"{inner.reader_id}+retry" + ("" if mode == "assertive" else "-neutral")
        if decider is not None:
            self.reader_id += f"-{getattr(decider, 'decider_id', 'decider')}"
        if guard_absent_entity:
            self.reader_id += "-entityguard"
        self.model = inner.model
        self.makes_model_calls = True
        self._counter = HeuristicTokenCounter()
        #: Refusals seen, retries that produced a non-refusal answer.
        self.retried = 0
        self.recovered = 0
        #: Refusals left alone by ``guard_absent_entity``.
        self.skipped = 0

    def describe(self) -> Mapping[str, Any]:
        return {
            **self.inner.describe(),
            "retry_refusal": True,
            "retry_mode": self.mode,
            "retry_require_context_overlap": self.require_context_overlap,
            **({"retry_guard_absent_entity": True} if self.guard_absent_entity else {}),
            **({"refusal_match": self.refusal_match} if self.refusal_match != "whole" else {}),
            **({"decider": self.decider.decider_id} if self.decider is not None else {}),
        }

    async def _refusal(self, question: str, text: str, log: list[dict[str, Any]]) -> bool:
        """Whether ``text`` is a refusal: the decider when set and sure, else the regex.
        Every decider call is appended to ``log`` (task, label, confidence, adapter)."""
        rule = is_refusal(text, self.refusal_match)
        if self.decider is None or not text.strip():
            return rule
        try:
            decision = await self.decider.decide("refusal", question, text)
        except Exception as exc:  # an enhancer, never a gate
            log.append({"task": "refusal", "adapter": self.decider.decider_id, "error": str(exc)})
            return rule
        sure = (
            decision.confidence is not None and decision.confidence >= self.decider_min_confidence
        )
        log.append({**decision.as_meta(used=sure), "heuristic": rule})
        return (decision.label == "refusal") if sure else rule

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        decisions: list[dict[str, Any]] = []
        if not context.strip() or not await self._refusal(question, first.text, decisions):
            if decisions:
                first = replace(first, extra_meta={**first.extra_meta, "decisions": decisions})
            return first
        if self.guard_absent_entity and names_absent_entity(question, context):
            self.skipped += 1
            return replace(
                first,
                extra_meta={
                    **first.extra_meta,
                    "retry_skipped": "question names an entity absent from the context",
                    **({"decisions": decisions} if decisions else {}),
                },
            )
        self.retried += 1
        second: ReaderAnswer = await self.inner.answer(
            question + self.instruction, context, question_date
        )
        accepted = not await self._refusal(question, second.text, decisions)
        if accepted and self.require_context_overlap:
            accepted = shares_content_word(second.text, context)
        if accepted:
            self.recovered += 1
        winner = second if accepted else first
        return ReaderAnswer(
            text=winner.text,
            prompt_tokens=first.prompt_tokens + second.prompt_tokens,
            completion_tokens=first.completion_tokens + second.completion_tokens,
            latency_ms=first.latency_ms + second.latency_ms,
            model_calls=max(first.model_calls, 0) + max(second.model_calls, 1),
            truncated=winner.truncated,
            cached_prompt_tokens=first.cached_prompt_tokens + second.cached_prompt_tokens,
            finish_reason=winner.finish_reason,
            raw_text=winner.raw_text,
            prompt_variant=first.prompt_variant,
            extra_meta={
                "retry_refusal": True,
                "first_answer": first.text,
                "retry_answer": second.text,
                "retry_accepted": accepted,
                "retry_mode": self.mode,
                "retry_require_context_overlap": self.require_context_overlap,
                **({"decisions": decisions} if decisions else {}),
                # D3 [HAR-2]: row prompt_tokens sums both calls; keep the split
                "first_prompt_tokens": first.prompt_tokens,
                "first_completion_tokens": first.completion_tokens,
                "retry_prompt_tokens": second.prompt_tokens,
                "retry_completion_tokens": second.completion_tokens,
            },
        )
