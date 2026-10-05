"""GP-7 (#18): entity resolution for ``extract_graph`` (ADR-015 amendment, 2026-10-06).

With ``memories.semantic.policies.extract_graph.resolve`` set to ``rules`` or
``llm``, every entity name an extracted edge carries is resolved against the
entities the namespace already knows before its fact is written, so "Mel" and
"Melanie" become one entity node instead of two. The ladder, cheapest first:

1. **Exact**: the canonical name (NFKC, casefolded, whitespace collapsed) is
   already known. Nothing to do.
2. **Alias table**: an earlier sweep resolved this name; the decision is a
   MARKER event, so the table is rebuilt from the log.
3. **Candidates**: the known entities nearest the name by embedding cosine
   (top :data:`~memspine.config.constants.ENTITY_RESOLVE_TOP_K`); without an
   embedder, by shingle overlap.
4. **Entropy gate**: a short, low-entropy name ("Mel", "Jo") is too ambiguous for
   a string match and skips step 5.
5. **MinHash**: a high-entropy name whose character-shingle Jaccard with a
   candidate reaches :data:`~memspine.config.constants.ENTITY_RESOLVE_JACCARD`
   is that candidate (spelling variants) — unless the two names differ only by
   a generational suffix or a number ("Thompson II" / "III") or in one token by
   a trailing "s" ("William" / "Williams"): such a pair is a different entity
   as often as not and is left to step 6.
6. **LLM** (``resolve: llm`` only): the names still unresolved go to the
   ``resolve_entity@batch`` prompt in one call (chunks of
   :data:`~memspine.config.constants.ENTITY_RESOLVE_LLM_BATCH`), each with its
   candidates. An answer that is not one of the candidates means a new entity.

**Trust guard.** A merge gives the name's source reach into the target entity's
records, so a match is applied only when the source's trust is within
:data:`~memspine.config.constants.ENTITY_RESOLVE_TRUST_TOLERANCE` of the target's
(the most trusted record naming it). A wider gap is recorded as ``contested`` and
the name stays its own entity. Names new in the same sweep are not resolved
against each other.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from datasketch import MinHash

from memspine.config import constants
from memspine.memories.associative.entities import canonical_entity

__all__ = [
    "MERGE_METHODS",
    "Embed",
    "EntityResolver",
    "KnownEntity",
    "Resolution",
    "ResolveBatch",
    "high_entropy",
    "name_jaccard",
]

#: Embed texts -> vectors (the engine's embedder).
Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
#: One batched LLM call: ``[(name, [candidate names])]`` -> {1-based index: the
#: candidate the name refers to}; a missing or empty answer means a new entity.
ResolveBatch = Callable[[list[tuple[str, list[str]]]], Awaitable[dict[int, str]]]

#: Decision methods that merge a name into a known entity (folded into the alias table).
MERGE_METHODS = frozenset({"minhash", "llm"})


@dataclass(frozen=True)
class KnownEntity:
    """An entity the namespace already names."""

    canonical: str
    #: The name as records write it (most frequent spelling).
    display: str
    #: The trust of the most trusted live record naming it.
    trust: float


@dataclass(frozen=True)
class Resolution:
    """The outcome for one ``(name, source trust)`` request."""

    name: str
    #: The display name of the known entity the name now writes as; None keeps it.
    target: str | None
    #: exact | alias | minhash | llm | contested | new
    method: str
    #: For ``contested``: the entity the name matched but was not merged into.
    candidate: str | None = None


def _compact(canonical: str) -> str:
    return canonical.replace(" ", "")


def high_entropy(canonical: str) -> bool:
    """Graphiti's entropy gate: long or multi-word names with varied characters
    can be matched by string similarity; short low-entropy ones cannot."""
    tokens = canonical.split()
    if (
        len(canonical) < constants.ENTITY_RESOLVE_MIN_NAME_CHARS
        and len(tokens) < constants.ENTITY_RESOLVE_MIN_TOKENS
    ):
        return False
    chars = _compact(canonical)
    if not chars:
        return False
    counts = Counter(chars)
    entropy = -sum((n / len(chars)) * math.log2(n / len(chars)) for n in counts.values())
    return entropy >= constants.ENTITY_RESOLVE_MIN_ENTROPY


def _shingles(canonical: str) -> set[str]:
    chars = _compact(canonical)
    size = constants.ENTITY_RESOLVE_SHINGLE
    if len(chars) < size:
        return {chars} if chars else set()
    return {chars[i : i + size] for i in range(len(chars) - size + 1)}


def _minhash(canonical: str) -> MinHash:
    sketch = MinHash(num_perm=constants.ENTITY_RESOLVE_NUM_PERM)
    for shingle in sorted(_shingles(canonical)):
        sketch.update(shingle.encode("utf-8"))
    return sketch


def name_jaccard(a: str, b: str) -> float:
    """The MinHash estimate of the shingle Jaccard of two canonical names."""
    return float(_minhash(a).jaccard(_minhash(b)))


def _distinct_variants(a: str, b: str) -> bool:
    """Two canonical names a string match must not merge: their token sets
    differ only by generational suffixes or numbers, or by one token each that
    differ only by a trailing "s" (near surnames)."""
    ta = {t.strip(".,") for t in a.split()} - {""}
    tb = {t.strip(".,") for t in b.split()} - {""}
    only_a, only_b = ta - tb, tb - ta
    if not only_a and not only_b:
        return False
    if all(t in constants.ENTITY_RESOLVE_GENERATIONAL or t.isdigit() for t in only_a | only_b):
        return True
    if len(only_a) == 1 and len(only_b) == 1:
        [x], [y] = only_a, only_b
        return x + "s" == y or y + "s" == x
    return False


def _exact_jaccard(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _cosine(u: Sequence[float], v: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v, strict=False))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    return dot / (nu * nv) if nu and nv else 0.0


class EntityResolver:
    """Resolve one sweep's entity names against a namespace's known entities."""

    def __init__(
        self,
        known: dict[str, KnownEntity],
        aliases: dict[str, str],
        *,
        embed: Embed | None = None,
        llm: ResolveBatch | None = None,
    ) -> None:
        self._known = known
        self._aliases = aliases
        self._embed = embed
        self._llm = llm
        self._vectors: dict[str, list[float]] | None = None
        #: LLM calls made (tests and stats).
        self.llm_calls = 0

    async def resolve(self, requests: Sequence[tuple[str, float]]) -> list[Resolution]:
        """One :class:`Resolution` per ``(name, source trust)`` request, in order."""
        results: list[Resolution | None] = [None] * len(requests)
        #: canonical -> (display, candidates, request indexes) awaiting the LLM.
        pending: dict[str, tuple[str, list[KnownEntity], list[int]]] = {}
        for i, (name, trust) in enumerate(requests):
            canonical = canonical_entity(name)
            if not canonical or canonical in self._known:
                results[i] = Resolution(name, None, "exact")
                continue
            alias = self._aliases.get(canonical)
            if alias is not None and alias in self._known:
                results[i] = self._guard(name, trust, self._known[alias], "alias")
                continue
            if canonical in pending:
                pending[canonical][2].append(i)
                continue
            candidates = await self._candidates(canonical)
            if not candidates:
                results[i] = Resolution(name, None, "new")
                continue
            mergeable = [c for c in candidates if not _distinct_variants(canonical, c.canonical)]
            if high_entropy(canonical) and mergeable:
                best = max(
                    mergeable, key=lambda c: (name_jaccard(canonical, c.canonical), c.display)
                )
                if name_jaccard(canonical, best.canonical) >= constants.ENTITY_RESOLVE_JACCARD:
                    results[i] = self._guard(name, trust, best, "minhash")
                    continue
            if self._llm is None:
                results[i] = Resolution(name, None, "new")
                continue
            pending[canonical] = (name, candidates, [i])
        if pending:
            await self._ask_llm(requests, pending, results)
        return [
            r if r is not None else Resolution(req[0], None, "new")
            for r, req in zip(results, requests, strict=True)
        ]

    async def _ask_llm(
        self,
        requests: Sequence[tuple[str, float]],
        pending: dict[str, tuple[str, list[KnownEntity], list[int]]],
        results: list[Resolution | None],
    ) -> None:
        assert self._llm is not None
        items = list(pending.values())
        size = constants.ENTITY_RESOLVE_LLM_BATCH
        for start in range(0, len(items), size):
            chunk = items[start : start + size]
            self.llm_calls += 1
            answers = await self._llm(
                [(name, [c.display for c in cands]) for name, cands, _ in chunk]
            )
            for offset, (_name, cands, indexes) in enumerate(chunk, start=1):
                answer = canonical_entity(answers.get(offset, "") or "")
                target = next((c for c in cands if c.canonical == answer), None)
                for i in indexes:
                    name, trust = requests[i]
                    results[i] = (
                        Resolution(name, None, "new")
                        if target is None
                        else self._guard(name, trust, target, "llm")
                    )

    def _guard(self, name: str, trust: float, target: KnownEntity, method: str) -> Resolution:
        """The trust guard: merge only within the tolerance, else contested."""
        if abs(trust - target.trust) > constants.ENTITY_RESOLVE_TRUST_TOLERANCE:
            return Resolution(name, None, "contested", candidate=target.display)
        return Resolution(name, target.display, method)

    async def _candidates(self, canonical: str) -> list[KnownEntity]:
        """The known entities nearest ``canonical``, best first (top-k)."""
        known = sorted(self._known.values(), key=lambda k: k.canonical)
        if not known:
            return []
        k = constants.ENTITY_RESOLVE_TOP_K
        if self._embed is not None:
            try:
                if self._vectors is None:
                    vectors = await self._embed([e.display for e in known])
                    self._vectors = {e.canonical: v for e, v in zip(known, vectors, strict=True)}
                [query] = await self._embed([canonical])
                vectors_by = self._vectors
                return sorted(
                    known, key=lambda e: (-_cosine(query, vectors_by[e.canonical]), e.canonical)
                )[:k]
            except Exception:  # an enhancer, never a gate: fall back to shingles
                self._embed = None
        return sorted(known, key=lambda e: (-_exact_jaccard(canonical, e.canonical), e.canonical))[
            :k
        ]
