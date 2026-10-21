"""W5 (plan v3.2, G01): a rule-based fact miner for personal facts. No model.

``consolidation.miner: rules`` plugs :func:`mine_rules` in where the LLM miner sits
(:func:`memspine.derive.pipeline.stages.mining.mine_facts`), so the whole mining path stays the
same: deposit through the write door, the conflict ladder (a newer home supersedes
the old one), list cards and the cards header. It reads first-person statements of
the speaker of each transcript line ("Caroline: I moved to Sweden ...").

Two kinds of slot, as the LLM miner's ``kind`` field:

- ``state`` (single-valued, a newer value supersedes): home, origin, job, employer,
  relationship status, age, education, diet, ``favourite_<thing>``, ``partner``,
  ``mother``, ``father``;
- ``event`` (many hold at once): likes, dislikes, pets, family members, activities,
  plans.

Every value is a span of the line it came from, cut at the clause end. Span
overlap alone does not establish speaker authority or an assertion: quoted,
reported, conditional and uncertain predicate matches are conservatively omitted.
English, regex only; recall is modest by design
and measured against the LLM miner and gold profiles (plan rows A6, W5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from memspine.prompts.models import ExtractedFact

__all__ = ["RULES", "canonical_thing", "mine_rules"]

#: "[3] [2023-05-08] Caroline: text" or "[2023-05-08] Caroline: text".
_LINE = re.compile(
    r"^(?:\[(?P<n>\d+)\]\s*)?(?:\[(?P<date>\d{4}-\d{2}-\d{2})\]\s*)?"
    r"(?:(?P<speaker>[A-Z][\w .'-]{0,40}?):\s+)?(?P<text>.+)$"
)
#: A value: up to the end of its clause.
_V = (
    r"(?P<v>[^.,!?;:()\n]{2,60}?)"
    r"(?=\s*(?:[.,!?;:()\n]|\band\b|\bbut\b|\bbecause\b|\bso\b|\bwhich\b|$))"
)
_I = r"\bI(?:'ve| have)?"
_IM = r"\bI(?:'m| am)"
_REL_STATE = (
    r"(?P<rel>wife|husband|partner|boyfriend|girlfriend|fiance|fiancee|mom|mother|dad|father)"
)
_REL_EVENT = (
    r"(?P<rel>son|daughter|kid|child|sister|brother|grandma|grandmother|grandpa|grandfather|"
    r"aunt|uncle|cousin|niece|nephew|friend|best friend)"
)
_PET = (
    r"(?P<pet>dog|cat|puppy|kitten|horse|pony|bird|parrot|hamster|rabbit|bunny|fish|"
    r"turtle|snake|pet)"
)
#: A capitalised name, case-sensitive inside the case-insensitive rules.
_NAME = r"(?P<v>(?-i:[A-Z][a-z]+(?: [A-Z][a-z]+)?))\b"


@dataclass(frozen=True)
class Rule:
    attribute: str
    kind: str  # "state" | "event"
    pattern: re.Pattern[str]


def _r(attribute: str, kind: str, pattern: str) -> Rule:
    return Rule(attribute, kind, re.compile(pattern, re.IGNORECASE))


RULES: tuple[Rule, ...] = (
    _r("home", "state", rf"{_I} (?:just |recently )?moved to {_V}"),
    _r("home", "state", rf"\bI (?:now |currently )?live in {_V}"),
    _r("origin", "state", rf"{_IM} (?:originally )?from {_V}"),
    _r("origin", "state", rf"\bI grew up in {_V}"),
    _r("job", "state", rf"\bI(?:'ve)? work(?:ed)? as (?:a|an) {_V}"),
    _r("job", "state", rf"\bmy job (?:is|as) (?:a |an )?{_V}"),
    _r("employer", "state", rf"\bI work (?:at|for) {_V}"),
    _r(
        "relationship_status",
        "state",
        rf"{_IM} (?:now |happily )?(?P<v>single|married|engaged|divorced|widowed|separated)\b",
    ),
    _r("age", "state", rf"{_IM} (?P<v>\d{{1,3}}) (?:years old|yrs old|now)\b"),
    _r("education", "state", rf"{_IM} (?:currently )?studying {_V}"),
    _r("education", "state", rf"\bI graduated (?:from|in) {_V}"),
    _r("diet", "state", rf"{_IM} (?:a |now a )?(?P<v>vegetarian|vegan|pescatarian)\b"),
    _r(
        "likes",
        "event",
        r"\bI (?:really |absolutely )?(?:love|like|enjoy|adore)"
        rf"(?! (?:how|that|what|when|it when|the way) you\b) {_V}",
    ),
    _r(
        "dislikes",
        "event",
        rf"\bI (?:really )?(?:hate|dislike|can't stand|cannot stand|don't like|do not like) {_V}",
    ),
    _r("pets", "event", rf"\bmy {_PET}(?:'s name is|,? named| is called| called) {_NAME}"),
    _r(
        "pets",
        "event",
        rf"{_I} (?:got |adopted )?(?:a |an |two |three )(?:new )?"
        rf"{_PET.replace('(?P<pet>', '(?P<v>')}s?\b",
    ),
    _r(
        "family", "event", rf"\bmy {_REL_EVENT}(?:'s name is|,? named| is called| called|,) {_NAME}"
    ),
    _r(
        "activities",
        "event",
        r"\bI (?:go|went|love going|enjoy going|have been going|started going) (?P<v>[a-z]+ing)\b",
    ),
    _r(
        "activities",
        "event",
        rf"\bI(?:'ve| have)? (?:just |recently )?(?:started|took up|taken up|picked up) {_V}",
    ),
    _r("plans", "event", rf"{_IM} (?:planning|going|hoping) to {_V}"),
    _r("plans", "event", rf"\bI plan to {_V}"),
)

#: ``favourite_<thing>`` and partner / parent names need the slot name from the match.
_FAVOURITE = re.compile(
    rf"\bmy (?:all-time )?favou?rite (?P<thing>[a-z]+(?: [a-z]+)?) (?:is|was|has to be) {_V}",
    re.IGNORECASE,
)
_REL_STATE_RX = re.compile(
    rf"\bmy {_REL_STATE}(?:'s name is|,? named| is called| called|,) {_NAME}", re.IGNORECASE
)
_STOP_VALUES = frozenset({"it", "that", "this", "them", "you", "so", "too", "a lot", "lots"})
#: A value that opens like a clause or a pronoun is not a slot value ("is awesome",
#: "it's tricky", "how art lets us ...", "who passed").
_BAD_START = re.compile(
    r"^(?:is|was|are|were|be|been|it|it's|its|how|that|what|when|where|who|which|this|"
    r"these|those|there|here|to be|so|very|really|just|about|doing|being|having|getting|"
    r"me|you|him|her|us|them|my|your|our|their)\b",
    re.IGNORECASE,
)


#: Time and filler words after a value ("Sweden four years ago", "Ohio originally").
_TRAILING = re.compile(
    r"\s+(?:(?:a few|several|some|\w+) (?:years?|months?|weeks?|days?) ago|ago|originally|"
    r"recently|now|lately|anymore|again|too|as well|last \w+|this \w+|next \w+|yesterday|"
    r"today|tonight|these days|for (?:a while|years|ages|now))$",
    re.IGNORECASE,
)


def _clean(value: str) -> str | None:
    text = " ".join(value.split()).strip(" '\"-")
    previous = None
    while previous != text:
        previous, text = text, _TRAILING.sub("", text).strip()
    if len(text) < 2 or text.lower() in _STOP_VALUES or _BAD_START.match(text):
        return None
    return text


_CONDITIONAL = re.compile(
    r"\b(?:if|unless|suppos(?:e|ing)|assuming|provided|providing|whenever|when)\b",
    re.IGNORECASE,
)
_REPORTING = re.compile(
    r"\b(?:say|says|said|tell|tells|told|claim|claims|claimed|report|reports|reported|"
    r"wrote|writes|texted|asked|suggest(?:s|ed)?|propos(?:e|es|ed)|deny|denies|denied)\b"
    r"|\baccording\s+to\b",
    re.IGNORECASE,
)
_UNCERTAIN_PREFIX = re.compile(
    r"\b(?:maybe|perhaps|possibly|apparently|allegedly|supposedly|would|could|might|"
    r"may|should|wish|imagine|pretend|hypothetically)\b"
    r"|\bI\s+(?:think|believe|guess|suspect|wonder|dreamed|dreamt)\b"
    r"|\b(?:don't|do not|didn't|did not)\s+(?:think|believe|know)\b"
    r"|\bnot\s+(?:true|correct|accurate|sure|that|the\s+case)\b|\bfalse\s+that\b"
    r"|\b(?:isn't|wasn't)\s+(?:true|correct|accurate|the\s+case)\b"
    r"|\b(?:plan(?:ned|ning)?|hope|hoping|want|intend)\s+to\b"
    r"|\bgoing\s+to\b",
    re.IGNORECASE,
)
_INDEPENDENT = re.compile(r"\b(?:but|however|nevertheless)\b", re.IGNORECASE)
_NEXT_CLAUSE = re.compile(r"\b(?:and|but|however|nevertheless)\b", re.IGNORECASE)
_ALTERNATIVE = re.compile(r"\b(?:either|or)\b", re.IGNORECASE)
_UNCERTAIN_SUFFIX = re.compile(
    r"\b(?:maybe|perhaps|possibly|apparently|allegedly|supposedly)\b"
    r"|\bI\s+(?:think|believe|guess|suspect)\b",
    re.IGNORECASE,
)


def _quoted_ranges(text: str) -> list[tuple[int, int]]:
    """Quoted/code ranges; word-internal apostrophes are not quotation marks.

    An unmatched opener protects the remaining line. This deliberately prefers
    omission over treating a partial quotation as an assertion by the speaker.
    """
    pairs = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019", "`": "`"}
    ranges: list[tuple[int, int]] = []
    closing: str | None = None
    start = 0
    for i, char in enumerate(text):
        if i and text[i - 1] == "\\":
            continue
        if (
            char in {"'", "\u2019"}
            and i
            and i + 1 < len(text)
            and text[i - 1].isalnum()
            and text[i + 1].isalnum()
        ):
            continue
        if closing is None and char in {"'", "\u2019"} and i and text[i - 1].isalnum():
            continue  # a trailing possessive apostrophe cannot open a quotation
        if closing is not None:
            if char == closing:
                ranges.append((start, i + 1))
                closing = None
        elif char in pairs:
            start, closing = i, pairs[char]
    if closing is not None:
        ranges.append((start, len(text)))
    return ranges


def _asserted_match(text: str, match: re.Match[str]) -> bool:
    """Conservative English scope guard, not general discourse resolution.

    Quoted values (e.g. a book title) are allowed when the predicate itself is
    outside quotation. Explicit independent contrast clauses can re-establish
    the transcript speaker; an ``and`` does not clear reported/conditional scope.
    """
    quoted = _quoted_ranges(text)
    if any(start <= match.start() < end for start, end in quoted):
        return False
    chars = list(text)
    for start, end in quoted:
        chars[start:end] = " " * (end - start)
    masked = "".join(chars).replace("\u2019", "'")
    # Original punctuation also recognises the end of a quoted sentence, so an
    # independent following sentence is not kept under the old reporting scope.
    previous = list(re.finditer(r"[.!?;\n]", text[: match.start()]))
    start = previous[-1].end() if previous else 0
    following = re.search(r"[.!?;\n]", text[match.end() :])
    end = match.end() + following.start() if following else len(text)
    if following and following.group() == "?":
        return False
    prefix = masked[start : match.start()]
    # Conditionals govern their consequent even across a coordinating clause.
    if _CONDITIONAL.search(prefix):
        return False
    contrasts = list(_INDEPENDENT.finditer(prefix))
    if contrasts:
        prefix = prefix[contrasts[-1].end() :]
    if _REPORTING.search(prefix) or _UNCERTAIN_PREFIX.search(prefix) or _ALTERNATIVE.search(prefix):
        return False
    current = masked[match.start() : end]
    next_clause = _NEXT_CLAUSE.search(current)
    if next_clause:
        current = current[: next_clause.start()]
    return not any(
        pattern.search(current) for pattern in (_CONDITIONAL, _UNCERTAIN_SUFFIX, _ALTERNATIVE)
    )


def _line_slots(text: str) -> list[tuple[str, str, str]]:
    """``(attribute, kind, raw value)`` for every rule match in one line's text."""
    found: list[tuple[str, str, str]] = []
    for rule in RULES:
        for m in rule.pattern.finditer(text):
            if not _asserted_match(text, m):
                continue
            value = m["v"]
            if rule.attribute == "job" and re.search(r" (?:at|for) ", value):
                # "a counselor at the LGBTQ center": the job, then the employer.
                value, employer = re.split(r" (?:at|for) ", value, maxsplit=1)
                found.append(("employer", "state", employer))
            found.append((rule.attribute, rule.kind, value))
    for m in _FAVOURITE.finditer(text):
        if not _asserted_match(text, m):
            continue
        found.append((f"favourite_{canonical_thing(m['thing'])}", "state", m["v"]))
    for m in _REL_STATE_RX.finditer(text):
        if not _asserted_match(text, m):
            continue
        rel = m["rel"].lower()
        slot = {"mom": "mother", "dad": "father"}.get(rel, rel)
        slot = "partner" if slot in {"wife", "husband", "boyfriend", "girlfriend"} else slot
        found.append((slot, "state", m["v"]))
    for m in _NO_LONGER.finditer(text):
        if not _asserted_match(text, m):
            continue
        found.append(("dislikes", "event", m["v"]))
    # N07 (plan v3.2, O-Mem attitude timeline): every like / dislike also sets a
    # single-valued attitude slot for its object, so a change of mind supersedes.
    for attribute, _, value in list(found):
        if attribute in ("likes", "dislikes"):
            obj = _clean(value)
            if obj:
                key = "_".join(obj.lower().split())[:40]
                found.append((f"attitude:{key}", "state", attribute))
    return found


