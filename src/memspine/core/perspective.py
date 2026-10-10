# ruff: noqa: E501, RUF001
"""I39-I46: the perspective / attribution layer. Rules only, no model (a decider may refine).

A memory record belongs to ONE owner (its namespace: the hard boundary), but its content
is spoken by someone, to someone, and is about someone or something, with a stance. The
layer records those axes as tags on the record (never in its content) and resolves the same
axes for a question, so the read can match "whose statement" and "about whom" and "how
asserted" instead of matching words alone.

Axes (tag prefixes)::

    spk:<id>      speaker: ``user`` / ``assistant`` / ``tool`` / a lower-cased participant name
    addr:<id>     addressee (who is spoken to)
    sub:<id>      subject; repeatable. ``self`` and ``you`` are resolved to ids at write time
                  (``sub:caroline``); a related third party is ``sub:<relation>@<anchor>``
                  ("my cousin" by Caroline -> ``sub:cousin@caroline``) plus ``sub:<name>`` when
                  named; an unresolved "he / she / they" is ``sub:3p``
    ask:<id>      who asked (question / request turns)
    mod:<m>       modality of the turn's sentences: fact plan wish hypo opinion question request
    pol:neg|pos   polarity: a negated / an affirmed sentence is present
    scope:<s>     event | habit | standing (a one-off, a routine, a trait / preference)
    cert:hedged   a hedge ("maybe", "I think", "not sure")
    rep:<id>      reported speech: the claim's source is not the speaker ("my mom said ...")
    sensitive:<c> the W16 lexicon categories (shared with ``firewall.sensitive_topics``)

An unknown speaker is ``user`` (the namespace owner), so a single-user note store resolves
exactly like a chat. The read side (:func:`resolve_question`, :func:`factor`) is pure; the
engine applies it behind ``read.perspective_mode``. Time (event vs mention time) already
lives in ``valid_from`` / ``happened:`` / ``said:`` tags and is not duplicated here.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

from memspine.core.records import MemoryRecord
from memspine.core.sensitive import sensitive_topics

__all__ = [
    "ALL_AXES",
    "OWNER",
    "Perspective",
    "PerspectiveOptions",
    "PerspectiveResolver",
    "QuestionPerspective",
    "RecordView",
    "Subject",
    "active_asker",
    "asker_scope",
    "attribution_marker",
    "factor",
    "is_hedged",
    "is_negated",
    "match_subject",
    "perspective_leg",
    "record_view",
    "refine_with_decider",
    "resolve_question",
    "resolve_write",
    "scope_of",
    "sentence_modality",
]

#: The namespace owner's id when no speaker is given.
OWNER = "user"
ALL_AXES = ("spk", "addr", "sub", "ask", "mod", "pol", "scope", "cert", "rep", "sens")

_ROLE_IDS = ("user", "assistant", "tool")
_NAME_PREFIX = re.compile(r"^\s*(?P<name>[A-Z][\w'-]{1,30}(?: [A-Z][\w'-]{1,30})?):\s+\S")
_ROLE_PREFIX = re.compile(r"^\s*(user|assistant|tool)\s*:\s", re.I)


@dataclass(frozen=True)
class PerspectiveOptions:
    """Parsed ``memories.episodic.policies.perspective``: ``"heuristic"`` / ``"decider"`` or
    ``{"mode": ..., "axes": [...]}``. Anything else (absent, ``off``, False) is off."""

    mode: str = "off"
    axes: tuple[str, ...] = ALL_AXES

    @classmethod
    def parse(cls, value: Any) -> PerspectiveOptions:
        if isinstance(value, dict):
            mode = str(value.get("mode", "heuristic"))
            axes = tuple(a for a in value.get("axes", ALL_AXES) if a in ALL_AXES)
        elif isinstance(value, str):
            mode, axes = value, ALL_AXES
        else:
            return cls()
        return cls(mode if mode in ("heuristic", "decider") else "off", axes or ALL_AXES)

    @property
    def on(self) -> bool:
        return self.mode != "off"


@dataclass(frozen=True)
class Subject:
    """One thing a text is about. ``id`` is a participant id, ``3p`` or ``<name>``;
    ``rel`` is ``<relation>@<anchor>`` when the text states a relation."""

    id: str
    kind: str  # self | addressee | named | third
    rel: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.id, self.rel) if self.rel else (self.id,)


@dataclass
class Perspective:
    spk: str | None = None
    addr: str | None = None
    subjects: tuple[Subject, ...] = ()
    ask: str | None = None
    modality: frozenset[str] = frozenset()
    polarity: frozenset[str] = frozenset()
    scope: frozenset[str] = frozenset()
    hedged: bool = False
    reported: str | None = None
    sensitive: tuple[str, ...] = ()

    def tags(self, axes: Collection[str] = ALL_AXES) -> list[str]:
        out: list[str] = []
        if "spk" in axes and self.spk:
            out.append(f"spk:{self.spk}")
        if "addr" in axes and self.addr:
            out.append(f"addr:{self.addr}")
        if "sub" in axes:
            for s in self.subjects:
                out.extend(f"sub:{k}" for k in s.keys)
        if "ask" in axes and self.ask:
            out.append(f"ask:{self.ask}")
        if "mod" in axes:
            out.extend(f"mod:{m}" for m in sorted(self.modality))
        if "pol" in axes:
            out.extend(f"pol:{p}" for p in sorted(self.polarity))
        if "scope" in axes:
            out.extend(f"scope:{s}" for s in sorted(self.scope))
        if "cert" in axes and self.hedged:
            out.append("cert:hedged")
        if "rep" in axes and self.reported:
            out.append(f"rep:{self.reported}")
        if "sens" in axes:
            out.extend(f"sensitive:{c}" for c in self.sensitive)
        return list(dict.fromkeys(out))


class PerspectiveResolver(Protocol):
    """The pluggable resolver port. The rules below are the default; a model-backed
    resolver (the decider port) implements the same call and returns a refined view."""

    async def __call__(self, text: str, base: Perspective) -> Perspective: ...


_ASKER: ContextVar[str | None] = ContextVar("memspine_asker", default=None)


@contextmanager
def asker_scope(asker: str | None) -> Iterator[None]:
    """Declare who asks for the reads inside the block (a persona, a participant id)."""
    token = _ASKER.set(asker.strip().lower() if asker else None)
    try:
        yield
    finally:
        _ASKER.reset(token)


def active_asker() -> str | None:
    return _ASKER.get()


# ----------------------------------------------------------------------------- lexicon

_FIRST = re.compile(
    r"\b(?:i|i'm|i've|i'd|i'll|me|my|mine|myself|we|we're|we've|our|ours|us)\b", re.I
)
_SECOND = re.compile(r"\b(?:you|you're|you've|you'd|you'll|your|yours|yourself|yourselves)\b", re.I)
_THIRD = re.compile(r"\b(?:he|she|they|him|them|his|hers?|their|theirs|he's|she's|they're)\b", re.I)

_REL_CANON = {
    "mom": "mother", "mum": "mother", "mama": "mother", "dad": "father", "papa": "father",
    "grandma": "grandmother", "granny": "grandmother", "grandpa": "grandfather",
    "kid": "child", "kids": "child", "children": "child", "friends": "friend",
    "neighbour": "neighbor", "coworker": "colleague", "girlfriend": "partner",
    "boyfriend": "partner", "husband": "spouse", "wife": "spouse",
}  # fmt: skip
_RELATIONS = [
    "mother",
    "mom",
    "mum",
    "mama",
    "father",
    "dad",
    "papa",
    "brother",
    "sister",
    "sibling",
    "son",
    "daughter",
    "kid",
    "kids",
    "child",
    "children",
    "wife",
    "husband",
    "spouse",
    "partner",
    "girlfriend",
    "boyfriend",
    "fiance",
    "fiancee",
    "cousin",
    "aunt",
    "uncle",
    "niece",
    "nephew",
    "grandma",
    "granny",
    "grandmother",
    "grandpa",
    "grandfather",
    "friend",
    "friends",
    "colleague",
    "coworker",
    "boss",
    "manager",
    "neighbor",
    "neighbour",
    "teacher",
    "roommate",
    "dog",
    "cat",
    "pet",
]
_POSS = re.compile(
    r"\b(?P<poss>my|your|his|her|their|our|[A-Za-z][\w-]*['’]s)\s+"
    r"(?:(?:best|close|old|younger|older|little|big|step|ex|new|late)\s+)?"
    r"(?P<rel>" + "|".join(_RELATIONS) + r")\b(?:,?\s+(?P<name>(?-i:[A-Z][a-z]{1,20})))?",
    re.I,
)
_SKIP_NAMES = frozenset(
    [
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "it",
        "the",
        "a",
        "an",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "this",
        "that",
        "and",
        "but",
        "or",
        "so",
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
)

_NEG = re.compile(
    r"\b(?:not|no longer|never|neither|nor|nobody|nothing|none|without|"
    r"(?:do|does|did|is|are|was|were|has|have|had|can|could|would|should|will|wo|ca)n'?t|"
    r"(?:do|does|did)n’t|dont|doesnt|didnt|cannot)\b|n't\b",
    re.I,
)
_NEG_IDIOM = re.compile(
    r"\b(?:not (?:only|bad|sure|yet|just)|no (?:problem|worries|way|doubt|idea)|why not|"
    r"if not|whether or not|not to mention|no matter)\b",
    re.I,
)
_HEDGE = re.compile(
    r"\b(?:maybe|perhaps|probably|possibly|i think|i guess|i suppose|i believe|not sure|"
    r"i'm not certain|might|seems? (?:like|to)|kind of|sort of|i reckon|apparently|"
    r"if i remember|i don't remember exactly|roughly)\b",
    re.I,
)
_QUESTION_WORDS = re.compile(
    r"^\s*(?:what|which|who|whom|whose|where|when|why|how|do|does|did|is|are|was|were|"
    r"can|could|would|will|should|have|has|had|may|shall)\b",
    re.I,
)
_REQUEST = re.compile(
    r"^\s*(?:please\b|(?:can|could|would|will) you\b|help me\b|tell me\b|show me\b|give me\b|"
    r"let'?s\b|i need you to\b|i want you to\b|remind me\b|recommend\b|suggest\b|write\b|"
    r"make\b|find\b|explain\b)",
    re.I,
)
_HYPO = re.compile(
    r"\b(?:if (?:i|we|you|he|she|they|it)\b|what if\b|suppose\b|imagine\b|hypothetically|"
    r"would have\b|could have\b|had i\b|in case)\b",
    re.I,
)
_WISH = re.compile(
    r"\b(?:i wish|i hope|i'd love to|i would love to|i'd like to|i would like to|i want to|"
    r"i dream of|someday|one day|bucket list|i'd rather)\b",
    re.I,
)
_PLAN = re.compile(
    r"\b(?:(?:i|we|he|she|they)(?:'m| am|'re| are|'s| is)? (?:thinking (?:of|about)|planning|"
    r"considering|going to|about to|gonna|intend(?:ing)? to|looking to|aiming to|"
    r"preparing to)|(?:i|we)(?:'ll| will| plan to)|next (?:week|month|year|summer|weekend)|"
    r"tomorrow|tonight|upcoming|soon)\b",
    re.I,
)
_OPINION = re.compile(
    r"\b(?:i think|i believe|in my opinion|imo|i feel like|i feel that|it seems|i reckon|"
    r"i'd say|personally|to me\b)\b",
    re.I,
)
_HABIT = re.compile(
    r"\b(?:usually|always|often|regularly|typically|used to|most (?:days|weekends|nights|"
    r"mornings)|every (?:day|week|month|year|morning|evening|night|weekend|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday)|each (?:day|week|morning)|on weekends|"
    r"daily|weekly|monthly|from time to time|never)\b",
    re.I,
)
_STANDING = re.compile(
    r"\b(?:i(?:'m| am)? (?:really |totally |absolutely |also |just )?(?:love|like|enjoy|prefer|"
    r"hate|dislike|adore|can't stand|cannot stand|am a fan|am into|am passionate|am allergic)|"
    r"my (?:favou?rite|fave|passion|hobby|hobbies)|favou?rite|big fan|i'm vegan|i'm vegetarian|"
    r"allergic to)\b",
    re.I,
)
_EVENT = re.compile(
    r"\b(?:yesterday|last (?:night|week|weekend|month|year|summer|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)|(?:\d+|a few|two|three|several) (?:days?|weeks?|months?|"
    r"years?) ago|this (?:morning|afternoon|evening)|earlier today|just (?:got|went|had|"
    r"finished|came|bought|saw)|went|got back|visited|attended|bought|moved|joined|"
    r"graduated|finished|started|met|saw|had|took|signed up|in (?:19|20)\d\d|"
    r"on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I,
)
_REPORTED = re.compile(
    r"\b(?P<who>(?:my|your|his|her|their|our)\s+(?:[a-z]+\s+)?(?:" + "|".join(_RELATIONS) + r")|"
    r"(?-i:[A-Z][a-z]{1,20})|he|she|they|everyone|people|someone|somebody|the (?:doctor|teacher|boss|"
    r"news))\s+(?:said|says|told (?:me|us)|tells? (?:me|us)|mentioned|claims?|thinks? that|"
    r"believes? that|reckons?|reported|heard)\b|\b(?P<anon>i heard|i was told|apparently|"
    r"they say|rumou?r has it|according to)\b",
    re.I,
)
_REPORTED_STOP = frozenset(["i", "you", "we"])

# ----------------------------------------------------------------------------- axes


def _clauses(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+|;\s+", text)
    return [p.strip() for p in parts if p and p.strip()]


def sentence_modality(sentence: str) -> str:
    """One of fact plan wish hypo opinion question request for a sentence."""
    s = sentence.strip()
    if _REQUEST.match(s):
        return "request"
    if s.endswith("?") or (_QUESTION_WORDS.match(s) and "?" in s):
        return "question"
    if _HYPO.search(s):
        return "hypo"
    if _WISH.search(s):
        return "wish"
    if _PLAN.search(s):
        return "plan"
    if _OPINION.search(s):
        return "opinion"
    return "fact"


def is_negated(sentence: str) -> bool:
    """A negation cue that is not an idiom ("no problem", "not bad", "why not")."""
    return bool(_NEG.search(_NEG_IDIOM.sub(" ", sentence)))


def is_hedged(text: str) -> bool:
    return bool(_HEDGE.search(text))


def scope_of(text: str) -> frozenset[str]:
    """event / habit / standing cues in ``text`` (any of them; empty when none)."""
    out = set()
    if _STANDING.search(text):
        out.add("standing")
    if _HABIT.search(text):
        out.add("habit")
    deixis = re.search(r"\b(?:yesterday|last|ago|today|this|earlier)\b", text, re.I)
    if _EVENT.search(text) and (not out or deixis):
        out.add("event")
    return frozenset(out)


def _canon_rel(rel: str) -> str:
    r = rel.lower()
    return _REL_CANON.get(r, r)


def _split_speaker(
    content: str, speaker: str | None, role: str | None
) -> tuple[str | None, str | None, str]:
    """(speaker id, role, body): explicit speaker > ``Name:`` text prefix > chat role."""
    body = content
    role_id = (role or "").lower() or None
    pm = _ROLE_PREFIX.match(content)
    if pm:
        role_id = pm[1].lower()  # a text prefix beats a default role
        body = content[pm.end() :]
    nm = _NAME_PREFIX.match(content)
    named = nm["name"].lower() if nm else None
    if nm:
        body = content.split(":", 1)[1].lstrip()
    if speaker:
        return speaker.strip().lower(), role_id, body
    if named:
        return named, role_id, body
    if role_id in _ROLE_IDS:
        return role_id, role_id, body
    return None, role_id, body


def _participant_names(body: str, known: Iterable[str]) -> list[str]:
    low = body.lower()
    return [
        k
        for k in known
        if k not in _ROLE_IDS and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", low) is not None
    ]


def resolve_write(
    content: str,
    *,
    speaker: str | None = None,
    role: str | None = None,
    addressee: str | None = None,
    known: Collection[str] = (),
    previous: str | None = None,
    axes: Collection[str] = ALL_AXES,
) -> Perspective:
    """The perspective of one stored turn. ``known`` = the participants seen so far in the
    namespace (lower-case ids), ``previous`` = the speaker of the preceding turn."""
    spk, _role_id, body = _split_speaker(content, speaker, role)
    me = spk or OWNER
    human = spk is not None and spk not in ("assistant", "tool")
    humans = {k for k in known if k not in ("assistant", "tool")} - {me}
    # --- addressee
    addr = addressee.strip().lower() if addressee else None
    if addr is None:
        if me == "user":
            addr = "assistant" if "assistant" in known else None
        elif me == "assistant":
            addr = "user"
        elif previous and previous != me:
            addr = previous
        elif len(humans) == 1:
            addr = next(iter(humans))
    # --- subjects
    subjects: list[Subject] = []
    seen: set[str] = set()

    def add(s: Subject) -> None:
        if s.id + (s.rel or "") not in seen:
            seen.add(s.id + (s.rel or ""))
            subjects.append(s)

    for m in _POSS.finditer(body):
        poss = m["poss"].lower()
        rel = _canon_rel(m["rel"])
        if poss in ("my", "our"):
            anchor = me
        elif poss == "your":
            anchor = addr or "addressee"
        elif poss.endswith(("'s", "’s")):
            anchor = poss[:-2]
        else:
            anchor = "3p"
        name = m["name"]
        relkey = f"{rel}@{anchor}"
        if name and name.lower() not in _SKIP_NAMES:
            add(Subject(name.lower(), "named", relkey))
        else:
            add(Subject(f"{rel}@{anchor}", "third", None))
    rest = _POSS.sub(" ", body)
    first = bool(_FIRST.search(rest))
    second = bool(_SECOND.search(rest))
    if first and (human or spk is None):
        add(Subject(me, "self"))
    elif first and spk == "assistant":
        add(Subject("assistant", "self"))
    if second:
        add(Subject(addr or "addressee", "addressee"))
    for name in _participant_names(rest, set(known) | ({addr} if addr else set())):
        if name == me:
            add(Subject(me, "self"))
        elif name == addr:
            add(Subject(name, "addressee"))
        else:
            add(Subject(name, "named"))
    if not any(s.kind in ("named", "third") for s in subjects) and _THIRD.search(rest):
        add(Subject("3p", "third"))
    if not subjects and human:
        add(Subject(me, "self"))  # "Went hiking yesterday": an elliptical first person
    # --- stance axes, per sentence
    mods: set[str] = set()
    pols: set[str] = set()
    for sent in _clauses(body):
        m = sentence_modality(sent)
        mods.add(m)
        if m in ("fact", "plan", "wish", "opinion", "hypo"):
            pols.add("neg" if is_negated(sent) else "pos")
    mods = mods or {"fact"}
    rep = _REPORTED.search(body)
    reported: str | None = None
    if rep:
        who = (rep["who"] or "").lower()
        if rep["anon"] or not who or who in _REPORTED_STOP:
            reported = "unspecified" if rep["anon"] else None
        else:
            pm = _POSS.match(rep["who"])
            if pm:
                poss = pm["poss"].lower()
                anchor = (
                    me
                    if poss in ("my", "our")
                    else (addr or "addressee")
                    if poss == "your"
                    else "3p"
                )
                reported = f"{_canon_rel(pm['rel'])}@{anchor}"
            else:
                reported = who.replace("the ", "")
    p = Perspective(
        spk=spk,
        addr=addr,
        subjects=tuple(subjects),
        ask=me if mods & {"question", "request"} else None,
        modality=frozenset(mods),
        polarity=frozenset(pols),
        scope=scope_of(body),
        hedged=is_hedged(body),
        reported=reported,
        sensitive=tuple(sensitive_topics(body)) if "sens" in axes else (),
    )
    return p


# ------------------------------------------------------------------- decider refinement

#: ``decide(task, text) -> (label, confidence)`` over the decider port's tasks.
DecideFn = Callable[[str, str], Awaitable[tuple[str, float | None]]]


async def refine_with_decider(
    base: Perspective, body: str, decide: DecideFn, min_confidence: float = 0.5
) -> Perspective:
    """Let a decider answer the typed yes/no questions the rules only guess: is the turn a
    plain statement of fact (``is_fact``), is it negated (``is_negated``), does it state a
    standing trait (``is_standing``), is it hedged (``is_hedged``). A label below
    ``min_confidence`` or a failure keeps the rule's answer."""

    async def ask(task: str, yes: str) -> bool | None:
        try:
            label, conf = await decide(task, body)
        except Exception:
            return None
        if conf is None or conf < min_confidence:
            return None
        return label == yes

    out = Perspective(**{**base.__dict__})
    fact = await ask("is_fact", "fact")
    if fact is not None:
        mods = set(base.modality) - {"fact"} if not fact else set(base.modality) | {"fact"}
        out.modality = frozenset(mods or {"fact"} if fact else (mods or {"opinion"}))
    neg = await ask("is_negated", "negated")
    if neg is not None:
        out.polarity = frozenset({"neg"} if neg else {"pos"})
    standing = await ask("is_standing", "standing")
    if standing is not None:
        scope = set(base.scope) - {"standing"}
        out.scope = frozenset(scope | ({"standing"} if standing else set()))
    hedged = await ask("is_hedged", "hedged")
    if hedged is not None:
        out.hedged = hedged
    return out


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class RecordView:
    spk: str | None = None
    addr: str | None = None
    subs: frozenset[str] = frozenset()
    mods: frozenset[str] = frozenset()
    pols: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    hedged: bool = False
    reported: str | None = None
    sensitive: frozenset[str] = frozenset()

    @property
    def annotated(self) -> bool:
        return bool(self.spk or self.subs or self.mods)


