"""E01: query-directed atomic assertions and the evidenced joins between them (pure).

The engine makes ONE bounded structured call per read over the evidence already in context and
gets subject-relation-object assertions, each with the exact source span, its modality and its
time as written. This module holds what needs no service:

* :func:`validate_assertions` keeps an assertion only when its span really is a substring of
  the numbered line it cites (whitespace, case and punctuation aside), so a paraphrase or an
  invented relation never reaches a join;
* :func:`join_chains` joins two assertions only through a RESOLVED ENTITY (the first one's
  object equals the second one's subject after exact normalisation) or an EVIDENCED RELATION
  (the first one's object names the second one's relation for the same subject, e.g.
  ``moved_from(X, home country)`` + ``home country(X, Sweden)``). There is no fuzzy matching,
  no similarity, no vocabulary of any dataset. An assertion that is negated, hypothetical,
  uncertain or conditional never joins;
* :func:`render_chains` writes the short derived block, every link quoting its span and naming
  where it came from.

A chain is read-time only: nothing here stores anything. Raw turns stay authoritative.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from memspine.config import constants
from memspine.core.query_contract import QueryContract

__all__ = [
    "Assertion",
    "Chain",
    "fact_projection_on",
    "fold",
    "join_chains",
    "norm_entity",
    "relation_unresolved",
    "render_chains",
    "slot_name",
    "validate_assertions",
]

#: Modalities that never take part in a join: the assertion does not state a fact.
NON_JOINING = frozenset(
    {"negated", "hypothetical", "uncertain", "conditional", "question", "doubt", "denied"}
)
_PRONOUNS = frozenset(
    [
        "he",
        "she",
        "it",
        "they",
        "them",
        "him",
        "her",
        "his",
        "hers",
        "its",
        "their",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "this",
        "that",
        "these",
        "those",
        "someone",
        "somebody",
        "something",
        "anyone",
        "one",
    ]
)
_ARTICLES = re.compile(r"^(?:the|a|an)\s+", re.I)
_POSSESSIVE = re.compile(r"^(?:my|his|her|their|our|your|its|the)\s+", re.I)
_OWNER = re.compile(r"^[\w.-]+['’]s\s+", re.I)  # noqa: RUF001
_PUNCT = re.compile(r"[^\w\s]+", re.U)
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "why",
        "how",
        "with",
        "from",
        "by",
        "as",
        "that",
        "this",
        "it",
        "he",
        "she",
        "they",
        "his",
        "her",
        "their",
        "about",
    ]
)


def _squash(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", text.casefold().replace("_", " ")).split())


def fold(text: str) -> str:
    """Case, punctuation and underscores folded away, whitespace collapsed (exact matching)."""
    return _squash(text)


def fact_projection_on(policies: object) -> bool:
    """``memories.semantic.policies.fact_projection``: ``off`` (default) | ``on``.

    A bare YAML ``on`` / ``off`` arrives as a boolean and is read as such. Anything else
    raises ``ValueError`` (the engine turns it into a config error at start)."""
    value = policies.get("fact_projection", "off") if hasattr(policies, "get") else "off"
    if value is None or value is False or str(value).strip().casefold() in ("off", "false", "no"):
        return False
    if value is True or str(value).strip().casefold() in ("on", "true", "yes"):
        return True
    raise ValueError(
        f"unknown memories.semantic.policies.fact_projection {value!r} (valid: off, on)"
    )


def norm_entity(text: str) -> str:
    """An entity name for exact comparison: case, punctuation, a leading article and a
    possessive ``'s`` suffix removed. Pronouns and empty names normalise to ``""``."""
    text = _ARTICLES.sub("", text.strip())
    text = re.sub(r"['’]s\b", "", text)  # noqa: RUF001
    out = _squash(text)
    return "" if out in _PRONOUNS else out


def slot_name(text: str) -> str:
    """A relation or slot name for exact comparison (``home_country`` == "his home country"
    == "Sven's home country")."""
    text = _OWNER.sub("", _POSSESSIVE.sub("", text.strip()))
    return _squash(text)


@dataclass(frozen=True, slots=True)
class Assertion:
    subject: str
    relation: str
    object: str
    line: int  # 1-based index of the numbered evidence line
    span: str
    modality: str = "asserted"
    time: str = ""

    @property
    def joinable(self) -> bool:
        return self.modality.casefold() not in NON_JOINING


@dataclass(frozen=True, slots=True)
class Chain:
    first: Assertion
    second: Assertion
    via: str  # "entity" | "relation"
    bridge: str  # the shared entity or relation name
    score: int = 0


