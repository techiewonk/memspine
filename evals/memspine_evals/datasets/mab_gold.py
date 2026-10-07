"""Gold-fact mapping and the supersession-order metric for MemoryAgentBench Conflict_Resolution.

The FactConsolidation contexts are numbered fact lists in which a larger serial number is a
newer fact, and a newer fact about the same (subject, relation) pair overrides an older one.
The release gives each question only its answer aliases, not the facts that support it. This
module recovers them deterministically, without any model:

1. every fact is parsed into ``(serial, subject, relation, object)`` with a fixed table of the
   release's relation templates (the MQuAKE / counterfact surface forms, typo
   ``"univeristy"`` / ``"origianl"`` included, because the data spells them so);
2. for each (subject, relation) the fact with the largest serial is **current**; every other
   fact with that pair is **stale** (superseded);
3. a question's subjects are the parsed subjects that occur in its text (case-insensitive,
   longest first). Single-hop: the current fact of a question subject whose object matches an
   answer alias. Multi-hop: the shortest chain of current facts (up to ``MAX_HOPS``) from a
   question subject whose last object matches an alias.

``gold_turn_ids`` = the chain's current facts (so ``R_all@k`` needs every hop);
``meta["stale_turn_ids"]`` = the stale facts of every (subject, relation) on the chain. A
question the mapper cannot resolve keeps empty gold and ``meta["gold_mapping"] =
"unmapped"`` (R@k is then ``None``-free but excluded by the coverage machinery), and an
ambiguous one (several shortest chains) keeps the first in serial order and says
``"ambiguous"``.

**Supersession order** (the metric this benchmark lacked in the harness): for one query and
one ranked retrieval, consider each gold (current) fact whose stale versions appear in the
top-k. The query passes if every such current fact is retrieved *and* ranked above all of its
stale versions; it fails if a stale version outranks it or it is missing; it is ``None``
(not applicable) when no stale version was retrieved. The rate is the mean over applicable
queries; the applicable count is reported beside it, because a system that never retrieves a
stale fact is not "ordering" anything.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_HOPS",
    "RELATION_TEMPLATES",
    "Fact",
    "GoldMapping",
    "build_index_mapper",
    "map_gold",
    "parse_fact",
    "supersession_order",
    "supersession_order_rate",
]

MAX_HOPS = 4

#: relation -> template, ``{s}`` subject and ``{o}`` object. Tried in order; prefixed forms
#: ("The X of ...") first so that a subject containing " is " cannot swallow them.
RELATION_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("head_of_government", "The name of the current head of the {s} government is {o}"),
    ("head_of_state", "The name of the current head of state in {s} is {o}"),
    ("headquarters", "The headquarters of {s} is located in the city of {o}"),
    ("educated_at", "The univeristy where {s} was educated is {o}"),
    ("producer", "The company that produced {s} is {o}"),
    ("music_genre", "The type of music that {s} plays is {o}"),
    ("ceo", "The chief executive officer of {s} is {o}"),
    ("official_language", "The official language of {s} is {o}"),
    ("broadcaster", "The origianl broadcaster of {s} is {o}"),
    ("head_coach", "The head coach of {s} is {o}"),
    ("chairperson", "The chairperson of {s} is {o}"),
    ("director", "The director of {s} is {o}"),
    ("author", "The author of {s} is {o}"),
    ("capital", "The capital of {s} is {o}"),
    ("child", "{s}'s child is {o}"),
    ("citizen", "{s} is a citizen of {o}"),
    ("religion", "{s} is affiliated with the religion of {o}"),
    ("sport", "{s} is associated with the sport of {o}"),
    ("continent", "{s} is located in the continent of {o}"),
    ("spouse", "{s} is married to {o}"),
    ("employer", "{s} is employed by {o}"),
    ("famous_for", "{s} is famous for {o}"),
    ("founder", "{s} was founded by {o}"),
    ("creator", "{s} was created by {o}"),
    ("performer", "{s} was performed by {o}"),
    ("developer", "{s} was developed by {o}"),
    ("country_of_origin", "{s} was created in the country of {o}"),
    ("founded_in", "{s} was founded in the city of {o}"),
    ("birthplace", "{s} was born in the city of {o}"),
    ("deathplace", "{s} died in the city of {o}"),
    ("work_location", "{s} worked in the city of {o}"),
    ("language_spoken", "{s} speaks the language of {o}"),
    ("language_written", "{s} was written in the language of {o}"),
    ("position", "{s} plays the position of {o}"),
    ("field", "{s} works in the field of {o}"),
)


def _compile(template: str) -> re.Pattern[str]:
    body = re.escape(template).replace(r"\{s\}", "(?P<s>.+?)").replace(r"\{o\}", "(?P<o>.+?)")
    return re.compile(rf"^{body}\.?$")


_PATTERNS = tuple((rel, _compile(t)) for rel, t in RELATION_TEMPLATES)
#: fallbacks for office-holder forms outside the table ("The Prime Minister of Sweden is
#: ...", "The Tánaiste is ..."): the role becomes the relation.
_GENERIC_OF = re.compile(r"^The (?P<r>.+?) of (?P<s>.+?) is (?P<o>.+?)\.?$")
_GENERIC_ROLE = re.compile(r"^The (?P<s>[^.]+?) is (?P<o>.+?)\.?$")


@dataclass(frozen=True, slots=True)
class Fact:
    serial: int
    turn_id: str
    subject: str
    relation: str
    obj: str


@dataclass(frozen=True, slots=True)
class GoldMapping:
    status: str  # "mapped" | "ambiguous" | "unmapped"
    gold: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    hops: int = 0
    #: gold fact id -> the stale versions of that fact's own (subject, relation) group
    stale_by_gold: tuple[tuple[str, tuple[str, ...]], ...] = ()


def parse_fact(text: str) -> tuple[str, str, str] | None:
    """``(subject, relation, object)`` of one fact sentence, or None for an unknown form."""
    sentence = text.strip()
    for relation, pattern in _PATTERNS:
        m = pattern.match(sentence)
        if m:
            return m.group("s").strip(), relation, m.group("o").strip()
    m = _GENERIC_OF.match(sentence)
    if m:
        return m.group("s").strip(), f"role:{m.group('r').lower()}", m.group("o").strip()
    m = _GENERIC_ROLE.match(sentence)
    if m:
        return m.group("s").strip(), "role", m.group("o").strip()
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _matches_alias(obj: str, aliases: Iterable[str]) -> bool:
    o = _norm(obj).rstrip(".")
    for alias in aliases:
        a = _norm(alias).rstrip(".")
        if a and (a == o or a in o or (len(o) > 3 and o in a)):
            return True
    return False


class _Index:
    def __init__(self, facts: Sequence[Fact]) -> None:
        groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
        for fact in facts:
            groups[(_norm(fact.subject), fact.relation)].append(fact)
        self.current: dict[tuple[str, str], Fact] = {}
        self.stale: dict[tuple[str, str], tuple[str, ...]] = {}
        self.by_subject: dict[str, list[Fact]] = defaultdict(list)
        self.all_by_subject: dict[str, list[Fact]] = defaultdict(list)
        for key, group in groups.items():
            group.sort(key=lambda f: f.serial)
            self.current[key] = group[-1]
            self.stale[key] = tuple(f.turn_id for f in group[:-1])
            self.by_subject[key[0]].append(group[-1])
            self.all_by_subject[key[0]].extend(group)
        for table in (self.by_subject, self.all_by_subject):
            for listed in table.values():
                listed.sort(key=lambda f: -f.serial)  # newest first: prefer newer chains
        # longest subjects first, so "University of California, Berkeley" beats "Berkeley"
        self.subjects = sorted(self.by_subject, key=len, reverse=True)


def _question_subjects(question: str, index: _Index) -> list[str]:
    q = _norm(question)
    found: list[str] = []
    for subject in index.subjects:
        if len(subject) >= 3 and subject in q and not any(subject in f for f in found):
            found.append(subject)
    return found


def map_gold(
    facts: Sequence[Fact], question: str, aliases: Sequence[str], multi_hop: bool
) -> GoldMapping:
    """Recover the supporting (current) facts and their stale versions for one question."""
    return _map(_Index(facts), question, aliases, multi_hop)


def _map(index: _Index, question: str, aliases: Sequence[str], multi_hop: bool) -> GoldMapping:
    starts = _question_subjects(question, index)
    found = _search(index.by_subject, starts, aliases, multi_hop)
    status = "mapped"
    if found is None:
        # The release is not always self-consistent: another question's edit can supersede
        # a fact this question's answer still needs (e.g. two capitals of one country). A
        # chain over *all* versions is then the answer's support; it is kept as gold for
        # R@k, flagged, and its non-current hops are left out of the order metric.
        found = _search(index.all_by_subject, starts, aliases, multi_hop)
        status = "inconsistent"
    if found is None:
        return GoldMapping(status="unmapped")
    chain, n_hits = found
    if status == "mapped" and n_hits > 1:
        status = "ambiguous"
    groups = tuple(
        (f.turn_id, index.stale.get(key, ()))
        for f in chain
        if index.current.get(key := (_norm(f.subject), f.relation)) is f
    )
    return GoldMapping(
        status=status,
        gold=tuple(f.turn_id for f in chain),
        stale=tuple(t for _, group in groups for t in group),
        hops=len(chain),
        stale_by_gold=groups,
    )


def _search(
    by_subject: Mapping[str, Sequence[Fact]],
    starts: Sequence[str],
    aliases: Sequence[str],
    multi_hop: bool,
) -> tuple[tuple[Fact, ...], int] | None:
    """Shortest chain from a question subject whose last object matches an alias."""
    max_hops = MAX_HOPS if multi_hop else 1
    frontier: list[tuple[Fact, ...]] = [(fact,) for s in starts for fact in by_subject.get(s, [])]
    for hop in range(1, max_hops + 1):
        hits = [p for p in frontier if _matches_alias(p[-1].obj, aliases)]
        if hits:
            return hits[0], len(hits)
        if hop == max_hops:
            break
        nxt: list[tuple[Fact, ...]] = []
        for path in frontier:
            seen = {f.turn_id for f in path}
            for fact in by_subject.get(_norm(path[-1].obj), []):
                if fact.turn_id not in seen:
                    nxt.append((*path, fact))
        frontier = nxt[:20000]  # bound the search; real chains are found well inside it
    return None


def supersession_order(
    retrieved_ids: Sequence[str],
    gold_ids: Sequence[str],
    stale_by_gold: Mapping[str, Sequence[str]],
    k: int | None = None,
) -> bool | None:
    """True if every gold fact whose stale versions were retrieved outranks all of them.

    ``retrieved_ids`` is a ranking (best first); ``k`` truncates it. ``None`` when no stale
    version of any gold fact is in the (truncated) ranking: the query does not test order.
    """
    ranked = list(dict.fromkeys(retrieved_ids))[: k if k is not None else None]
    position = {tid: i for i, tid in enumerate(ranked)}
    applicable = False
    for gold in gold_ids:
        stale_positions = [position[s] for s in stale_by_gold.get(gold, ()) if s in position]
        if not stale_positions:
            continue
        applicable = True
        if gold not in position or position[gold] > min(stale_positions):
            return False
    return True if applicable else None


def supersession_order_rate(
    rows: Iterable[Mapping[str, Any]],
    query_meta: Mapping[str, Mapping[str, Any]],
    k: int | None = None,
) -> dict[str, float | int | None]:
    """Pool :func:`supersession_order` over result rows.

    ``rows`` are result-row dicts (``query_id``, ``retrieved_ids``); ``query_meta`` maps
    query id -> the adapter's ``Query.meta`` (with ``gold_facts`` and ``stale_by_gold``).
    Returns the rate over applicable queries and the counts behind it.
    """
    passed = applicable = total = 0
    for row in rows:
        meta = query_meta.get(str(row.get("query_id")))
        if not meta or not meta.get("gold_facts"):
            continue
        total += 1
        verdict = supersession_order(
            row.get("retrieved_ids") or (),
            meta["gold_facts"],
            meta.get("stale_by_gold") or {},
            k,
        )
        if verdict is None:
            continue
        applicable += 1
        passed += int(verdict)
    return {
        "supersession_order": round(passed / applicable, 4) if applicable else None,
        "n_applicable": applicable,
        "n_with_gold": total,
    }


def build_index_mapper(facts: Sequence[Fact]) -> Any:
    """A reusable mapper over one context (parsing and grouping done once)."""
    index = _Index(facts)

    def _mapper(question: str, aliases: Sequence[str], multi_hop: bool) -> GoldMapping:
        return _map(index, question, aliases, multi_hop)

    return _mapper
