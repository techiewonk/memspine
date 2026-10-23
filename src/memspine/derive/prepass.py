# ruff: noqa: SIM905
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
(list of surfaces) on a flagged envelope and, in GLiNER mode, source-bound unasserted
``extras["gliner_mentions"]`` annotations for cited source turns. Mentions are not evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from memspine.config.schema.model import GlinerSettings
from memspine.derive.envelope_stages import EnvelopeStage, EnvelopeStageContext
from memspine.derive.gliner_mentions import (
    GlinerMentionError,
    MentionSpan,
    bind_gliner_mentions,
    parse_gliner_mentions,
)
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.entity_mentions import EntityMention
from memspine.services.extraction.base import ExtractionModels

__all__ = [
    "FILLER_VOCAB",
    "GlinerHints",
    "Hints",
    "Prepass",
    "PrepassMode",
    "Span",
    "is_filler",
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
    confidence: float | None = None


@dataclass(frozen=True)
class Hints:
    """What the pre-pass saw in one window's turns."""

    mode: str
    turns: int
    entities: tuple[Span, ...] = ()
    #: Exact GLiNER mentions in original source-record coordinates. These are annotations only.
    gliner_mentions: tuple[tuple[int, MentionSpan], ...] = ()
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


def is_filler(text: str) -> bool:
    """True when ``text`` is a pure filler turn (greeting, acknowledgement, emoji only)."""
    return _is_filler(text)


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
    gliner_mentions: Sequence[tuple[int, MentionSpan]] = (),
) -> Hints:
    nothing = bool(turns) and len(filler) == turns and not (entities or dates or numbers)
    return Hints(
        mode=mode,
        turns=turns,
        entities=tuple(entities),
        gliner_mentions=tuple(gliner_mentions),
        dates=tuple(dates),
        numbers=tuple(numbers),
        known=tuple(known),
        speakers=tuple(dict.fromkeys(speakers)),
        filler_lines=tuple(filler),
        nothing_extractable=nothing,
    )


#: labels asked of GLiNER2.5 (the decision provider's set plus date and quantity)
GLINER_LABELS = ("person", "place", "organization", "event", "date", "quantity")
GLINER_DEFAULT_MODEL = GlinerSettings().model


class GlinerHints:
    """GLiNER2 entity finder (local CPU, ``[ner]``). Loads lazily; ``available()`` is False when
    the package is missing, in which case the caller uses the rules alone."""

    def __init__(self, pool: ExtractionModels, settings: GlinerSettings) -> None:
        self._settings = settings
        # RM-73: one Engine-lifetime lease, not an acquire/release per call.
        self._model = pool.binding(settings)
        if "entities" not in settings.tasks:
            raise ValueError(
                "model.extraction.prepass settings.tasks must include 'entities' for the write pre-pass"
            )

    @property
    def producer_id(self) -> str:
        settings = self._settings
        return (
            f"gliner2:{settings.model}@{settings.revision or 'default'}:{settings.device}:"
            f"local={settings.local_files_only}:tasks={','.join(sorted(settings.tasks))}:"
            f"concurrency={settings.inference_concurrency}:prepass-v2-source-bound"
        )

    @staticmethod
    def available() -> bool:
        try:
            from memspine.services.decider.gliner2 import auto_extractor_class

            auto_extractor_class()
        except Exception:
            return False
        return True

    async def aentities(self, text: str) -> tuple[MentionSpan, ...]:
        """Return exact source spans; malformed or ambiguous output fails closed."""
        raw = await self._model.call(
            lambda model: model.extract_entities(
                text,
                list(GLINER_LABELS),
                include_confidence=True,
                include_spans=True,
            )
        )
        return parse_gliner_mentions(text, raw, GLINER_LABELS)


def _gliner_spans(finder: Any, records: Sequence[Any]) -> list[Span]:
    out: list[Span] = []
    for n, rec in enumerate(records, 1):
        text = _content(rec)
        raw_found = finder.entities(text)
        typed: dict[str, list[str]] = {}
        for surface, label in raw_found:
            typed.setdefault(label, []).append(surface)
        try:
            found = parse_gliner_mentions(text, typed, tuple(typed))
        except GlinerMentionError:
            continue
        out.extend(
            Span(item.text, item.start, item.end, n, item.label, "gliner", item.confidence)
            for item in found
        )
    return out


def prepass_hints(
    records: Sequence[Any],
    *,
    mode: PrepassMode = "rules",
    known_aliases: Iterable[str] = (),
    gliner: Any = None,
) -> Hints:
    """Hints for one window's raw turns. ``off`` returns an empty, never-skipping :class:`Hints`.

    ``gliner`` is a synchronous test finder. Integrated inference runs through
    :meth:`Prepass.ahints` and the engine-owned asynchronous model pool."""
    if mode == "off":
        return Hints(mode="off", turns=len(records))
    base = rule_hints(records, known_aliases=known_aliases, mode=mode)
    if mode != "gliner" or gliner is None:
        return base
    extra = _gliner_spans(gliner, records)
    return _with_gliner_spans(base, extra)


