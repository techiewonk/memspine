# ruff: noqa: E501, SIM905
"""W2-13 (P-23, ``write.prepass``): a small local pre-pass over a window's raw turns, as hints.

It runs BEFORE the extraction call and produces :class:`Hints`: candidate entities with spans, dates,
numbers, known aliases seen, and a ``nothing_extractable`` signal. Three uses:

1. **Prompt context** (KNOWN / NEW). ``Hints.prompt_lines()`` renders them; W2-06b's
   ``derive.PromptContextBuilder`` takes the lines with one call (see ``docs/v2/areas/models.md``
   P-23): ``context.hints = prepass_hints(records, ...).prompt_lines()``.
2. **Skip.** ``WindowPipeline`` (``workers/windows.py``) skips the extraction call of a window whose
   hints say ``nothing_extractable`` (greetings, filler) and counts it (``windows_prepass_skipped``).
3. **Cross-check.** :func:`prepass_stage` is an envelope stage at order 500. It only flags: an
   envelope whose proper-name entity the pre-pass never saw gets ``extras["prepass_unseen"]`` and a
   note; it never adds, drops or rewrites a fact.

Modes: ``rules`` (regex baseline: capitalised names, dates, numbers, aliases the engine already
learned) and ``gliner`` (the rules plus GLiNER2 entities, local CPU; falls back to ``rules`` when the
``[ner]`` extra is absent). GLiNER2 only ever adds hints (it can veto a skip, never cause one).

The skip rule is deliberately conservative: a turn is filler only when every word is in a small
closed vocabulary of greetings / thanks / acknowledgements. Anything else (a first-person
statement, a number, a date cue, a name, a non-Latin word) keeps the window.

Envelope fields read: ``subject.surface``, ``object.surface``. Written: ``extras["prepass_unseen"]``
(list of surfaces) on a flagged envelope. Nothing else.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from memspine.derive.envelope_stages import EnvelopeStage, EnvelopeStageContext
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.schemas.durable.assertion import AssertionEnvelope

__all__ = [
    "FILLER_VOCAB",
    "GlinerHints",
    "Hints",
    "Prepass",
    "PrepassMode",
    "Span",
    "prepass_hints",
    "prepass_stage",
    "rule_hints",
]

_log = get_logger(__name__)

PrepassMode = Literal["off", "rules", "gliner"]

#: closed vocabulary of filler words (lower case). A turn made only of these is filler.
FILLER_VOCAB = frozenset(
    """hi hello hey yo hiya howdy good morning evening afternoon night bye goodbye cya later see you
    soon thanks thank thx ty ok okay k kk sure yes yeah yep yup no nope nah cool great nice awesome
    wow lol haha hehe hmm hm oh ah uh um right alright fine sounds got it welcome np problem please
    sorry pardon how are doing is what up and too well same lovely perfect noted understood
    indeed absolutely definitely true agreed exactly cheers take care have a day the that this
    again also so then yay oops ha there much very""".split()
)
_FILLER_MAX_WORDS = 8

_SPEAKER = re.compile(r"^\s*[A-Z][\w'-]{1,30}(?: [A-Z][\w'-]{1,30})?:\s+")
_WORD = re.compile(r"\w+(?:'\w+)?", re.UNICODE)
_CAP_RUN = re.compile(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*")
_MONTHS = (
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec"
).split()
_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"
    r"|\b(?:" + "|".join(_MONTHS) + r")\b\.?(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+\d{4})?"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:" + "|".join(_MONTHS) + r")\b"
    r"|\b(?:" + "|".join(_WEEKDAYS) + r")\b"
    r"|\b(?:yesterday|today|tomorrow|tonight|ago|last\s+\w+|next\s+\w+|this\s+(?:week|month|year|"
    r"morning|evening|weekend)|recently|earlier|lately|since\s+\w+|until\s+\w+|\d{4})\b",
    re.I,
)
_NUMBER = re.compile(
    r"\b\d[\d,.:]*\b|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|"
    r"thirty|forty|fifty|hundred|thousand|million|dozen|half|twice|double|triple)\b",
    re.I,
)
#: capitalised words that are not names (sentence openers, pronouns, filler)
_NOT_NAMES = (
    frozenset(FILLER_VOCAB)
    | frozenset(_MONTHS)
    | frozenset(_WEEKDAYS)
    | frozenset(
        """i i'm i've i'll i'd my we he she they it its there when where why who but because if
        can could would should do did does will let let's maybe just now my our your his her their
        yesterday today tomorrow last next""".split()
    )
)


@dataclass(frozen=True)
class Span:
    """One hint: ``text`` at ``[start, end)`` in the content of turn ``line`` (1-based)."""

    text: str
    start: int
    end: int
    line: int
    label: str  # name | alias | date | number | a GLiNER2 label
    source: str = "rules"  # rules | gliner


@dataclass(frozen=True)
class Hints:
    """What the pre-pass saw in one window's turns."""

    mode: str
    turns: int
    entities: tuple[Span, ...] = ()
    dates: tuple[Span, ...] = ()
    numbers: tuple[Span, ...] = ()
    #: store-known aliases found in the turns (a subset of ``entities``, label ``alias``)
    known: tuple[str, ...] = ()
    #: speaker names (``Name:`` prefix or the record's speaker axis); known to the cross-check but
    #: never a reason to keep a window
    speakers: tuple[str, ...] = ()
    #: 1-based numbers of the turns that are pure filler
    filler_lines: tuple[int, ...] = ()
    #: True when every turn is filler and nothing was seen: the extraction call can be skipped
    nothing_extractable: bool = False

    def names(self) -> frozenset[str]:
        """Lower-cased entity surfaces and their words (what a cross-check may match against)."""
        out: set[str] = set()
        for sp in self.speakers:
            low = sp.lower()
            out.add(low)
            out.update(low.split())
        for s in self.entities:
            low = s.text.lower()
            out.add(low)
            out.update(low.split())
        return frozenset(out)

    def new_entities(self) -> tuple[str, ...]:
        """Entity surfaces not in ``known`` (the NEW side of KNOWN / NEW), order kept, deduped."""
        known = {k.lower() for k in self.known}
        seen: dict[str, None] = {}
        for s in self.entities:
            if s.text.lower() not in known:
                seen.setdefault(s.text, None)
        return tuple(seen)

    def prompt_lines(self) -> list[str]:
        """Hint lines for the extraction prompt; empty when there is nothing to say."""
        out: list[str] = []
        if self.known:
            out.append("KNOWN: " + "; ".join(dict.fromkeys(self.known)))
        new = self.new_entities()
        if new:
            out.append("NEW: " + "; ".join(new))
        if self.dates:
            out.append("DATES: " + "; ".join(dict.fromkeys(s.text for s in self.dates)))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "turns": self.turns,
            "entities": [s.text for s in self.entities],
            "dates": [s.text for s in self.dates],
            "numbers": [s.text for s in self.numbers],
            "known": list(self.known),
            "filler_lines": list(self.filler_lines),
            "nothing_extractable": self.nothing_extractable,
        }