def _vals(tags: Iterable[str], prefix: str) -> list[str]:
    n = len(prefix)
    return [t[n:] for t in tags if t.startswith(prefix) and len(t) > n]


def record_view(record: MemoryRecord) -> RecordView:
    """The perspective tags of a stored record."""
    tags = record.tags
    spk = _vals(tags, "spk:")
    addr = _vals(tags, "addr:")
    rep = _vals(tags, "rep:")
    return RecordView(
        spk=spk[0] if spk else None,
        addr=addr[0] if addr else None,
        subs=frozenset(_vals(tags, "sub:")),
        mods=frozenset(_vals(tags, "mod:")),
        pols=frozenset(_vals(tags, "pol:")),
        scopes=frozenset(_vals(tags, "scope:")),
        hedged="cert:hedged" in tags,
        reported=rep[0] if rep else None,
        sensitive=frozenset(_vals(tags, "sensitive:")),
    )


# ---------------------------------------------------------------------------- reading


@dataclass
class QuestionPerspective:
    """Who asks and who / what the question is about. ``targets`` empty = unresolved."""

    asker: str | None = None
    targets: tuple[Subject, ...] = ()
    about: str = "none"  # self | assistant | participant | third | mixed | none
    negated: bool = False
    wants_nonfact: bool = False
    scope: str | None = None  # trait | event
    sensitive: frozenset[str] = field(default_factory=frozenset)

    @property
    def bound(self) -> bool:
        return bool(self.targets)

    def meta(self) -> dict[str, Any]:
        return {
            "asker": self.asker,
            "about": self.about,
            "targets": [s.keys for s in self.targets],
            "negated": self.negated,
            "wants_nonfact": self.wants_nonfact,
            "scope": self.scope,
            "sensitive": sorted(self.sensitive),
        }


