"""W12 / G08 (plan v3.2, ADR-061): deterministic edges from conversation turns.

No model is called. Two extractors, both pure functions over text:

- **Causal links** (:func:`causal_clauses`, :func:`causal_links`). A sentence with a
  causal connective splits into a cause clause and an effect clause: "E because C",
  "E since C", "E after C" put the cause after the connective; "C, so E" and
  "C. As a result, E" put the effect after it; a sentence that opens with
  "Because / Since / After C, E" keeps the cause before the first comma. A clause
  that names something said earlier (content-word overlap with one of the
  previous ``lookback`` turns, at least ``min_overlap`` words) links the two turns.
  The edge always runs **effect -> cause**: ``src`` is the turn holding or naming
  the effect, ``dst`` the one holding or naming the cause. A turn opening with
  "Because ..." answers the turn before it (that turn is the effect); one opening
  with "That's why ..." is the effect of the turn before it.
- **Kinship / role relations** (:func:`kinship_relations`): "my sister Ana",
  "Ana, my boss", "Ana is my mentor" -> ``(person, relation, owner)`` where the
  owner is the turn's speaker ("Name: text"), else ``user``.

The rules over-generate on purpose: the read side (``read.causal_walk``) ranks
what a walk reaches, it never admits a turn on an edge alone. Opt-in through
``memories.associative.policies.rule_edges``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from memspine.core.query_shape import content_words
from memspine.core.temporal_query import speaker_of

__all__ = [
    "BECAUSE_REL",
    "CausalClause",
    "CausalLink",
    "KinshipRelation",
    "causal_clauses",
    "causal_links",
    "is_why_question",
    "kinship_relations",
    "strip_speaker",
]

#: The rel of a rule causal edge (effect -> cause).
BECAUSE_REL = "because"

#: Connectives whose clause after them is the cause ("E because C").
_CAUSE_NEXT = ("because of", "because", "'cause", "cuz", "since", "after", "due to")
#: Connectives whose clause after them is the effect ("C, so E").
_EFFECT_NEXT = ("as a result", "that's why", "that is why", "which is why", "so")

_CONNECTIVE = re.compile(
    r"(?<![\w'])("
    + "|".join(re.escape(c) for c in sorted((*_CAUSE_NEXT, *_EFFECT_NEXT), key=len, reverse=True))
    + r")(?![\w'])",
    re.I,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_SPEAKER_HEAD = re.compile(r"^\s*[A-Z][\w'-]{1,30}(?: [A-Z][\w'-]{1,30})?:\s+")
#: "so" marks a cause only as a clause joiner (", so" / "; so") or opening a
#: sentence ("So I ..."), never as an intensifier ("so happy", "so much").
_SO_INTENSIFIER = re.compile(
    r"^so\s+(?:much|many|happy|proud|glad|good|great|cool|far|that|sorry|excited|"
    r"nice|fun|true|awesome|amazing|lucky|cute|sweet|beautiful|hard|important|"
    r"thankful|grateful|special|exciting|inspiring|thoughtful|kind|long|often|"
    r"well|tough|bad|lovely|interesting|relaxing|peaceful|sad|tired)\b",
    re.I,
)
_WHY = re.compile(r"^\s*(?:why\b|how come\b|what (?:made|caused|led)\b|for what reason\b)", re.I)
#: A turn that asks for a cause ("What made you pick it?", "Why so shiny?"): the
#: next turn's answer is its cause.
_ASKS_WHY = re.compile(
    r"\b(?:why|how come|what (?:made|makes|gave|inspired|led|got|drew|motivated|prompted)"
    r"|what(?:'s| is) the reason|(?:any|a special|a particular) reason"
    r"|what is it about)\b[^.!?]*\?",
    re.I,
)


def is_why_question(query: str) -> bool:
    """A question asking for a cause ("Why ...", "How come ...", "What made ...")."""
    return _WHY.match(query) is not None


@dataclass(frozen=True)
class CausalClause:
    """One causal sentence split: the connective and its two sides (either may be "")."""

    cue: str
    cause: str
    effect: str


@dataclass(frozen=True)
class CausalLink:
    """One rule edge between two turns: ``src`` = effect turn, ``dst`` = cause turn."""

    src: str
    dst: str
    cue: str
    overlap: int


@dataclass(frozen=True)
class KinshipRelation:
    """``person`` is ``owner``'s ``relation`` ("Ana" is "caroline"'s "sister")."""

    person: str
    relation: str
    owner: str


def strip_speaker(text: str) -> str:
    """``text`` without a leading "Name: " speaker prefix."""
    return _SPEAKER_HEAD.sub("", text, count=1)


def causal_clauses(text: str) -> list[CausalClause]:
    """The causal splits of ``text``: one per sentence with a connective (its first)."""
    found: list[CausalClause] = []
    for raw in _SENTENCE.split(strip_speaker(text)):
        sentence = raw.strip()
        match = _CONNECTIVE.search(sentence)
        if match is None:
            continue
        cue = match.group(1).lower()
        head = sentence[: match.start()]
        before = head.strip(" ,;:-")
        after = sentence[match.end() :].strip(" ,;:-")
        if cue == "so":
            joined = not before or head.rstrip().endswith((",", ";"))
            if not joined or _SO_INTENSIFIER.match(sentence[match.start() :]):
                continue
        if cue in _CAUSE_NEXT:
            if not before:
                # "Because C, E": the cause runs to the first comma.
                cause, _, effect = after.partition(",")
                found.append(CausalClause(cue, cause.strip(), effect.strip()))
            else:
                found.append(CausalClause(cue, after, before))
        else:
            found.append(CausalClause(cue, before, after))
    return found


def _words(text: str) -> frozenset[str]:
    return frozenset(w for w in content_words(text) if len(w) > 2)


def causal_links(
    turns: Sequence[tuple[str, str]], *, lookback: int = 40, min_overlap: int = 2
) -> list[CausalLink]:
    """Rule ``because`` edges over ``turns`` (``(record_id, text)``, oldest first).

    For each causal clause of turn ``i``: the earlier turn (within ``lookback``)
    sharing the most content words with the cause clause becomes the cause of turn
    ``i`` (edge ``i -> j``); the one sharing the most with the effect clause becomes
    its effect (edge ``j -> i``). Ties go to the nearest turn; a side needs
    ``min_overlap`` shared words of three letters or more. A turn answering one
    that asks for a cause ("What made you pick it?", cue ``why?``) or opening with
    "because" is the previous turn's cause (edge ``i-1 -> i``); one opening with
    "that's why" is its effect (edge ``i -> i-1``). Each ``(src, dst)`` pair
    appears once, with the first cue that produced it."""
    words = [_words(strip_speaker(text)) for _, text in turns]
    seen: set[tuple[str, str]] = set()
    links: list[CausalLink] = []

    def add(src: str, dst: str, cue: str, overlap: int) -> None:
        if src != dst and (src, dst) not in seen:
            seen.add((src, dst))
            links.append(CausalLink(src, dst, cue, overlap))

    def best_earlier(i: int, clause: str) -> tuple[int, int] | None:
        want = _words(clause)
        if len(want) < min_overlap:
            return None
        best: tuple[int, int] | None = None
        for j in range(i - 1, max(-1, i - 1 - lookback), -1):
            shared = len(want & words[j])
            if shared >= min_overlap and (best is None or shared > best[1]):
                best = (j, shared)
        return best

    for i, (rid, text) in enumerate(turns):
        opening = strip_speaker(text).lstrip().lower()
        if i > 0 and _ASKS_WHY.search(strip_speaker(turns[i - 1][1])):
            add(turns[i - 1][0], rid, "why?", 0)
        if i > 0 and opening.startswith("because"):
            add(turns[i - 1][0], rid, "because", 0)
        elif i > 0 and opening.startswith(("that's why", "that is why")):
            add(rid, turns[i - 1][0], "that's why", 0)
        for clause in causal_clauses(text):
            cause = best_earlier(i, clause.cause)
            if cause is not None:
                add(rid, turns[cause[0]][0], clause.cue, cause[1])
            effect = best_earlier(i, clause.effect)
            if effect is not None:
                add(turns[effect[0]][0], rid, clause.cue, effect[1])
    return links


_RELATIONS = (
    "best friend",
    "boyfriend",
    "girlfriend",
    "grandmother",
    "grandfather",
    "co-worker",
    "coworker",
    "colleague",
    "roommate",
    "neighbour",
    "neighbor",
    "partner",
    "husband",
    "daughter",
    "brother",
    "sister",
    "mother",
    "father",
    "cousin",
    "nephew",
    "mentor",
    "friend",
    "manager",
    "teacher",
    "grandma",
    "grandpa",
    "niece",
    "uncle",
    "aunt",
    "wife",
    "boss",
    "mom",
    "mum",
    "dad",
    "son",
)
_REL = "|".join(re.escape(r) for r in _RELATIONS)
_NAME = r"[A-Z][a-z]{1,20}"
#: Capitalised words that open a clause rather than name a person.
_NOT_NAMES = frozenset(
    {"And", "But", "The", "She", "He", "They", "We", "It", "This", "That", "So", "I", "My"}
)
_MY_REL_NAME = re.compile(rf"\b[Mm]y (?P<rel>{_REL}),? (?P<name>{_NAME})\b")
_NAME_MY_REL = re.compile(rf"\b(?P<name>{_NAME}),? (?:is |was )?my (?P<rel>{_REL})\b")


def kinship_relations(text: str) -> list[KinshipRelation]:
    """The ``(person, relation, owner)`` triples ``text`` states, first seen first.

    "my sister Ana", "my boss, Ana", "Ana, my boss", "Ana is my mentor". The owner
    is the turn's speaker ("Name: ..." prefix, lowercased), else ``user``."""
    owner = speaker_of(text) or "user"
    body = strip_speaker(text)
    matches = sorted(
        (m for pattern in (_MY_REL_NAME, _NAME_MY_REL) for m in pattern.finditer(body)),
        key=lambda m: m.start(),
    )
    found: list[KinshipRelation] = []
    for match in matches:
        name = match["name"]
        if name in _NOT_NAMES or name.lower() == owner:
            continue
        item = KinshipRelation(name, match["rel"].lower().replace("-", ""), owner)
        if item not in found:
            found.append(item)
    return found