def _content(record: Any) -> str:
    return record if isinstance(record, str) else str(getattr(record, "content", "") or "")


def _is_filler(text: str) -> bool:
    body = _SPEAKER.sub("", text, count=1)
    words = [w.lower() for w in _WORD.findall(body)]
    if not words:
        return not _NUMBER.search(body)  # punctuation / emoji only
    if len(words) > _FILLER_MAX_WORDS:
        return False
    return all(w in FILLER_VOCAB for w in words)


def _name_spans(body: str, offset: int, line: int) -> list[Span]:
    out: list[Span] = []
    for m in _CAP_RUN.finditer(body):
        words = m.group(0).split()
        # drop leading / trailing non-name words ("Hello Ilse" -> "Ilse"); keep the offsets exact
        start = m.start()
        while words and words[0].lower() in _NOT_NAMES:
            start += len(words[0]) + 1
            words = words[1:]
        while words and words[-1].lower() in _NOT_NAMES:
            words = words[:-1]
        if not words:
            continue
        text = " ".join(words)
        out.append(Span(text, offset + start, offset + start + len(text), line, "name"))
    return out


def rule_hints(
    records: Sequence[Any],
    *,
    known_aliases: Iterable[str] = (),
    mode: str = "rules",
) -> Hints:
    """The regex baseline over the turns of one window (no model, no I/O)."""
    aliases = sorted({a for a in known_aliases if a and a.strip()}, key=len, reverse=True)
    alias_re = (
        re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b", re.I)
        if aliases
        else None
    )
    entities: list[Span] = []
    dates: list[Span] = []
    numbers: list[Span] = []
    known: list[str] = []
    filler: list[int] = []
    speakers: list[str] = []
    for n, rec in enumerate(records, 1):
        text = _content(rec)
        prefix = _SPEAKER.match(text)
        off = prefix.end() if prefix else 0
        if prefix:
            speakers.append(prefix.group(0).strip().rstrip(":").strip())
        axis = getattr(getattr(rec, "axes", None), "speaker", None)
        if isinstance(axis, str) and axis:
            speakers.append(axis)
        body = text[off:]
        if _is_filler(text):
            filler.append(n)
        taken: list[tuple[int, int]] = []
        if alias_re is not None:
            for m in alias_re.finditer(body):
                entities.append(Span(m.group(0), off + m.start(), off + m.end(), n, "alias"))
                known.append(m.group(0))
                taken.append((m.start(), m.end()))
        for span in _name_spans(body, off, n):
            if not any(a <= span.start - off < b for a, b in taken):
                entities.append(span)
        dates.extend(
            Span(m.group(0), off + m.start(), off + m.end(), n, "date")
            for m in _DATE.finditer(body)
        )
        numbers.extend(
            Span(m.group(0), off + m.start(), off + m.end(), n, "number")
            for m in _NUMBER.finditer(body)
        )
    return _finish(mode, len(records), entities, dates, numbers, known, filler, speakers)


