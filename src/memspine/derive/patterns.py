"""LIB-31: rule-based pattern observations from assertions and value histories (O4-05, opt-in L4).

Pure and model-free. An :class:`Observation` is a derived L4 candidate about a *pattern* (never a single fact):

* ``recurrence``: the same event kind at regular weekly, monthly or yearly intervals.
* ``frequency``: at least N distinct occurrences inside a trailing window.
* ``trend``: a numeric attribute rising or falling across three or more dated points.
* ``co_occurrence``: two kinds repeatedly appearing in the same session (or day).
* ``preference_stability``: the same preference restated across N or more sessions.

Decisions encoded:

* Lineage (O4-05): every observation lists the ids of all assertions that support it (``support_ids``); trust is
  ``<=`` the minimum trust of those parents (G1, :func:`memspine.schemas.durable.assertion.inherit_trust`).
* Independence (S4-2, S4-3): several assertions on the same day for the same kind are one occurrence, the rest are
  counted as ``restated``. A preference restated by one source family is a stability signal, not corroboration,
  so its confidence is capped (``restated_cap``) unless more than one independent source family backs it.
* Invalidation (ER-28 B): each observation has a ``group_key``; :func:`invalidate` names the observations whose
  support contains a changed (corrected, superseded, erased) assertion, so only those groups are recomputed.
* Statements are language-neutral structured ``fields``; rendering into a sentence happens elsewhere.

No I/O, no clock reads (``as_of`` is passed in), no model calls. Unwired: wiring is O4-05.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memspine.derive.histories import ValueHistory, value_history
from memspine.derive.integrate import CardinalityRegistry, record_id
from memspine.derive.resolve import normalise_name
from memspine.schemas.durable.assertion import AssertionEnvelope, inherit_trust

__all__ = [
    "Observation",
    "ObservationKind",
    "PatternConfig",
    "find_observations",
    "invalidate",
    "invalidated_groups",
    "trend_observations",
    "usefulness",
]

ObservationKind = Literal[
    "recurrence", "frequency", "trend", "co_occurrence", "preference_stability"
]
_ALL_KINDS: frozenset[str] = frozenset(
    {"recurrence", "frequency", "trend", "co_occurrence", "preference_stability"}
)
FieldValue = str | int | float | bool
_EPOCH = date(1970, 1, 1)
_MONTH_DAYS = 30.4375
_YEAR_DAYS = 365.25


# --------------------------------------------------------------------------- types


@dataclass(frozen=True)
class PatternConfig:
    """Minimum-evidence thresholds. Every kind needs at least two pieces of evidence by construction."""

    kinds: frozenset[str] = _ALL_KINDS
    min_recurrence: int = 3  # distinct dated occurrences
    weekly_tol_days: float = 1.0
    monthly_tol_days: float = 3.0
    yearly_tol_days: float = 7.0
    min_regular_fraction: float = 0.75  # share of gaps that equal exactly one period
    max_skipped_cycles: int = 1  # a gap may span up to this many missed cycles beyond one
    min_frequency: int = 4  # occurrences inside the window
    frequency_window_days: int = 30
    min_trend_points: int = 3
    trend_min_rel_change: float = 0.05  # |last - first| / max(|first|, 1)
    min_cooccur_units: int = 3
    min_cooccur_ratio: float = 0.6  # co-units / units of the rarer kind
    min_pref_sessions: int = 3
    restated_cap: float = 0.75  # confidence cap when one source family backs the pattern
    preference_kinds: frozenset[str] = frozenset({"preference", "like", "dislike"})

    def __post_init__(self) -> None:
        unknown = set(self.kinds) - _ALL_KINDS
        if unknown:
            raise ValueError(f"unknown observation kinds: {sorted(unknown)}")
        for name in (
            "min_recurrence",
            "min_frequency",
            "min_trend_points",
            "min_cooccur_units",
            "min_pref_sessions",
        ):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be at least 2")
        if self.frequency_window_days < 1:
            raise ValueError("frequency_window_days must be positive")
        if self.max_skipped_cycles < 0:
            raise ValueError("max_skipped_cycles must be >= 0")
        for name in ("min_regular_fraction", "min_cooccur_ratio", "restated_cap"):
            if not 0.0 < getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")


class Observation(BaseModel):
    """A derived L4 pattern candidate with lineage to every supporting assertion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    kind: ObservationKind
    group_key: str  # the unit of invalidation and recomputation
    statement: dict[str, FieldValue]  # language-neutral statement parts
    support_ids: tuple[str, ...]  # lineage: parents, sorted
    count: int = Field(ge=1)  # distinct occurrences, sessions or points that carry the pattern
    restated: int = Field(ge=0)  # same-day or same-source repeats (never corroboration, S4-3)
    independent_sources: int = Field(ge=0)
    first_seen: datetime
    last_seen: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    trust: float = Field(ge=0.0, le=1.0)  # <= min over parents (G1)
    members_fp: str  # fingerprint of kind + support ids, for idempotency