def _with_gliner_spans(base: Hints, extra: list[Span]) -> Hints:
    have = {(s.line, s.start, s.end, s.text.lower()) for s in base.entities}
    added = [s for s in extra if (s.line, s.start, s.end, s.text.lower()) not in have]
    return _finish(
        base.mode,
        base.turns,
        [*base.entities, *(s for s in added if s.label not in ("date", "quantity"))],
        [*base.dates, *(s for s in added if s.label == "date")],
        [*base.numbers, *(s for s in added if s.label == "quantity")],
        list(base.known),
        list(base.filler_lines),
        base.speakers,
        base.gliner_mentions,
    )


class Prepass:
    """The configured pre-pass of one engine: mode, learned aliases, a small hints cache."""

    def __init__(
        self,
        mode: PrepassMode = "rules",
        *,
        gliner: Any = None,
        gliner_pool: ExtractionModels | None = None,
        gliner_settings: GlinerSettings | None = None,
        aliases: Callable[[str], Iterable[str]] | None = None,
        cache_size: int = 64,
    ) -> None:
        if mode == "gliner" and gliner is None:
            if gliner_pool is not None and gliner_settings is not None:
                finder = GlinerHints(gliner_pool, gliner_settings)
                if finder.available():
                    gliner = finder
            else:
                _log.warning(
                    "prepass.gliner_unavailable",
                    detail="engine GLiNER pool is unavailable; using the rules",
                )
        if mode == "gliner" and gliner is None:
            from memspine.services.decider.gliner2 import auto_extractor_class

            try:
                auto_extractor_class()
            except Exception:
                _log.warning(
                    "prepass.gliner_unavailable",
                    detail="write.prepass=gliner but gliner2 is not installed; using the rules",
                )
        self.mode: PrepassMode = mode
        self._gliner = gliner
        self._aliases = aliases
        #: RM-18: learned aliases per namespace; one namespace's surfaces never hint in another
        self._learned: dict[str, dict[str, None]] = {}
        #: FU-GAPS: aliases learned since the last drain, per namespace
        self._fresh: dict[str, list[str]] = {}
        self._cache: OrderedDict[tuple[str, ...], Hints] = OrderedDict()
        self._cache_size = cache_size
        self.skipped = 0

    @property
    def uses_gliner(self) -> bool:
        return self.mode == "gliner" and self._gliner is not None

    def learn(self, names: Iterable[str], *, namespace: str) -> None:
        """Remember entity surfaces the pipeline deposited in ``namespace`` (the 'known aliases'
        of that namespace's later windows only)."""
        learned = self._learned.setdefault(namespace, {})
        for n in names:
            if n and len(n) > 1 and n not in learned:
                learned[n] = None
                self._fresh.setdefault(namespace, []).append(n)

    def drain_new(self, namespace: str) -> list[str]:
        """Aliases ``namespace`` learned since its last drain (what its markers have not seen)."""
        return self._fresh.pop(namespace, [])

    def restore(self, names: Iterable[str], *, namespace: str) -> int:
        """Re-learn ``namespace``'s logged aliases at start (not queued again); returns how many
        were added."""
        learned = self._learned.setdefault(namespace, {})
        n = 0
        for name in names:
            if name and len(name) > 1 and name not in learned:
                learned[name] = None
                n += 1
        return n

    def forget(self, namespace: str) -> None:
        """Drop everything ``namespace`` learned, queued or not (its erasure); cached hints of the
        namespace go too."""
        self._learned.pop(namespace, None)
        self._fresh.pop(namespace, None)
        marker = f"namespace:{namespace}"
        for key in [k for k in self._cache if len(k) > 1 and k[1] == marker]:
            del self._cache[key]

    def known_aliases(self, namespace: str) -> list[str]:
        """The aliases learned in ``namespace`` plus the configured extra source's for it."""
        extra = list(self._aliases(namespace)) if self._aliases is not None else []
        return [*self._learned.get(namespace, {}), *extra]

    def hints(self, records: Sequence[Any], *, namespace: str) -> Hints:
        """Synchronous rules-only hints; model inference is restricted to :meth:`ahints`."""
        key = self._cache_key(records, "sync", namespace)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = prepass_hints(
            records,
            mode=self.mode,
            known_aliases=self.known_aliases(namespace),
            gliner=(self._gliner if hasattr(self._gliner, "entities") else None),
        )
        self._cache[key] = out
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return out

    async def ahints(self, records: Sequence[Any], *, namespace: str) -> Hints:
        """Hints without blocking the event loop (GLiNER2 runs in a worker thread)."""
        producer = str(getattr(self._gliner, "producer_id", "rules"))
        key = self._cache_key(records, f"async:{producer}", namespace)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        aliases = self.known_aliases(namespace)
        base = rule_hints(records, known_aliases=aliases, mode=self.mode)
        finder = self._gliner
        if self.mode == "gliner" and finder is not None:
            extra: list[Span] = []
            mentions: list[tuple[int, MentionSpan]] = []
            for line, record in enumerate(records, 1):
                text = _content(record)
                async_entities = getattr(finder, "aentities", None)
                try:
                    if callable(async_entities):
                        found = async_entities(text)
                        if hasattr(found, "__await__"):
                            found = await found
                    else:
                        # Test finders can use the same typed parser result contract.
                        raw_found = await asyncio.to_thread(finder.entities, text)
                        typed: dict[str, list[str]] = {}
                        for surface, label in raw_found:
                            typed.setdefault(label, []).append(surface)
                        found = parse_gliner_mentions(text, typed, tuple(typed))
                    if not all(isinstance(item, MentionSpan) for item in found):
                        raise GlinerMentionError("pre-pass finder returned unvalidated mentions")
                except GlinerMentionError:
                    _log.warning("prepass.gliner_invalid_mentions", line=line)
                    continue
                mentions.extend((line, item) for item in found)
                extra.extend(
                    Span(
                        item.text, item.start, item.end, line, item.label, "gliner", item.confidence
                    )
                    for item in found
                )
            base = _with_gliner_spans(base, extra)
            base = replace(base, gliner_mentions=tuple(mentions))
        self._cache[key] = base
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return base

    @staticmethod
    def _cache_key(records: Sequence[Any], producer: str, namespace: str) -> tuple[str, ...]:
        identities: list[str] = []
        for record in records:
            source = getattr(record, "source", None)
            message = getattr(source, "message", None) or {}
            sender = message.get("sender", {}) if isinstance(message, dict) else {}
            axes = getattr(record, "axes", None)
            identity = {
                "namespace": getattr(record, "namespace", None),
                "record_id": getattr(record, "record_id", None),
                "version": getattr(record, "version", None),
                "content": _content(record),
                "role": getattr(source, "role", None),
                "channel": getattr(source, "channel", None),
                "source_parents": getattr(source, "parents", None),
                "source_message_id": getattr(source, "message_id", None),
                "message": message,
                "sender": sender,
                "speaker": getattr(axes, "speaker", None),
            }
            encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode()
            identities.append(hashlib.sha256(encoded).hexdigest())
        return (
            producer,
            f"namespace:{namespace}",
            *identities,
        )