def validate_assertions(items: Iterable[object], lines: Sequence[str]) -> list[Assertion]:
    """The assertions whose cited line exists and contains their span.

    ``items`` are objects with the :class:`~memspine.prompts.models.AssertionOut` fields.
    Duplicates (same triple on the same line) are dropped."""
    shown = [_squash(line) for line in lines]
    out: list[Assertion] = []
    seen: set[tuple[str, str, str, int]] = set()
    for item in items:
        line = int(getattr(item, "line", 0) or 0)
        span = str(getattr(item, "span", "") or "").strip()
        if not 1 <= line <= len(lines) or not span:
            continue
        needle = _squash(span)
        if not needle or needle not in shown[line - 1]:
            continue
        a = Assertion(
            subject=str(getattr(item, "subject", "")).strip(),
            relation=str(getattr(item, "relation", "")).strip(),
            object=str(getattr(item, "object", "")).strip(),
            line=line,
            span=span,
            modality=str(getattr(item, "modality", "") or "asserted").strip().casefold(),
            time=str(getattr(item, "time", "") or "").strip(),
        )
        key = (norm_entity(a.subject), slot_name(a.relation), norm_entity(a.object), line)
        if key in seen or not (key[0] and key[1] and key[2]):
            continue
        seen.add(key)
        out.append(a)
    return out


def _focus_tokens(texts: Iterable[str]) -> set[str]:
    return {w for t in texts for w in _squash(t).split() if len(w) > 2 and w not in _STOP}


def _score(a: Assertion, b: Assertion, focus: set[str]) -> int:
    words = _focus_tokens([a.subject, a.relation, a.object, b.subject, b.relation, b.object])
    return len(words & focus)


def join_chains(
    assertions: Sequence[Assertion],
    *,
    focus: Iterable[str] = (),
    max_chains: int = constants.FACT_CHAIN_MAX_CHAINS,
) -> list[Chain]:
    """The two-link chains of ``assertions``, best first.

    Links: ``entity`` (first.object == second.subject, both resolved, not a pronoun) and
    ``relation`` (first.object names second.relation, same subject). With a non-empty
    ``focus`` (the question's words) a chain must share at least one word with it; chains are
    ranked by that overlap, then by line order. A chain never joins an assertion to itself or
    back to its own subject."""
    focus_set = _focus_tokens(focus)
    chains: list[Chain] = []
    seen: set[tuple[Assertion, Assertion]] = set()
    pool = [a for a in assertions if a.joinable]
    for a in pool:
        a_obj = norm_entity(a.object)
        a_slot = slot_name(a.object)
        for b in pool:
            if a is b:
                continue
            via = bridge = ""
            if (
                a_obj
                and a_obj == norm_entity(b.subject)
                and a_obj != norm_entity(a.subject)
                and norm_entity(b.object) != norm_entity(a.subject)
            ):
                via, bridge = "entity", a.object
            if (
                not via
                and a_slot
                and norm_entity(a.subject) == norm_entity(b.subject)
                and a_slot == slot_name(b.relation)
                and slot_name(a.relation) != slot_name(b.relation)
            ):
                via, bridge = "relation", b.relation
            if not via or (a, b) in seen:
                continue
            score = _score(a, b, focus_set)
            if focus_set and score == 0:
                continue
            seen.add((a, b))
            chains.append(Chain(a, b, via, bridge, score))
    chains.sort(key=lambda c: (-c.score, c.first.line, c.second.line))
    return chains[:max_chains]


def _link(a: Assertion, cite: Callable[[int], str]) -> str:
    when = f" [{a.time}]" if a.time else ""
    mode = f" ({a.modality})" if a.modality != "asserted" else ""
    return f'{a.subject} {a.relation} {a.object}{when}{mode} - "{a.span}" {cite(a.line)}'


def render_chains(chains: Sequence[Chain], cite: Callable[[int], str]) -> str:
    """The derived block: the marker, then per chain the joined reading and the two quoted
    links. Empty when there is no chain."""
    if not chains:
        return ""
    lines = [constants.FACT_CHAIN_MARKER]
    for i, c in enumerate(chains, 1):
        if c.via == "entity":
            reading = (
                f"{c.first.subject} {c.first.relation} {c.first.object}, "
                f"which {c.second.relation} {c.second.object}"
            )
        else:
            reading = f"{c.first.subject} {c.first.relation} {c.second.object} (its {c.bridge})"
        lines.append(f"{i}. {reading}")
        lines.append(f"   a) {_link(c.first, cite)}")
        lines.append(f"   b) {_link(c.second, cite)}")
    return "\n".join(lines)


def relation_unresolved(contract: QueryContract, lines: Sequence[str]) -> bool:
    """True when the question's relation is not stated next to its subject in any one line.

    The relation is the contract's content words (less the words of its answer type:
    "country" in "which country ..." is the slot asked for, not the relation); a line
    resolves it when it names a subject (if the contract has any) and at least half of those
    words (compared on a five-letter prefix, so inflections match). No relation (nothing to
    resolve) is not unresolved."""
    typed = {w[:5] for w in _squash(f"{contract.answer_type} {contract.subtype}").split()}
    words = [
        w[:5]
        for w in _squash(contract.relation).split()
        if len(w) > 2 and w not in _STOP and w[:5] not in typed
    ]
    if not words:
        return False
    need = (len(words) + 1) // 2
    subjects = [s for s in (norm_entity(x) for x in contract.subjects) if s]
    for line in lines:
        text = _squash(line)
        if subjects and not any(s in text for s in subjects):
            continue
        if sum(1 for w in words if w in text) >= need:
            return False
    return True