#: N08 (plan v3.2, O-Mem / Memobase slot unification, rule variant): synonymous slot
#: names map to one canonical key, so "favourite novel" and "favourite book" are the
#: same slot and a new value supersedes the old one.
_THING_SYNONYMS: dict[str, str] = {
    "novel": "book",
    "books": "book",
    "author": "writer",
    "movie": "film",
    "movies": "film",
    "films": "film",
    "tv show": "show",
    "tv series": "show",
    "series": "show",
    "dish": "food",
    "meal": "food",
    "cuisine": "food",
    "track": "song",
    "tune": "song",
    "band": "artist",
    "musician": "artist",
    "singer": "artist",
    "hobbies": "hobby",
    "pastime": "hobby",
    "colour": "color",
    "game": "game",
    "video game": "game",
    "sports": "sport",
    "team": "team",
    "place": "place",
    "spot": "place",
    "restaurant": "restaurant",
    "drink": "drink",
    "beverage": "drink",
}


def canonical_thing(thing: str) -> str:
    """N08: the canonical slot word for ``thing`` ("Novel" -> "book")."""
    text = " ".join(thing.lower().split())
    return "_".join(_THING_SYNONYMS.get(text, text).split())


#: N07: "I no longer like / don't enjoy X anymore" is a change of mind to a dislike.
_NO_LONGER = re.compile(
    r"\bI (?:no longer|don't really|do not) (?:like|love|enjoy) "
    r"(?P<v>[^.,!?;:()\n]{2,60}?)(?=\s*(?:[.,!?;:()\n]|\banymore\b|$))",
    re.IGNORECASE,
)


def mine_rules(transcript: str) -> list[ExtractedFact]:
    """The rule facts of a mining transcript (one ``[n] [date] Speaker: text`` per line).

    A line without a speaker is attributed to ``user``. One fact per (line, slot,
    value); a value said twice in a line is kept once."""
    facts: list[ExtractedFact] = []
    for raw in transcript.splitlines():
        match = _LINE.match(raw.strip())
        if match is None:
            continue
        speaker = (match["speaker"] or "user").strip()
        turns = [int(match["n"])] if match["n"] else []
        seen: set[tuple[str, str]] = set()
        for attribute, kind, raw_value in _line_slots(match["text"]):
            value = _clean(raw_value)
            if value is None or (attribute, value.lower()) in seen:
                continue
            seen.add((attribute, value.lower()))
            facts.append(
                ExtractedFact(
                    entity=speaker,
                    attribute=attribute,
                    value=value,
                    date=match["date"],
                    kind="state" if kind == "state" else "event",
                    turns=turns,
                    persons=[speaker] if speaker != "user" else [],
                )
            )
    return facts
