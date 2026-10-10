"""P03: check what the user asserts about the past before agreeing (``--assertion-check``).

Memory-level sycophancy: the user says "remember when I ... ?", "as I told you, ...", "I always
...", and the assistant plays along although nothing in memory says so (or memory says the
opposite). I32 (``no_record.py``) tells the reader "no record" when a narrow set of recall cues
fires and the context lacks the question's words; it fired on 6 of 80 probes. This module
generalises it:

1. **Detect (code).** :func:`detect_claims` splits the user message into sentences and keeps the
   ones that assert the user's own past or habit: a first-person past-tense verb ("I went", "we
   bought", "I've visited"), a recall cue ("remember when", "as I told you", "last time",
   "you said"), or a habit ("I always / never / every ..."). A value or preference stated now
   ("I love / prefer / want / hate ...", also "I always prefer ...") is NOT a claim: it is taken
   at face value. Only the claim sentences are checked, so a request or an opinion in the same
   message is never contradicted.
2. **Classify (code first).** :func:`classify_claim` compares the claim with the retrieved memory
   lines (public-knowledge blocks removed): ``supported`` (a line holds the claim's content words
   and every one of its specifics: numbers and proper names), ``contradicted`` (a line is about
   the same thing but swaps a specific for another one of the same kind, or flips the polarity),
   else ``unknown``. Absence is never contradiction. An optional *decider* (an OpenDecider ``noul``
   question or a model call, :func:`chat_decider`) may return a verdict and the line it rests on;
   code accepts it only when the cited line exists, and falls back to the rules otherwise.
3. **Tell the reader (a precise note).** supported -> proceed on that memory; contradicted ->
   correct the premise, quoting the memory line, then help with the rest; unknown -> say there is
   no record, without claiming it never happened. Every note says that a preference or opinion
   the user states now stands.

No extra reader call (the optional decider costs one small call per detected claim). Generic:
cue grammar and stop words only; no dataset word, no category, no gold (rule I37). Off by default.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ReaderAnswer
from .no_record import SUPPORT_MIN_FRACTION, strip_public_knowledge

__all__ = [
    "ASSERTION_CHECK_MODES",
    "ASSERTION_CHECK_VERSION",
    "AssertionCheckReader",
    "Claim",
    "Verdict",
    "asserts_claim",
    "chat_decider",
    "classify_claim",
    "detect_claims",
    "note_for",
]

ASSERTION_CHECK_VERSION = "v1"
#: ``rules`` (code only) or ``llm`` (a decider call first, the rules as fallback).
ASSERTION_CHECK_MODES = ("rules", "llm")

# ---- detect ----------------------------------------------------------------------------------

_SUBJ = r"(?:i|we|my (?:\w+ )?and i|me and my \w+)"
_IRREGULAR_PAST = (
    "went had got bought took met saw made did was were gave told said ate drank ran wrote read "
    "found lost won left came began started felt knew thought kept sold paid sent spent built "
    "broke drove flew wore taught caught chose held heard fell grew led"
)
_PAST = rf"(?:[a-z]{{3,}}ed|{'|'.join(_IRREGULAR_PAST.split())})"
_PAST_CLAIM = re.compile(
    rf"\b{_SUBJ}(?:'ve| have| had)? (?:(?:just|also|really|finally|already|recently|actually) )?"
    rf"{_PAST}\b",
    re.I,
)
_PERFECT = re.compile(
    rf"\b{_SUBJ}(?:'ve| have) (?:never |always |already |just )?[a-z]{{3,}}(?:ed|en|ne|t)\b", re.I
)
_RECALL = re.compile(
    r"\bdo you (?:still )?(?:remember|recall)\b"
    r"|\bremember (?:when|that|how|the time|what)\b"
    r"|\b(?:as )?i (?:already )?(?:told|mentioned|said|asked|showed|sent|shared|explained) (?:you|to you)\b"
    r"|\blike i (?:said|told you|mentioned)\b"
    r"|\bthe (?:time|day|night) (?:i|we)\b"
    r"|\blast (?:time|week|month|year) (?:i|we)\b"
    r"|\bwe (?:talked|spoke|discussed|agreed|decided) (?:about|that|on)\b"
    r"|\byou (?:said|told me|promised|recommended|suggested|mentioned|agreed)\b"
    r"|\bearlier (?:i|we) (?:said|talked|discussed)\b",
    re.I,
)
_HABIT = re.compile(
    rf"\b{_SUBJ} (?:always|never|usually|often|constantly|every (?:day|week|morning|night|time))\b"
    rf"|\bevery (?:day|week|morning|night|time) {_SUBJ}\b"
    rf"|\b{_SUBJ} (?:used to|have been|'ve been|had been)\b",
    re.I,
)
#: a habit or "told you" clause whose content is a value stated now, not a past fact.
_VALUE_VERB = re.compile(
    r"\b(?:prefer\w*|like[sd]?|love[sd]?|hate[sd]?|enjoy\w*|want\w*|need\w*|wish\w*|"
    r"believ\w*|wonder\w*|(?:always )?think|feel|"
    r"(?:am|are|'m) (?:a fan|into|vegan|vegetarian)|care about|can't stand|dislike\w*)\b",
    re.I,
)
_QUESTION_ONLY = re.compile(
    r"^\s*(?:can|could|would|will|should|please|help|give|tell|write)\b", re.I
)
#: "I read that ...", "I heard about ...", "I was wondering ...": hearsay or musing, not a personal
#: past event the memory could confirm.
_NOT_PERSONAL = re.compile(
    rf"\b{_SUBJ}(?:'ve| have| just| also| recently)* (?:read|heard|learned|learnt|saw|noticed|"
    r"watched|came across|found out|was (?:reading|hearing|watching|told|wondering|thinking)|"
    r"wondered|thought)\b[^.?!]{0,40}?\b(?:that|they|it|there|this|those|these|about|how|what|"
    r"why|somewhere|if|whether|of)\b"
    rf"|\b{_SUBJ} was (?:wondering|thinking)\b",
    re.I,
)
_CLAUSE = re.compile(r"\s*[,;:—]\s+|\s+-\s+|\?\s*")
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    #: ``past`` (first-person past event), ``recall`` (remember when / as I told you / you said),
    #: ``habit`` (I always / never ...)
    kind: str


def _is_value(sentence: str, kind: str) -> bool:
    """A preference or opinion stated now: taken at face value, never checked. A recall cue
    ("remember when ...") is a past claim even next to a value verb."""
    if kind == "recall":
        return False
    return bool(_VALUE_VERB.search(sentence)) and not _PAST_CLAIM.search(sentence)


def detect_claims(message: str) -> list[Claim]:
    """The sentences of ``message`` that assert the user's own past or habit."""
    claims: list[Claim] = []
    for sent in _SENT.split(message.strip()):
        sent = sent.strip()
        if len(sent) < 8:
            continue
        kind = ""
        if _NOT_PERSONAL.search(sent) and not _RECALL.search(sent):
            rest = _NOT_PERSONAL.sub(" ", sent)
            if not (_HABIT.search(rest) or _PAST_CLAIM.search(rest) or _PERFECT.search(rest)):
                continue
        if _RECALL.search(sent):
            kind = "recall"
        elif _HABIT.search(sent):
            kind = "habit"
        elif _PAST_CLAIM.search(sent) or _PERFECT.search(sent):
            kind = "past"
        if not kind or _is_value(sent, kind):
            continue
        # only the clause(s) that carry the cue: "I ran 15 km on Sunday, good pace?" checks the run
        clauses = [c.strip() for c in _CLAUSE.split(sent) if c.strip()]
        cued = [
            c
            for c in clauses
            if _RECALL.search(c)
            or (
                not _NOT_PERSONAL.search(c)
                and (_HABIT.search(c) or _PAST_CLAIM.search(c) or _PERFECT.search(c))
            )
        ]
        if not cued:
            continue
        claims.append(Claim(" ".join(cued), kind))
    return claims