_TRAIT_Q = re.compile(
    r"\b(?:like|likes|enjoy|enjoys|prefer|prefers|favou?rite|hobbies|hobby|interested in|"
    r"usually|typically|always|often|into|love|loves|passion|fan of|habit|routine)\b",
    re.I,
)
_EVENT_Q = re.compile(
    r"\b(?:when|what day|what date|yesterday|last (?:week|month|year|night|weekend)|how many "
    r"times|did \w+ (?:go|do|buy|visit|attend|meet|see|move|get|take|make))\b",
    re.I,
)
_NONFACT_Q = re.compile(
    r"\b(?:plan|planning|plans|going to|want|wants|wish|hope|hopes|thinking of|thinking about|"
    r"ask|asked|asking|wonder|wondered|hypothetically|would|could|dream|intend|intends)\b",
    re.I,
)
_NEG_Q = re.compile(
    r"\b(?:not|never|no longer|n't|dislike|dislikes|hate|hates|avoid|avoids|refuse|refuses|"
    r"allergic|can't stand|cannot|without)\b|n't\b",
    re.I,
)
_EVER_Q = re.compile(r"\b(?:ever|did .* not|didn't|doesn't|never)\b", re.I)


def resolve_question(
    query: str,
    *,
    asker: str | None = None,
    known: Collection[str] = (),
) -> QuestionPerspective:
    """The perspective of a question. ``asker`` is the configured / context asker; with none,
    ``user`` when the store has user turns, a lone human participant, else unresolved (a
    first-person question then binds to nothing and the layer stays neutral)."""
    humans = sorted(k for k in known if k not in ("assistant", "tool"))
    who = asker.strip().lower() if asker else None
    if who is None:
        if "user" in known or not known:
            who = OWNER
        elif len(humans) == 1:
            who = humans[0]
    targets: list[Subject] = []

    def add(s: Subject) -> None:
        if all(s.keys != t.keys for t in targets):
            targets.append(s)

    other = next((h for h in humans if h != who), None) if len(humans) == 2 else None
    for m in _POSS.finditer(query):
        poss = m["poss"].lower()
        rel = _canon_rel(m["rel"])
        if poss in ("my", "our"):
            anchor = who
        elif poss == "your":
            anchor = "assistant" if "assistant" in known else other
        elif poss.endswith(("'s", "’s")):
            anchor = poss[:-2]
        else:
            anchor = None
        if anchor is None:
            continue
        name = m["name"]
        relkey = f"{rel}@{anchor}"
        if name and name.lower() not in _SKIP_NAMES:
            add(Subject(name.lower(), "named", relkey))
        else:
            add(Subject(relkey, "third"))
    rest = _POSS.sub(" ", query)
    if _FIRST.search(rest) and who:
        add(Subject(who, "self"))
    if _SECOND.search(rest):
        if "assistant" in known:
            add(Subject("assistant", "addressee"))
        elif other:
            add(Subject(other, "addressee"))
    for name in _participant_names(rest, known):
        add(Subject(name, "self" if name == who else "named"))
    kinds = {
        "self" if s.kind == "self" else "assistant" if s.id == "assistant" else
        "third" if s.kind == "third" or s.rel else "participant"
        for s in targets
    }  # fmt: skip
    about = "none" if not kinds else next(iter(kinds)) if len(kinds) == 1 else "mixed"
    neg = bool(_NEG_Q.search(query)) or bool(_EVER_Q.search(query))
    scope = "trait" if _TRAIT_Q.search(query) else "event" if _EVENT_Q.search(query) else None
    return QuestionPerspective(
        asker=who,
        targets=tuple(targets),
        about=about,
        negated=neg,
        wants_nonfact=bool(_NONFACT_Q.search(query)),
        scope=scope,
        sensitive=frozenset(sensitive_topics(query)),
    )