# --------------------------------------------------------------------------- helpers


@dataclass(frozen=True)
class _Occ:
    """One eligible assertion reduced to what the miners need."""

    rid: str
    at: datetime
    day: date
    unit: str  # session id, or the day when the write carries none
    unit_is_session: bool
    trust: float
    family: str
    restated: int
    value_key: str


def _start(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    if ev is not None and ev.start is not None:
        return ev.start.at
    return e.time.source_time or e.time.recorded_at


def _eligible(e: AssertionEnvelope) -> bool:
    return (
        e.status in ("active", "contested", "superseded")
        and e.assessment.modality == "asserted"
        and e.assessment.binding == "bound"
    )


def _subject_key(e: AssertionEnvelope) -> str:
    s = e.subject
    if s is None:
        return ""
    return s.entity_id or s.surface.strip().casefold()


def _predicate(e: AssertionEnvelope) -> str:
    return normalise_name(e.attribute or e.kind)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _family(e: AssertionEnvelope) -> str:
    fams = sorted({ev.source_family for ev in e.evidence if getattr(ev, "source_family", None)})
    return fams[0] if fams else (e.speaker or e.src_class)


def _occ(e: AssertionEnvelope) -> _Occ | None:
    at = _start(e)
    if at is None:
        return None
    at = at.astimezone(UTC)
    sid = e.provenance.session_id
    return _Occ(
        rid=record_id(e),
        at=at,
        day=at.date(),
        unit=sid if sid else at.date().isoformat(),
        unit_is_session=bool(sid),
        trust=e.assessment.source_trust,
        family=_family(e),
        restated=e.assessment.support.restated,
        value_key=str(e.value if e.value is not None else e.text).strip().casefold(),
    )


def _group_prefix(e: AssertionEnvelope) -> str:
    return f"{e.scope.tenant}|{e.scope.owner}|{_subject_key(e)}"


def _confidence(n: int, *, regularity: float = 1.0, capped: bool, cap: float) -> float:
    """Evidence-count confidence: ``n / (n + 2)`` scaled by regularity, in (0, 1); never above ``cap`` if capped."""
    base = (n / (n + 2.0)) * regularity
    base = min(base, cap) if capped else base
    return round(max(0.0, min(1.0, base)), 4)


def _build(
    kind: ObservationKind,
    group_key: str,
    fields_: Mapping[str, FieldValue],
    occs: list[_Occ],
    *,
    count: int,
    restated: int,
    confidence: float,
) -> Observation:
    support = tuple(sorted({o.rid for o in occs}))
    trust = inherit_trust([o.trust for o in occs], 1.0)
    fp = _digest(kind, group_key, *support)
    return Observation(
        observation_id=f"obs_{fp}",
        kind=kind,
        group_key=group_key,
        statement=dict(sorted(fields_.items())),
        support_ids=support,
        count=count,
        restated=restated,
        independent_sources=len({o.family for o in occs}),
        first_seen=min(o.at for o in occs),
        last_seen=max(o.at for o in occs),
        confidence=confidence,
        trust=round(trust, 6),
        members_fp=fp,
    )


def _per_day(occs: list[_Occ]) -> list[list[_Occ]]:
    """Occurrences collapsed to one bucket per day (several mentions of one day are one occurrence)."""
    by: dict[date, list[_Occ]] = defaultdict(list)
    for o in occs:
        by[o.day].append(o)
    return [sorted(by[d], key=lambda o: (o.at, o.rid)) for d in sorted(by)]


def _restated_of(buckets: list[list[_Occ]]) -> int:
    return sum(len(b) - 1 for b in buckets) + sum(o.restated for b in buckets for o in b)


# --------------------------------------------------------------------------- event groups


def _event_groups(
    assertions: Iterable[AssertionEnvelope], cfg: PatternConfig
) -> tuple[dict[tuple[str, str], list[_Occ]], dict[tuple[str, str], list[_Occ]]]:
    """(event groups, preference groups), keyed by (tenant|owner|subject, predicate)."""
    events: dict[tuple[str, str], list[_Occ]] = defaultdict(list)
    prefs: dict[tuple[str, str], list[_Occ]] = defaultdict(list)
    for e in assertions:
        if not _eligible(e):
            continue
        o = _occ(e)
        if o is None:
            continue
        pred = _predicate(e)
        key = (_group_prefix(e), pred)
        if (
            e.kind and normalise_name(e.kind) in cfg.preference_kinds
        ) or pred in cfg.preference_kinds:
            prefs[key].append(o)
        else:
            events[key].append(o)
    return events, prefs


def _match_period(gaps: list[float], cfg: PatternConfig) -> tuple[str, float] | None:
    """The first of weekly / monthly / yearly that the gaps fit, with its regularity in (0, 1]."""
    for name, period, tol in (
        ("weekly", 7.0, cfg.weekly_tol_days),
        ("monthly", _MONTH_DAYS, cfg.monthly_tol_days),
        ("yearly", _YEAR_DAYS, cfg.yearly_tol_days),
    ):
        exact = 0
        ok = True
        for g in gaps:
            k = round(g / period)
            if k < 1 or k > 1 + cfg.max_skipped_cycles or abs(g - k * period) > tol * k:
                ok = False
                break
            if k == 1:
                exact += 1
        if ok and exact / len(gaps) >= cfg.min_regular_fraction:
            return name, exact / len(gaps)
    return None


def _recurrence(key: tuple[str, str], occs: list[_Occ], cfg: PatternConfig) -> Observation | None:
    buckets = _per_day(occs)
    n = len(buckets)
    if n < cfg.min_recurrence:
        return None
    days = [(b[0].day - _EPOCH).days for b in buckets]
    gaps = [float(b - a) for a, b in pairwise(days)]
    m = _match_period(gaps, cfg)
    if m is None:
        return None
    period, regularity = m
    flat = [o for b in buckets for o in b]
    f: dict[str, FieldValue] = {
        "subject": key[0].split("|", 2)[2],
        "predicate": key[1],
        "period": period,
    }
    ds = [b[0].day for b in buckets]
    if period == "weekly" and len({d.weekday() for d in ds}) == 1:
        f["weekday"] = ds[0].weekday()  # Monday = 0
    elif period == "monthly" and len({d.day for d in ds}) == 1:
        f["day_of_month"] = ds[0].day
    elif period == "yearly" and len({(d.month, d.day) for d in ds}) == 1:
        f["month"], f["day_of_month"] = ds[0].month, ds[0].day
    return _build(
        "recurrence",
        f"{key[0]}|{key[1]}|recurrence",
        f,
        flat,
        count=n,
        restated=_restated_of(buckets),
        confidence=_confidence(n, regularity=regularity, capped=False, cap=1.0),
    )


def _frequency(
    key: tuple[str, str], occs: list[_Occ], cfg: PatternConfig, as_of: datetime | None
) -> Observation | None:
    buckets = _per_day(occs)
    end = as_of.astimezone(UTC).date() if as_of is not None else max(b[0].day for b in buckets)
    lo = end.toordinal() - cfg.frequency_window_days + 1
    inside = [b for b in buckets if lo <= b[0].day.toordinal() <= end.toordinal()]
    n = len(inside)
    if n < cfg.min_frequency:
        return None
    flat = [o for b in inside for o in b]
    f: dict[str, FieldValue] = {
        "subject": key[0].split("|", 2)[2],
        "predicate": key[1],
        "occurrences": n,
        "window_days": cfg.frequency_window_days,
    }
    return _build(
        "frequency",
        f"{key[0]}|{key[1]}|frequency",
        f,
        flat,
        count=n,
        restated=_restated_of(inside),
        confidence=_confidence(n, capped=False, cap=1.0),
    )


def _co_occurrence(
    prefix: str, preds: dict[str, list[_Occ]], cfg: PatternConfig
) -> list[Observation]:
    units: dict[str, dict[str, list[_Occ]]] = {p: defaultdict(list) for p in preds}
    for p, occs in preds.items():
        for o in occs:
            units[p][o.unit].append(o)
    out: list[Observation] = []
    names = sorted(preds)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            common = sorted(set(units[a]) & set(units[b]))
            rarer = min(len(units[a]), len(units[b]))
            if len(common) < cfg.min_cooccur_units or len(common) / rarer < cfg.min_cooccur_ratio:
                continue
            occs = [o for u in common for p in (a, b) for o in units[p][u]]
            by_session = all(o.unit_is_session for o in occs)
            f: dict[str, FieldValue] = {
                "subject": prefix.split("|", 2)[2],
                "predicate_a": a,
                "predicate_b": b,
                "unit": "session" if by_session else "day",
                "units": len(common),
            }
            ratio = len(common) / rarer
            same_day = sum(len(units[p][u]) - 1 for p in (a, b) for u in common)
            out.append(
                _build(
                    "co_occurrence",
                    f"{prefix}|{a}+{b}|co_occurrence",
                    f,
                    occs,
                    count=len(common),
                    restated=same_day,
                    confidence=_confidence(len(common), regularity=ratio, capped=False, cap=1.0),
                )
            )
    return out


def _preference(key: tuple[str, str], occs: list[_Occ], cfg: PatternConfig) -> list[Observation]:
    """A value is stable if >= N sessions state it and it is still the latest stated value for the predicate."""
    latest = max(occs, key=lambda o: (o.at, o.rid)).value_key
    by_value: dict[str, list[_Occ]] = defaultdict(list)
    for o in occs:
        by_value[o.value_key].append(o)
    if len(by_value) > 1 and latest not in by_value:  # pragma: no cover - defensive
        return []
    mine = by_value[latest]
    sessions = {o.unit for o in mine if o.unit_is_session}
    if len(sessions) < cfg.min_pref_sessions:
        return []
    mine = [o for o in mine if o.unit_is_session]
    n = len(sessions)
    independent = len({o.family for o in mine})
    f: dict[str, FieldValue] = {
        "subject": key[0].split("|", 2)[2],
        "predicate": key[1],
        "value": latest,
        "sessions": n,
    }
    return [
        _build(
            "preference_stability",
            f"{key[0]}|{key[1]}|{latest}|preference_stability",
            f,
            mine,
            count=n,
            restated=len(mine) - n + sum(o.restated for o in mine),
            confidence=_confidence(n, capped=independent <= 1, cap=cfg.restated_cap),
        )
    ]


# --------------------------------------------------------------------------- trends


def _number(text: str) -> float | None:
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def trend_observations(
    history: ValueHistory, cfg: PatternConfig | None = None
) -> list[Observation]:
    """Rising or falling numeric attributes: >= ``min_trend_points`` dated points, strictly monotone."""
    cfg = cfg or PatternConfig()
    if "trend" not in cfg.kinds:
        return []
    out: list[Observation] = []
    for line in history.lines:
        by_day: dict[date, tuple[datetime, float, str, float, int]] = {}
        for s in line.spans:
            if s.status == "contested" or s.effective_from is None:
                continue
            v = _number(s.value)
            if v is None:
                continue
            at = s.effective_from.astimezone(UTC)
            prev = by_day.get(at.date())
            cand = (at, v, s.record_id, s.trust, s.restated)
            if prev is None or (at, s.record_id) > (prev[0], prev[2]):
                by_day[at.date()] = cand
        pts = [by_day[d] for d in sorted(by_day)]
        if len(pts) < cfg.min_trend_points:
            continue
        deltas = [b[1] - a[1] for a, b in pairwise(pts)]
        if not (all(d > 0 for d in deltas) or all(d < 0 for d in deltas)):
            continue
        first, last = pts[0][1], pts[-1][1]
        if abs(last - first) < cfg.trend_min_rel_change * max(abs(first), 1.0):
            continue
        span_days = max((pts[-1][0] - pts[0][0]).days, 1)
        k = line.key
        prefix = f"{k.tenant}|{k.owner}|{k.subject or ''}"
        occs = [
            _Occ(rid, at, at.date(), at.date().isoformat(), False, tr, "", rs, str(v))
            for at, v, rid, tr, rs in pts
        ]
        f: dict[str, FieldValue] = {
            "subject": k.subject or "",
            "predicate": k.attribute,
            "direction": "rising" if deltas[0] > 0 else "falling",
            "points": len(pts),
            "first_value": first,
            "last_value": last,
            "slope_per_day": round((last - first) / span_days, 6),
        }
        out.append(
            _build(
                "trend",
                f"{prefix}|{k.attribute}|trend",
                f,
                occs,
                count=len(pts),
                restated=sum(o.restated for o in occs),
                confidence=_confidence(len(pts), capped=False, cap=1.0),
            )
        )
    return out


# --------------------------------------------------------------------------- entry points


def find_observations(
    assertions: Iterable[AssertionEnvelope],
    *,
    config: PatternConfig | None = None,
    registry: CardinalityRegistry | None = None,
    as_of: datetime | None = None,
) -> list[Observation]:
    """All pattern observations the rules support, sorted by ``(group_key, observation_id)``.

    ``as_of`` ends the frequency window (default: the last occurrence of each group). Deterministic: the result
    does not depend on the order of ``assertions``.
    """
    cfg = config or PatternConfig()
    items = list(assertions)
    events, prefs = _event_groups(items, cfg)
    out: list[Observation] = []
    for key in sorted(events):
        if "recurrence" in cfg.kinds and (o := _recurrence(key, events[key], cfg)):
            out.append(o)
        if "frequency" in cfg.kinds and (o := _frequency(key, events[key], cfg, as_of)):
            out.append(o)
    if "co_occurrence" in cfg.kinds:
        per_prefix: dict[str, dict[str, list[_Occ]]] = defaultdict(dict)
        for (prefix, pred), occs in events.items():
            per_prefix[prefix][pred] = occs
        for prefix in sorted(per_prefix):
            out.extend(_co_occurrence(prefix, per_prefix[prefix], cfg))
    if "preference_stability" in cfg.kinds:
        for key in sorted(prefs):
            out.extend(_preference(key, prefs[key], cfg))
    if "trend" in cfg.kinds:
        out.extend(trend_observations(value_history(items, registry), cfg))
    return sorted(out, key=lambda o: (o.group_key, o.observation_id))


def invalidate(
    observations: Iterable[Observation], changed_assertion_ids: Iterable[str]
) -> list[str]:
    """Ids of observations whose lineage contains a changed assertion (corrected, superseded or erased).

    Only these groups need recomputing; the rest keep their stored observation (ER-28 B).
    """
    changed = set(changed_assertion_ids)
    if not changed:
        return []
    return sorted({o.observation_id for o in observations if changed.intersection(o.support_ids)})


def invalidated_groups(
    observations: Iterable[Observation], changed_assertion_ids: Iterable[str]
) -> list[str]:
    """Group keys of the invalidated observations (the unit a worker recomputes)."""
    changed = set(changed_assertion_ids)
    return sorted({o.group_key for o in observations if changed.intersection(o.support_ids)})


def usefulness(shown: Iterable[str], helped: Iterable[str]) -> float:
    """Share of shown observation ids that the answer actually used (0.0 when none were shown)."""
    s = set(shown)
    return 0.0 if not s else len(s & set(helped)) / len(s)