def _finish(
    mode: str,
    turns: int,
    entities: list[Span],
    dates: list[Span],
    numbers: list[Span],
    known: list[str],
    filler: list[int],
    speakers: Sequence[str] = (),
) -> Hints:
    nothing = bool(turns) and len(filler) == turns and not (entities or dates or numbers)
    return Hints(
        mode=mode,
        turns=turns,
        entities=tuple(entities),
        dates=tuple(dates),
        numbers=tuple(numbers),
        known=tuple(known),
        speakers=tuple(dict.fromkeys(speakers)),
        filler_lines=tuple(filler),
        nothing_extractable=nothing,
    )


#: labels asked of GLiNER2 (the decision provider's set plus date and quantity)
GLINER_LABELS = ("person", "place", "organization", "event", "date", "quantity")


class GlinerHints:
    """GLiNER2 entity finder (local CPU, ``[ner]``). Loads lazily; ``available()`` is False when
    the package is missing, in which case the caller uses the rules alone."""

    def __init__(self, model: str = "fastino/gliner2-base-v1") -> None:
        self._model_id = model
        self._model: Any = None

    @staticmethod
    def available() -> bool:
        try:
            from memspine.models.providers.decision.gliner2_decision import gliner2_class

            gliner2_class()
        except Exception:
            return False
        return True

    def _load(self) -> Any:
        if self._model is None:
            from memspine.models.providers.decision.gliner2_decision import gliner2_class

            self._model = gliner2_class().from_pretrained(self._model_id)
        return self._model

    def entities(self, text: str) -> list[tuple[str, str]]:
        """``[(surface, label)]`` for one text; an unrecognised result shape yields nothing."""
        raw = self._load().extract_entities(text, list(GLINER_LABELS))
        found = raw.get("entities", raw) if isinstance(raw, dict) else None
        if not isinstance(found, dict):
            return []
        return [(str(n), str(label)) for label, names in found.items() for n in (names or [])]


def _gliner_spans(finder: Any, records: Sequence[Any]) -> list[Span]:
    out: list[Span] = []
    for n, rec in enumerate(records, 1):
        text = _content(rec)
        for surface, label in finder.entities(text):
            at = text.find(surface)
            if at < 0:
                continue  # a span the model invented is not a hint
            out.append(Span(surface, at, at + len(surface), n, label, "gliner"))
    return out