def match_subject(qp: QuestionPerspective, rv: RecordView) -> float | None:
    """1.0 the record is about a target, 0.6 a target speaks of someone else, 0.5 an
    unresolved third party, 0.0 about someone / something else; None = no information."""
    if not qp.bound or not rv.annotated:
        return None
    best = 0.0
    for t in qp.targets:
        if any(k in rv.subs for k in t.keys):
            return 1.0
        if rv.spk == t.id:
            best = max(best, 1.0 if not rv.subs else 0.6)
        elif t.kind in ("third", "named") and "3p" in rv.subs:
            best = max(best, 0.5)
    return best


def factor(
    qp: QuestionPerspective,
    rv: RecordView,
    weight: float,
    axes: Collection[str] = ("subject",),
) -> tuple[float, dict[str, float]]:
    """The multiplier for a record's relevance, and the per-axis parts that were not 1."""
    parts: dict[str, float] = {}
    if "subject" in axes:
        m = match_subject(qp, rv)
        if m is not None and m < 1.0:
            parts["subject"] = 1.0 - weight * (1.0 - m)
    if "modality" in axes and rv.mods and "fact" not in rv.mods and not qp.wants_nonfact:
        parts["modality"] = 1.0 - weight
    if "polarity" in axes and "neg" in rv.pols and "pos" not in rv.pols and not qp.negated:
        parts["polarity"] = 1.0 - weight
    if "scope" in axes and qp.scope == "trait" and rv.scopes == frozenset({"event"}):
        parts["scope"] = 1.0 - weight * 0.5
    if "sensitivity" in axes and rv.sensitive and not (rv.sensitive & qp.sensitive):
        parts["sensitivity"] = 1.0 - weight
    out = 1.0
    for v in parts.values():
        out *= v
    return out, parts


