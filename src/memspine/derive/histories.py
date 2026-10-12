# ruff: noqa: E501
"""Near-duplicate merge and value histories for offline consolidation (LIB-20; O-13, O-14, M-13; S4-3, S5-2).

Pure and unwired (wiring is O4-04): no I/O, no clock, no model call. Embeddings, if used, arrive as
precomputed vectors. Depends on ``memspine.schemas`` and ``memspine.derive.integrate`` / ``resolve``.

Near-duplicates (O-13 B, S5)
----------------------------
``find_near_duplicates`` groups candidates by ``(tenant, owner, subject, attribute-or-kind)``, so a pair is never
compared across owners, subjects or attributes. Inside a group the records are visited in event-time order
(``ordering_key``); each record joins the first earlier cluster whose *canonical* (earliest) record it matches,
so clusters do not chain (A~B and B~C never pull in an A that is not similar to C). A pair is a near-duplicate when

* it is similar: normalised character-shingle similarity ``>= thresholds.text``, or (when vectors are given)
  cosine ``>= thresholds.vec``; and
* the code-only qualifier guard passes: same modality, same negation and hedging cues, same numbers (digits,
  thousands separators and small number words), same month names, and values that are equal after
  normalisation (typo-close non-numeric values are allowed; numeric values must be identical).

Guard failures are reported as blocked pairs (``scan_near_duplicates``), never merged. Only ``active`` or
``contested`` asserted/negated, bound records take part; tentative, superseded and hidden ones never do
(S4 runs before S5: a superseded value is not merged into a live one).

Merge plan (``merge_group``)
----------------------------
* Canonical = earliest occurrence (the same rule LIB-02 uses inside a value group). The richer text is reported
  as ``richer_text_id`` for a caller that wants to carry it; it does not change which record is canonical.
* The others become hidden (``verdict.op="merge"``). Lineage: one ``merge_member`` edge per dropped record with
  ``child`` = canonical (the aggregate) and ``parent`` = the dropped record, which is the direction the erasure
  planner reads (erasing a member re-runs the aggregate without it). The edge carries the member's family.
* Support (S4-3): walking members in event order, a member from a family already counted is ``restated`` (+1),
  a new family is ``independent`` (+1); members' own prior ``restated`` counts are added.
* Trust: the canonical is an original record, so its trust is unchanged (``trust``). ``derived_trust`` is the cap
  (min over all members) for any record *derived* from the group (for example a rewritten richer text), per the
  rule that a derived record never exceeds its weakest parent.

Value histories (O-14 B, M-13 D)
--------------------------------
``value_history`` replays the assertions with LIB-02's ``replay`` (so the closed/open state is independent of
arrival order) and projects the stored set into per-(owner, subject, attribute) value lines: ordered spans with
event intervals (closed when superseded or retired), contested spans, and tentative entries kept apart. It is a
projection: it adds no facts. Cardinality comes from the registry; an undeclared attribute is ``many`` (no
span is ever closed by a later value), so declare ``one`` attributes (for example residence) to get a chain.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Literal

from memspine.derive.integrate import (
    CardinalityRegistry,
    ordering_key,
    record_id,
    replay,
)
from memspine.derive.resolve import normalise_name
from memspine.schemas.durable.assertion import AssertionEnvelope, IntegrationVerdict
from memspine.schemas.durable.assessment import Support
from memspine.schemas.durable.lineage import LineageEdge

__all__ = [
    "ChangeEvent",
    "DupGroup",
    "DupPair",
    "DupScan",
    "DupThresholds",
    "Guard",
    "HistoryKey",
    "KeyHistory",
    "MergePlan",
    "TentativeEntry",
    "ValueHistory",
    "ValueSpan",
    "apply_merge",
    "check_guard",
    "cosine",
    "find_near_duplicates",
    "merge_group",
    "scan_near_duplicates",
    "shingle_similarity",
    "value_history",
]

Guard = Literal[
    "pass",
    "numbers_differ",
    "dates_differ",
    "negation_differs",
    "modality_differs",
    "value_differs",
]
TextSim = Callable[[str, str], float]
VecSim = Callable[[str, str], float] | Mapping[str, Sequence[float]]


# --------------------------------------------------------------------------- similarity


def shingle_similarity(a: str, b: str, *, k: int = 3) -> float:
    """Dice coefficient over character ``k``-shingles of the normalised texts (0..1; typo tolerant)."""
    na, nb = normalise_name(a), normalise_name(b)
    if na == nb:
        return 1.0 if na else 0.0
    sa, sb = _shingles(na, k), _shingles(nb, k)
    if not sa or not sb:
        return 0.0
    return 2.0 * sum((sa & sb).values()) / (sum(sa.values()) + sum(sb.values()))


def _shingles(text: str, k: int) -> Counter[str]:
    padded = f" {text} "
    if len(padded) <= k:
        return Counter([padded])
    return Counter(padded[i : i + k] for i in range(len(padded) - k + 1))


def cosine(u: Sequence[float], v: Sequence[float]) -> float:
    """Cosine of two vectors; a zero vector gives 0.0. Length mismatch is a caller bug (ValueError)."""
    if len(u) != len(v):
        raise ValueError(f"vector length mismatch: {len(u)} vs {len(v)}")
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(u, v, strict=True)) / (nu * nv)


# --------------------------------------------------------------------------- qualifier guard

_NUM_WORDS = {
    w: str(i)
    for i, w in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        ]
    )
}
_NUM_WORDS.update(
    {
        "thirty": "30",
        "forty": "40",
        "fifty": "50",
        "sixty": "60",
        "seventy": "70",
        "eighty": "80",
        "ninety": "90",
        "hundred": "100",
        "thousand": "1000",
        "million": "1000000",
    }
)
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_WORD = re.compile(r"[^\W\d_]+")
_MONTHS = {
    m: m[:3]
    for m in [
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
}
_MONTHS.update({m[:3]: m[:3] for m in list(_MONTHS)})
_MONTHS.update({"sept": "sep"})
_NEGATION = frozenset(
    [
        "not",
        "no",
        "never",
        "none",
        "nobody",
        "nothing",
        "neither",
        "nor",
        "without",
        "cannot",
        "cant",
        "dont",
        "doesnt",
        "didnt",
        "isnt",
        "wasnt",
        "arent",
        "werent",
        "wont",
        "wouldnt",
        "shouldnt",
        "hasnt",
        "havent",
        "hadnt",
        "niet",
        "geen",
        "nooit",
    ]
)
_HEDGE = frozenset(
    [
        "maybe",
        "might",
        "perhaps",
        "possibly",
        "probably",
        "planning",
        "plans",
        "plan",
        "considering",
        "wants",
        "hope",
        "hopes",
        "if",
        "would",
        "could",
        "should",
        "supposedly",
        "allegedly",
    ]
)


def _canon_number(raw: str) -> str:
    s = raw.replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.lstrip("0") or "0"


def _numbers(text: str) -> Counter[str]:
    low = text.casefold()
    out: Counter[str] = Counter(_canon_number(m) for m in _NUMBER.findall(low))
    for w in _WORD.findall(low):
        if w in _NUM_WORDS:
            out[_NUM_WORDS[w]] += 1
    return out


def _months(text: str) -> frozenset[str]:
    return frozenset(_MONTHS[w] for w in _WORD.findall(text.casefold()) if w in _MONTHS)


def _words(text: str) -> frozenset[str]:
    return frozenset(normalise_name(text).split())


def _scalar_text(e: AssertionEnvelope) -> str | None:
    return None if e.value is None else str(e.value).strip()


def _values_agree(a: AssertionEnvelope, b: AssertionEnvelope, typo_ratio: float) -> bool:
    va, vb = _scalar_text(a), _scalar_text(b)
    if va is None or vb is None:
        return True
    na, nb = _numbers(va), _numbers(vb)
    if na or nb:
        return na == nb and normalise_name(re.sub(r"[\d.,]+", " ", va)) == normalise_name(
            re.sub(r"[\d.,]+", " ", vb)
        )
    xa, xb = normalise_name(va), normalise_name(vb)
    return xa == xb or SequenceMatcher(None, xa, xb).ratio() >= typo_ratio


def check_guard(a: AssertionEnvelope, b: AssertionEnvelope, *, value_typo: float = 0.88) -> Guard:
    """Code-only qualifier guard (O-13): ``"pass"`` or the first reason the two must not be merged."""
    if a.assessment.modality != b.assessment.modality:
        return "modality_differs"
    ta, tb = f"{a.text} {_scalar_text(a) or ''}", f"{b.text} {_scalar_text(b) or ''}"
    wa, wb = _words(a.text), _words(b.text)
    if bool(wa & _NEGATION) != bool(wb & _NEGATION):
        return "negation_differs"
    if bool(wa & _HEDGE) != bool(wb & _HEDGE):
        return "modality_differs"
    if _numbers(a.text) != _numbers(b.text):
        return "numbers_differ"
    if _months(ta) != _months(tb):
        return "dates_differ"
    if not _values_agree(a, b, value_typo):
        return "value_differs"
    return "pass"


# --------------------------------------------------------------------------- near-duplicate detection


@dataclass(frozen=True)
class DupThresholds:
    """Thresholds (calibrate per embedding model; RV17). ``text`` is the shingle Dice, ``vec`` the cosine."""

    text: float = 0.80
    vec: float = 0.92
    value_typo: float = 0.88

    def __post_init__(self) -> None:
        for name in ("text", "vec", "value_typo"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"threshold {name} must be in [0, 1]")


@dataclass(frozen=True)
class DupPair:
    """Mirrors ``DedupPair`` (keep = canonical, drop = member), plus the text score."""

    keep_id: str
    drop_id: str
    text_score: float
    cosine: float | None
    guard: Guard


@dataclass(frozen=True)
class DupGroup:
    """A cluster of two or more near-duplicates; ``records`` is in event order, canonical first."""

    key: tuple[str, str, str | None, str]  # (tenant, owner, subject, attribute-or-kind)
    records: tuple[AssertionEnvelope, ...]
    pairs: tuple[DupPair, ...]

    @property
    def canonical(self) -> AssertionEnvelope:
        return self.records[0]

    @property
    def member_ids(self) -> tuple[str, ...]:
        return tuple(record_id(e) for e in self.records[1:])


@dataclass(frozen=True)
class DupScan:
    groups: tuple[DupGroup, ...]
    blocked: tuple[DupPair, ...]  # similar but stopped by the guard (``blocked_by_guard``)


def _group_key(e: AssertionEnvelope) -> tuple[str, str, str | None, str]:
    s = e.subject
    subj = None if s is None else (s.entity_id or s.surface.strip().casefold())
    return (e.scope.tenant, e.scope.owner, subj, normalise_name(e.attribute or e.kind))


def _eligible(e: AssertionEnvelope) -> bool:
    a = e.assessment
    return (
        e.status in ("active", "contested")
        and a.modality in ("asserted", "negated")
        and a.binding == "bound"
    )


def scan_near_duplicates(
    records: Iterable[AssertionEnvelope],
    *,
    text_sim: TextSim | None = None,
    vec_sim: VecSim | None = None,
    thresholds: DupThresholds | None = None,
) -> DupScan:
    """Find near-duplicate clusters and the pairs the guard blocked (see module docstring)."""
    th = thresholds or DupThresholds()
    tsim: TextSim = text_sim or shingle_similarity
    by_key: dict[tuple[str, str, str | None, str], list[AssertionEnvelope]] = {}
    seen: set[str] = set()
    for e in records:
        rid = record_id(e)
        if rid in seen or not _eligible(e):
            continue
        seen.add(rid)
        by_key.setdefault(_group_key(e), []).append(e)

    def vec(a: AssertionEnvelope, b: AssertionEnvelope) -> float | None:
        if vec_sim is None:
            return None
        ia, ib = record_id(a), record_id(b)
        if callable(vec_sim):
            return float(vec_sim(ia, ib))
        va, vb = vec_sim.get(ia), vec_sim.get(ib)
        return None if va is None or vb is None else cosine(va, vb)

    groups: list[DupGroup] = []
    blocked: list[DupPair] = []
    for key in sorted(by_key, key=lambda k: tuple("" if x is None else x for x in k)):
        recs = sorted(by_key[key], key=ordering_key)
        clusters: list[tuple[list[AssertionEnvelope], list[DupPair]]] = []
        for e in recs:
            placed = False
            for members, pairs in clusters:
                canon = members[0]
                ts = float(tsim(canon.text, e.text))
                cs = vec(canon, e)
                if not (ts >= th.text or (cs is not None and cs >= th.vec)):
                    continue
                g = check_guard(canon, e, value_typo=th.value_typo)
                pair = DupPair(record_id(canon), record_id(e), ts, cs, g)
                if g == "pass":
                    members.append(e)
                    pairs.append(pair)
                    placed = True
                    break
                blocked.append(pair)
            if not placed:
                clusters.append(([e], []))
        for members, pairs in clusters:
            if len(members) > 1:
                groups.append(DupGroup(key, tuple(members), tuple(pairs)))
    return DupScan(tuple(groups), tuple(blocked))


def find_near_duplicates(
    records: Iterable[AssertionEnvelope],
    *,
    text_sim: TextSim | None = None,
    vec_sim: VecSim | None = None,
    thresholds: DupThresholds | None = None,
) -> list[DupGroup]:
    """Near-duplicate clusters (two or more records). ``text_sim`` defaults to :func:`shingle_similarity`;
    ``vec_sim`` is a precomputed ``record_id -> vector`` mapping or a ``(id, id) -> cosine`` callable."""
    return list(
        scan_near_duplicates(
            records, text_sim=text_sim, vec_sim=vec_sim, thresholds=thresholds
        ).groups
    )


# --------------------------------------------------------------------------- merge plan


@dataclass(frozen=True)
class MergePlan:
    canonical_id: str
    member_ids: tuple[str, ...]  # dropped records, hidden with verdict op "merge"
    edges: tuple[LineageEdge, ...]  # merge_member, child = canonical, parent = dropped record
    support: Support  # combined support for the canonical (S4-3)
    reinforcement: int  # number of repeats absorbed (R-16 boost input; bounded by the caller)
    restated_delta: int
    independent_delta: int
    trust: float  # canonical is an original record: unchanged
    derived_trust: float  # cap for a record derived from the group: min over all members
    richer_text_id: str  # longest text, for a caller that carries it
    pairs: tuple[DupPair, ...] = ()
    verdict: IntegrationVerdict = field(
        default_factory=lambda: IntegrationVerdict(op="merge", rung="near_duplicate")
    )


def _family_of(e: AssertionEnvelope) -> str:
    for ev in e.evidence:
        if ev.role == "support":
            return ev.source_family
    for ev in e.evidence:
        if ev.counts_as_evidence:
            return ev.source_family
    return e.src_class


def merge_group(group: DupGroup) -> MergePlan:
    """Plan the merge of one cluster (pure; see module docstring for the rules)."""
    if len(group.records) < 2:
        raise ValueError("a merge group needs at least two records")
    canon, *others = group.records
    cid = record_id(canon)
    known = {_family_of(canon), *canon.assessment.support.families}
    base_ind = max(canon.assessment.support.independent, len(known))
    restated = canon.assessment.support.restated
    r_delta = i_delta = 0
    edges: list[LineageEdge] = []
    for m in others:
        fam = _family_of(m)
        fams = {fam, *m.assessment.support.families}
        new = fams - known
        if fam in known:
            restated += 1
            r_delta += 1
        n_new = len(new)
        base_ind += n_new
        i_delta += n_new
        known |= fams
        restated += m.assessment.support.restated
        edges.append(
            LineageEdge(child=cid, parent=record_id(m), role="merge_member", source_family=fam)
        )
    fams_out = sorted(known) if len(known) == base_ind else []
    support = Support(independent=base_ind, restated=restated, families=fams_out)
    trusts = [e.assessment.source_trust for e in group.records]
    richer = max(group.records, key=lambda e: (len(e.text), -group.records.index(e)))
    return MergePlan(
        canonical_id=cid,
        member_ids=tuple(record_id(m) for m in others),
        edges=tuple(edges),
        support=support,
        reinforcement=len(others),
        restated_delta=r_delta,
        independent_delta=i_delta,
        trust=canon.assessment.source_trust,
        derived_trust=min(trusts),
        richer_text_id=record_id(richer),
        pairs=group.pairs,
    )


def apply_merge(plan: MergePlan, records: Iterable[AssertionEnvelope]) -> list[AssertionEnvelope]:
    """Return ``records`` with the plan applied: canonical gets the combined support, members are hidden."""
    drop = set(plan.member_ids)
    out: list[AssertionEnvelope] = []
    for e in records:
        rid = record_id(e)
        if rid == plan.canonical_id:
            out.append(
                e.model_copy(
                    update={"assessment": e.assessment.model_copy(update={"support": plan.support})}
                )
            )
        elif rid in drop:
            out.append(e.model_copy(update={"status": "hidden", "verdict": plan.verdict}))
        else:
            out.append(e)
    return out


# --------------------------------------------------------------------------- value histories


@dataclass(frozen=True)
class HistoryKey:
    tenant: str
    owner: str
    subject: str | None
    attribute: str


@dataclass(frozen=True)
class ValueSpan:
    value: str
    effective_from: datetime | None
    effective_to: datetime | None  # None = still open
    record_id: str
    status: str  # "active", "contested" or "superseded"
    trust: float
    source_dates: tuple[datetime, ...] = ()  # canonical plus merged duplicates, ascending
    restated: int = 0

    def covers(self, t: datetime) -> bool:
        return (self.effective_from is None or self.effective_from <= t) and (
            self.effective_to is None or t < self.effective_to
        )


@dataclass(frozen=True)
class TentativeEntry:
    value: str
    record_id: str
    modality: str
    at: datetime | None


@dataclass(frozen=True)
class ChangeEvent:
    at: datetime
    kind: Literal["start", "end"]
    value: str
    record_id: str


@dataclass(frozen=True)
class KeyHistory:
    key: HistoryKey
    spans: tuple[ValueSpan, ...]  # ordered by effective_from, contested spans included
    tentative: tuple[TentativeEntry, ...] = ()

    @property
    def contested(self) -> tuple[ValueSpan, ...]:
        return tuple(s for s in self.spans if s.status == "contested")

    def value_at(self, t: datetime, *, include_contested: bool = False) -> tuple[ValueSpan, ...]:
        """Values holding at ``t`` (several only for a many-valued attribute)."""
        return tuple(
            s for s in self.spans if s.covers(t) and (include_contested or s.status != "contested")
        )

    def current(self) -> tuple[ValueSpan, ...]:
        """Open, uncontested values (the live value or values)."""
        return tuple(s for s in self.spans if s.effective_to is None and s.status == "active")

    def open_contests(self) -> tuple[ValueSpan, ...]:
        return tuple(s for s in self.contested if s.effective_to is None)

    def changes_between(self, a: datetime, b: datetime) -> tuple[ChangeEvent, ...]:
        """Starts and ends of uncontested spans with ``a < at <= b``, in time order."""
        ev: list[ChangeEvent] = []
        for s in self.spans:
            if s.status == "contested":
                continue
            if s.effective_from is not None and a < s.effective_from <= b:
                ev.append(ChangeEvent(s.effective_from, "start", s.value, s.record_id))
            if s.effective_to is not None and a < s.effective_to <= b:
                ev.append(ChangeEvent(s.effective_to, "end", s.value, s.record_id))
        return tuple(sorted(ev, key=lambda c: (c.at, 0 if c.kind == "end" else 1, c.record_id)))


@dataclass(frozen=True)
class ValueHistory:
    """All value lines of an assertion set. With a single key, the query helpers need no ``key``."""

    lines: tuple[KeyHistory, ...]

    def keys(self) -> tuple[HistoryKey, ...]:
        return tuple(h.key for h in self.lines)

    def line(self, key: HistoryKey | None = None) -> KeyHistory:
        if key is None:
            if len(self.lines) != 1:
                raise ValueError(f"{len(self.lines)} value lines; pass a key")
            return self.lines[0]
        for h in self.lines:
            if h.key == key:
                return h
        return KeyHistory(key, ())

    def value_at(
        self, t: datetime, key: HistoryKey | None = None, *, include_contested: bool = False
    ) -> tuple[ValueSpan, ...]:
        return self.line(key).value_at(t, include_contested=include_contested)

    def changes_between(
        self, a: datetime, b: datetime, key: HistoryKey | None = None
    ) -> tuple[ChangeEvent, ...]:
        return self.line(key).changes_between(a, b)

    def current(self, key: HistoryKey | None = None) -> tuple[ValueSpan, ...]:
        return self.line(key).current()


def _start(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    if ev is not None and ev.start is not None:
        return ev.start.at
    return e.time.source_time or e.time.recorded_at


def _end(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    return ev.end.at if ev is not None and ev.end is not None else None


def _value_text(e: AssertionEnvelope) -> str:
    return str(e.value if e.value is not None else e.text)


def _vkey(e: AssertionEnvelope) -> str:
    return _value_text(e).strip().casefold()


def value_history(
    assertions: Iterable[AssertionEnvelope], registry: CardinalityRegistry | None = None
) -> ValueHistory:
    """Build the per-(owner, subject, attribute) value lines via LIB-02's ``replay``."""
    reg = registry or CardinalityRegistry()
    stored = replay(list(assertions), reg).records
    buckets: dict[tuple[str, str, str | None, str], list[AssertionEnvelope]] = {}
    for e in stored:
        buckets.setdefault(_group_key(e), []).append(e)
    lines: list[KeyHistory] = []
    for gk in sorted(buckets, key=lambda k: tuple("" if x is None else x for x in k)):
        recs = buckets[gk]
        spans: list[tuple[tuple, ValueSpan]] = []
        merged: list[AssertionEnvelope] = []
        tentative: list[TentativeEntry] = []
        for e in recs:
            if e.status == "hidden":
                if e.verdict is not None and e.verdict.op == "merge":
                    merged.append(e)
                continue
            if e.status == "tentative":
                tentative.append(
                    TentativeEntry(_value_text(e), record_id(e), e.assessment.modality, _start(e))
                )
                continue
            if e.assessment.modality != "asserted":
                continue  # a retraction: visible as the end of the span it closed
            spans.append(
                (
                    ordering_key(e),
                    ValueSpan(
                        value=_value_text(e),
                        effective_from=_start(e),
                        effective_to=_end(e),
                        record_id=record_id(e),
                        status=e.status,
                        trust=e.assessment.source_trust,
                        source_dates=tuple(
                            d for d in (e.time.source_time or e.time.recorded_at,) if d
                        ),
                        restated=e.assessment.support.restated,
                    ),
                )
            )
        spans.sort(key=lambda p: p[0])
        ordered = [s for _, s in spans]
        for m in sorted(merged, key=ordering_key):  # attach merged duplicates' source dates
            d = m.time.source_time or m.time.recorded_at
            same = [i for i, s in enumerate(ordered) if s.value.strip().casefold() == _vkey(m)]
            if d is None or not same:
                continue
            idx = same[0]
            for i in same:
                f = ordered[i].effective_from
                if f is not None and f <= d:
                    idx = i
            s = ordered[idx]
            ordered[idx] = ValueSpan(
                s.value, s.effective_from, s.effective_to, s.record_id, s.status, s.trust,
                tuple(sorted({*s.source_dates, d})), s.restated,
            )  # fmt: skip
        tent = tuple(
            sorted(
                tentative,
                key=lambda t: (t.at is None, t.at.timestamp() if t.at else 0.0, t.record_id),
            )
        )
        lines.append(KeyHistory(HistoryKey(*gk), tuple(ordered), tent))
    return ValueHistory(tuple(lines))