def prepass_hints(
    records: Sequence[Any],
    *,
    mode: PrepassMode = "rules",
    known_aliases: Iterable[str] = (),
    gliner: Any = None,
) -> Hints:
    """Hints for one window's raw turns. ``off`` returns an empty, never-skipping :class:`Hints`.

    ``gliner`` is a finder with ``entities(text) -> [(surface, label)]`` (:class:`GlinerHints` or a
    stub); without one, ``gliner`` mode is the rules."""
    if mode == "off":
        return Hints(mode="off", turns=len(records))
    base = rule_hints(records, known_aliases=known_aliases, mode=mode)
    if mode != "gliner" or gliner is None:
        return base
    extra = _gliner_spans(gliner, records)
    have = {(s.line, s.text.lower()) for s in base.entities}
    added = [s for s in extra if (s.line, s.text.lower()) not in have]
    return _finish(
        mode,
        base.turns,
        [*base.entities, *(s for s in added if s.label not in ("date", "quantity"))],
        [*base.dates, *(s for s in added if s.label == "date")],
        [*base.numbers, *(s for s in added if s.label == "quantity")],
        list(base.known),
        list(base.filler_lines),
        base.speakers,
    )


class Prepass:
    """The configured pre-pass of one engine: mode, learned aliases, a small hints cache."""

    def __init__(
        self,
        mode: PrepassMode = "rules",
        *,
        gliner: Any = None,
        aliases: Callable[[], Iterable[str]] | None = None,
        cache_size: int = 64,
    ) -> None:
        if mode == "gliner" and gliner is None:
            finder = GlinerHints()
            if finder.available():
                gliner = finder
            else:
                _log.warning(
                    "prepass.gliner_unavailable",
                    detail="write.prepass=gliner but gliner2 is not installed; using the rules",
                )
        self.mode: PrepassMode = mode
        self._gliner = gliner
        self._aliases = aliases
        self._learned: dict[str, None] = {}
        self._cache: OrderedDict[tuple[str, ...], Hints] = OrderedDict()
        self._cache_size = cache_size
        self.skipped = 0

    @property
    def uses_gliner(self) -> bool:
        return self.mode == "gliner" and self._gliner is not None

    def learn(self, names: Iterable[str]) -> None:
        """Remember entity surfaces the pipeline deposited (the 'known aliases' of later windows)."""
        for n in names:
            if n and len(n) > 1:
                self._learned.setdefault(n, None)

    def known_aliases(self) -> list[str]:
        extra = list(self._aliases()) if self._aliases is not None else []
        return [*self._learned, *extra]

    def hints(self, records: Sequence[Any]) -> Hints:
        """Synchronous hints (rules; GLiNER2 too when it is loaded and cheap enough to block)."""
        key = tuple(str(getattr(r, "record_id", id(r))) for r in records)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = prepass_hints(
            records, mode=self.mode, known_aliases=self.known_aliases(), gliner=self._gliner
        )
        self._cache[key] = out
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return out

    async def ahints(self, records: Sequence[Any]) -> Hints:
        """Hints without blocking the event loop (GLiNER2 runs in a worker thread)."""
        if self.uses_gliner:
            return await asyncio.to_thread(self.hints, records)
        return self.hints(records)


_PRONOUNS = frozenset({"i", "me", "my", "we", "you", "he", "she", "they", "user", "assistant"})


def _checkable(surface: str) -> bool:
    words = surface.split()
    return bool(words) and words[0][:1].isupper() and surface.lower() not in _PRONOUNS


def prepass_stage(prepass: Prepass) -> EnvelopeStage:
    """The order-500 envelope stage: flag (never add or drop) envelopes with an unseen name."""

    async def run(
        envelopes: list[AssertionEnvelope], ctx: EnvelopeStageContext
    ) -> list[AssertionEnvelope]:
        hints = prepass.hints(ctx.records)
        seen = hints.names() | {a.lower() for a in prepass.known_aliases()}
        out: list[AssertionEnvelope] = []
        for env in envelopes:
            unseen = [
                m.surface
                for m in (env.subject, env.object)
                if m is not None
                and _checkable(m.surface)
                and not any(w in seen for w in (m.surface.lower(), *m.surface.lower().split()))
            ]
            if unseen:
                ctx.notes["prepass_unseen_entity"] += 1
                if tap.enabled():
                    tap.event(
                        "write.prepass.unseen", window=ctx.window_id, n=len(unseen)
                    )
                env = env.model_copy(update={"extras": {**env.extras, "prepass_unseen": unseen}})
            out.append(env)
        if hints.nothing_extractable and envelopes:
            ctx.notes["prepass_filler_with_facts"] += len(envelopes)
        prepass.learn(
            m.surface for e in out for m in (e.subject, e.object) if m is not None and _checkable(m.surface)
        )
        return out

    return EnvelopeStage(name="prepass", order=500, run=run)