def perspective_leg(
    qp: QuestionPerspective,
    records: Iterable[MemoryRecord],
    ranked_ids: Iterable[str],
    top_k: int,
) -> list[str]:
    """The subject vote: ids of records about a target (match 1.0), in the order of the
    ranked ids (the vector leg), at most ``top_k``. Empty when the question is unresolved."""
    if not qp.bound:
        return []
    by_id = {r.record_id: r for r in records}
    out: list[str] = []
    for rid in ranked_ids:
        rec = by_id.get(rid)
        if rec is not None and match_subject(qp, record_view(rec)) == 1.0:
            out.append(rid)
            if len(out) >= top_k:
                break
    return out


def attribution_marker(record: MemoryRecord) -> str | None:
    """``[about: Caroline's cousin]`` when the record's subject differs from its speaker."""
    rv = record_view(record)
    if not rv.spk or not rv.subs:
        return None
    others = [s for s in sorted(rv.subs) if s != rv.spk and s not in ("3p", "assistant", "user")]
    rel = [s for s in others if "@" in s]
    shown: str | None = None
    if rel:
        r, _, anchor = rel[0].partition("@")
        anchor = "their" if anchor in ("3p", "addressee") else anchor
        shown = f"{anchor.title()}'s {r}" if anchor != "their" else f"someone's {r}"
        named = [s for s in others if "@" not in s]
        if named:
            shown = f"{named[0].title()} ({shown})"
    elif others:
        shown = others[0].title()
    elif rv.subs == frozenset({"3p"}):
        shown = "a third party"
    return f"[about: {shown}]" if shown else None