def asserts_claim(message: str) -> bool:
    """Drop-in for ``no_record.asserts_past_event`` (the I32 detector hook), broader."""
    return bool(detect_claims(message))


# ---- classify --------------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]{4,}")
_STOP = frozenset(
    [
        "that",
        "this",
        "with",
        "from",
        "have",
        "has",
        "had",
        "were",
        "was",
        "been",
        "being",
        "they",
        "them",
        "their",
        "there",
        "then",
        "than",
        "what",
        "when",
        "where",
        "which",
        "while",
        "would",
        "could",
        "should",
        "about",
        "into",
        "over",
        "also",
        "does",
        "did",
        "remember",
        "recall",
        "told",
        "mentioned",
        "said",
        "asked",
        "showed",
        "sent",
        "shared",
        "know",
        "think",
        "still",
        "talked",
        "spoke",
        "discussed",
        "promised",
        "recommended",
        "suggested",
        "please",
        "tell",
        "last",
        "time",
        "week",
        "month",
        "year",
        "always",
        "never",
        "usually",
        "often",
        "every",
        "used",
        "been",
        "just",
        "really",
        "finally",
        "already",
        "recently",
        "actually",
        "agreed",
        "explained",
        "decided",
        "like",
    ]
)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’]*|\d+(?:[.,:]\d+)?")
_SPEAKER = re.compile(r"^[A-Z][\w .'-]{0,30}:\s+")
_NUM = re.compile(r"\b\d+(?:[.,:]\d+)?\b")
_NEG = re.compile(
    r"\b(?:not|never|no longer|didn't|did not|wasn't|weren't|don't|do not|isn't|hasn't|haven't|hadn't|can't|cannot|won't|neither|nor)\b",
    re.I,
)
_LINE_HEAD = re.compile(r"^\s*(?:[*-]\s+|\[hit \d+\]\s*)?(?:\[[^\]]*\]\s*)?")