_PRONOUNS = frozenset({"i", "me", "my", "we", "you", "he", "she", "they", "user", "assistant"})


def _checkable(surface: str) -> bool:
    words = surface.split()
    return bool(words) and words[0][:1].isupper() and surface.lower() not in _PRONOUNS


def prepass_stage(prepass: Prepass) -> EnvelopeStage:
    """The order-500 envelope stage: flag (never add or drop) envelopes with an unseen name."""

    async def run(
        envelopes: list[AssertionEnvelope], ctx: EnvelopeStageContext
    ) -> list[AssertionEnvelope]:
        hints = await prepass.ahints(ctx.records, namespace=ctx.namespace)
        seen = hints.names() | {a.lower() for a in prepass.known_aliases(ctx.namespace)}
        records_by_id = {str(getattr(record, "record_id", "")): record for record in ctx.records}
        mentions_by_record: dict[str, list[MentionSpan]] = {}
        for line, mention in hints.gliner_mentions:
            if 1 <= line <= len(ctx.records):
                record = ctx.records[line - 1]
                mentions_by_record.setdefault(str(getattr(record, "record_id", "")), []).append(
                    mention
                )
        out: list[AssertionEnvelope] = []
        for env in envelopes:
            unseen = [
                m.surface
                for m in (env.subject, env.object)
                if m is not None
                and _checkable(m.surface)
                and not any(w in seen for w in (m.surface.lower(), *m.surface.lower().split()))
            ]
            extras = dict(env.extras)
            # The extraction model does not own this reserved system annotation.
            extras.pop("gliner_mentions", None)
            if unseen:
                ctx.notes["prepass_unseen_entity"] += 1
                if tap.enabled():
                    tap.event("write.prepass.unseen", window=ctx.window_id, n=len(unseen))
                extras["prepass_unseen"] = unseen
            cited_ids = {
                evidence.turn.source_id
                for evidence in env.evidence
                if evidence.turn is not None and evidence.turn.source_id in records_by_id
            }
            bound: list[EntityMention] = []
            for record_id in sorted(cited_ids):
                record = records_by_id[record_id]
                record_mentions = mentions_by_record.get(record_id, [])
                if record_mentions:
                    try:
                        bound.extend(bind_gliner_mentions(record, record_mentions))
                    except GlinerMentionError:
                        _log.warning(
                            "prepass.gliner_source_changed",
                            record_id=record_id,
                            detail="source no longer matches validated mention offsets",
                        )
            if bound:
                extras["gliner_mentions"] = [mention.model_dump(mode="json") for mention in bound]
            if extras != env.extras:
                env = env.model_copy(update={"extras": extras})
            out.append(env)
        if hints.nothing_extractable and envelopes:
            ctx.notes["prepass_filler_with_facts"] += len(envelopes)
        prepass.learn(
            (
                m.surface
                for e in out
                for m in (e.subject, e.object)
                if m is not None and _checkable(m.surface)
            ),
            namespace=ctx.namespace,
        )
        return out

    return EnvelopeStage(name="prepass", order=500, run=run)