def _stem(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 4 else w


def _content(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP}


def _specifics(text: str) -> tuple[set[str], set[str]]:
    """(numbers, proper-name words) of ``text``; sentence-initial words do not count as names."""
    names: set[str] = set()
    text = _SPEAKER.sub("", text.strip())
    start = True  # the next word opens a sentence
    for tok in _TOKEN.findall(text):
        if tok[0].isupper() and not start and tok != "I" and len(tok) > 1:
            names.add(tok.lower())
        start = False
    # a sentence end re-opens "start"; handled coarsely: sentence-initial capitals after . ! ?
    for m in re.finditer(r"[.!?]\s+([A-Z][A-Za-z'’]*)", text):
        if m.group(1).lower() in names and len(re.findall(rf"\b{m.group(1)}\b", text)) == 1:
            names.discard(m.group(1).lower())
    return set(_NUM.findall(text)), names


def memory_lines(context: str) -> list[str]:
    out = []
    for raw in strip_public_knowledge(context).splitlines():
        body = _LINE_HEAD.sub("", raw).strip()
        if body and not body.startswith("[Note:"):
            out.append(body)
    return out


@dataclass(frozen=True, slots=True)
class Verdict:
    label: str  # supported | contradicted | unknown
    line: str = ""
    method: str = "rules"
    reason: str = ""

    def as_meta(self) -> dict[str, str]:
        return dataclasses.asdict(self)


def classify_claim(claim: str, lines: Sequence[str]) -> Verdict:
    """Rules: the memory line that shares the most of the claim's content words (at least
    :data:`SUPPORT_MIN_FRACTION` of them) is the evidence. Supported when it also holds every
    specific of the claim and agrees in polarity; contradicted when it swaps a specific for
    another of the same kind, or flips polarity; unknown otherwise (also when nothing matches:
    absence is not contradiction)."""
    words = _content(claim)
    if not words or not lines:
        return Verdict("unknown", reason="no content or no memory")
    best, best_share = "", 0.0
    for ln in lines:
        share = len(words & _content(ln)) / len(words)
        if share > best_share:
            best, best_share = ln, share
    if best_share < SUPPORT_MIN_FRACTION:
        return Verdict("unknown", reason=f"no memory line matches (best share {best_share:.2f})")
    c_nums, c_names = _specifics(claim)
    l_nums, l_names = _specifics(best)
    l_words = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", best)}
    c_words = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", claim)}
    missing_nums = {n for n in c_nums if n not in l_nums and n not in l_words}
    missing_names = {n for n in c_names if n not in l_names and n not in l_words}
    # the claim's non-specific words: how much of "what was done" the line really shares
    generic = {w for w in words if w not in c_nums and w not in c_names}
    anchor = len(generic & _content(best)) / len(generic) if generic else 0.0
    anchored = len(generic) >= 2 and anchor >= 0.75
    # a swap needs an anchored line (it is about the same thing) and an alternative of the same
    # kind in it that the claim does not mention
    swapped_num = bool(missing_nums and (l_nums - c_nums))
    swapped_name = bool(missing_names and (l_names - c_words))
    if anchored and (swapped_num or swapped_name):
        what = sorted(missing_nums | missing_names)
        return Verdict("contradicted", best, reason=f"memory has a different detail than {what}")
    if anchored and bool(_NEG.search(claim)) != bool(_NEG.search(best)):
        return Verdict("contradicted", best, reason="polarity differs")
    if missing_nums or missing_names:
        return Verdict("unknown", best, reason="a specific of the claim is not in the memory")
    return Verdict("supported", best, reason=f"share {best_share:.2f}")


# ---- optional decider ------------------------------------------------------------------------

Decider = Callable[[str, Sequence[str]], Awaitable[Verdict | None]]

_DECIDER_SYSTEM = (
    "You compare one statement a user made about their own past with numbered memory lines. "
    "Reply with one JSON object only."
)
_DECIDER_BODY = (
    "Statement: {claim}\n\nMemory lines:\n{lines}\n\n"
    'Does a memory line support the statement ("supported"), say something that conflicts with it '
    '("contradicted"), or neither ("unknown")? A line that is merely silent on the statement is '
    '"unknown", never "contradicted". Cite the line id for supported / contradicted. Reply '
    '{{"verdict": "supported|contradicted|unknown", "line": "L3"}}'
)
_MAX_DECIDER_LINES = 30


def chat_decider(chat: Callable[..., Awaitable[str]]) -> Decider:
    """A decider backed by one small model call (``chat(user, system=...)``). Its verdict is used
    only when it is one of the three labels and the cited line exists; otherwise None (the rules
    decide). The model's line text is taken from the context, never from its reply."""
    import json

    async def decide(claim: str, lines: Sequence[str]) -> Verdict | None:
        shown = list(lines)[:_MAX_DECIDER_LINES]
        numbered = "\n".join(f"L{i} {ln[:300]}" for i, ln in enumerate(shown, start=1))
        reply = await chat(
            _DECIDER_BODY.format(claim=claim, lines=numbered), system=_DECIDER_SYSTEM
        )
        m = re.search(r"\{.*\}", reply, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            return None
        label = str(obj.get("verdict", "")).strip().lower()
        if label not in ("supported", "contradicted", "unknown"):
            return None
        if label == "unknown":
            return Verdict("unknown", method="llm")
        num = re.search(r"\d+", str(obj.get("line", "")))
        if not num or not 1 <= int(num.group(0)) <= len(shown):
            return None
        return Verdict(label, shown[int(num.group(0)) - 1], method="llm")

    return decide


# ---- the reader note -------------------------------------------------------------------------

_FACE_VALUE = " A preference or opinion the user states now stands as given."


def note_for(verdict: Verdict) -> str:
    quoted = verdict.line[:300]
    if verdict.label == "supported":
        return (
            f'[Note: a stored memory supports what the user says about the past: "{quoted}". '
            "Proceed on that basis." + _FACE_VALUE + "]"
        )
    if verdict.label == "contradicted":
        return (
            f'[Note: what the user says about the past differs from a stored memory: "{quoted}". '
            "Do not confirm their version. Politely correct that detail using the memory, then "
            "help with the rest of the request." + _FACE_VALUE + "]"
        )
    return (
        "[Note: no stored memory confirms the past event or habit the user describes. Say there "
        "is no record of it; do not say it did not happen, and do not confirm it. Still help "
        "with the rest of the request." + _FACE_VALUE + "]"
    )


# ---- the reader wrapper ----------------------------------------------------------------------


class AssertionCheckReader:
    """Wraps a reader: a message that asserts the user's past gets a checked note in front of
    the context. Same number of reader calls as the inner reader (``llm`` mode: plus one small
    decider call per detected claim)."""

    def __init__(
        self,
        inner: Any,
        mode: str = "rules",
        *,
        decider: Decider | None = None,
        detector: Callable[[str], list[Claim]] = detect_claims,
        classifier: Callable[[str, Sequence[str]], Verdict] = classify_claim,
    ) -> None:
        if mode not in ASSERTION_CHECK_MODES:
            raise ValueError(f"assertion-check mode must be one of {ASSERTION_CHECK_MODES}")
        if mode == "llm" and decider is None:
            raise ValueError("assertion-check llm mode needs a decider")
        self.inner = inner
        self.mode = mode
        self.decider = decider
        self.detector = detector
        self.classifier = classifier
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+assert-{mode}"
        self.model = inner.model
        self.makes_model_calls = getattr(inner, "makes_model_calls", True) or mode == "llm"
        #: counters for the run log
        self.seen = 0
        self.detected = 0
        self.verdicts = {"supported": 0, "contradicted": 0, "unknown": 0}

    def describe(self) -> Mapping[str, Any]:
        return {
            **self.inner.describe(),
            "assertion_check": f"{ASSERTION_CHECK_VERSION}/{self.mode}",
        }

    async def _verdict(self, claim: Claim, lines: Sequence[str]) -> Verdict:
        if self.mode == "llm" and self.decider is not None and lines:
            try:
                got = await self.decider(claim.text, lines)
            except Exception:  # an enhancer: the rules decide
                got = None
            if got is not None:
                return got
        return self.classifier(claim.text, lines)

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        self.seen += 1
        claims = self.detector(question)
        if not claims:
            return await self.inner.answer(question, context, question_date)
        self.detected += 1
        lines = memory_lines(context)
        verdicts = [(c, await self._verdict(c, lines)) for c in claims]
        # the worst verdict decides the note: contradicted > unknown > supported
        order = {"contradicted": 0, "unknown": 1, "supported": 2}
        claim, verdict = min(verdicts, key=lambda cv: order[cv[1].label])
        self.verdicts[verdict.label] += 1
        note = note_for(verdict)
        context = f"{note}\n{context}" if context.strip() else note
        result: ReaderAnswer = await self.inner.answer(question, context, question_date)
        meta = {
            **(result.extra_meta or {}),
            "assertion_check": {
                "version": ASSERTION_CHECK_VERSION,
                "mode": self.mode,
                "claims": [{"text": c.text, "kind": c.kind, **v.as_meta()} for c, v in verdicts],
                "decided_on": claim.text,
                "verdict": verdict.label,
            },
        }
        return dataclasses.replace(result, extra_meta=meta)
