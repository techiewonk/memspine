"""RM-96: qualifier columns on stored edges and the Cypher compilation of a QualifierFilter.

The shared walk (:func:`~memspine.services.graph.qualifiers.qualified_hops`) parses the
qualifiers JSON of every fetched edge in Python. To filter inside the database instead
(fewer rows, no per-edge parse, fewer round trips), a Cypher adapter also stores each
edge's qualifiers as flat columns (derived on
``upsert_edge`` from the same parse the shared walk uses, so they hold exactly what
:meth:`QualifierFilter.matches` reads) and compiles the filter into the query's WHERE clause.

Columns (:data:`COLUMNS`): one per scalar qualifier the filter reads, the provenance
fields, the topical domain, the sensitivity label list and its check state, the grants
list, the numeric minima, and the valid-time interval pre-widened to whole precision
periods (RM-97, computed on the bound's own timezone by
:meth:`EdgeQualifiers._periods` and stored as epoch microseconds, so the database compares
instants exactly as Python compares aware datetimes). ``q_v`` is :data:`QUALIFIER_COLUMNS_VERSION`; an edge whose columns are missing or stale is
never traversed by a pushed-down walk (fail closed) until the adapter backfills it.

Compilation (:func:`compile_filter`): every :class:`QualifierFilter` field compiles; there
is no per-field fallback today (:func:`compile_filter` returns None only for a field this
module does not know, so a new filter field falls back to the shared walk until it is
compiled here). The traversable check (erased / erasure_pending never walked, RM-83) is
always part of the clause, even for an empty filter. Null handling follows the shared rules:
every allow-set/minimum conjunct requires a known value (unknown never passes), every
exclusion is ``NOT (known AND excluded)`` (unknown passes, except the sensitivity rule).
Empty sets compile to literals (an empty allow-set admits nothing; an empty exclusion
excludes nothing) so no empty list parameter needs a type.

Known divergence: a valid-time bound whose precision period ends past year 9999 makes the
shared walk raise (``datetime.replace`` overflow); its column end is clamped to the largest
representable instant instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from memspine.services.graph.qualifiers import (
    _EXCLUDE_FIELDS,
    _MODIFIER_FIELDS,
    _PROVENANCE_FIELDS,
    _SET_FIELDS,
    EdgeQualifiers,
    QualifierFilter,
    edge_qualifiers,
)

__all__ = [
    "COLUMNS",
    "QUALIFIER_COLUMNS_VERSION",
    "compile_filter",
    "qualifier_columns",
]

#: Bump when a column's derivation changes; stored edges with another version are re-derived.
QUALIFIER_COLUMNS_VERSION = 1

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_US = (datetime.max.replace(tzinfo=UTC) - _EPOCH) // timedelta(microseconds=1)

#: Allow-set filter field -> its column (scalar qualifiers and provenance fields).
_SET_COLUMNS: dict[str, str] = {
    **{field: f"q_{attr}" for field, attr in _SET_FIELDS.items()},
    **{field: f"q_prov_{attr}" for field, attr in _PROVENANCE_FIELDS.items()},
}
#: Exclusion filter field -> its column.
_EXCLUDE_COLUMNS: dict[str, str] = {field: f"q_{attr}" for field, attr in _EXCLUDE_FIELDS.items()}

#: column -> LadybugDB type. Neo4j stores the same names as relationship properties.
COLUMNS: dict[str, str] = {
    "q_v": "INT64",
    **dict.fromkeys(_SET_COLUMNS.values(), "STRING"),  # incl. q_erasure_state (RM-83)
    "q_topical_domain": "STRING",
    "q_vfrom_lo": "INT64",
    "q_vfrom_hi": "INT64",
    "q_vto_lo": "INT64",
    "q_vto_hi": "INT64",
    "q_vopen": "BOOLEAN",
    "q_vapprox": "BOOLEAN",
    "q_known": "INT64",
    "q_trust": "DOUBLE",
    "q_extraction_confidence": "DOUBLE",
    "q_support": "INT64",
    "q_sens": "STRING[]",
    "q_sens_checked": "BOOLEAN",
    "q_grants": "STRING[]",
}


def _us(value: datetime | None) -> int | None:
    if value is None:
        return None
    return (value - _EPOCH) // timedelta(microseconds=1)


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value.value) if isinstance(value, Enum) else str(value)


def _periods(q: EdgeQualifiers) -> tuple[int | None, int | None, int | None, int | None]:
    if q.valid_from is None:
        return None, None, None, None
    try:
        from_lo, from_hi, to_lo, to_hi = q._periods()
    except (OverflowError, ValueError):  # a period ending past year 9999 (module docstring)
        lo = _us(q.valid_from)
        to = _us(q.valid_to)
        return lo, _MAX_US, to, (None if to is None else _MAX_US)
    return _us(from_lo), _us(from_hi), _us(to_lo), _us(to_hi)


def qualifier_columns(properties: Mapping[str, object]) -> dict[str, Any]:
    """Every :data:`COLUMNS` value of an edge with ``properties`` (None = unknown)."""
    q = edge_qualifiers(properties)
    out: dict[str, Any] = dict.fromkeys(COLUMNS)
    out["q_v"] = QUALIFIER_COLUMNS_VERSION
    out["q_vopen"] = False
    out["q_vapprox"] = False
    if q is None:
        return out
    for field, attr in _SET_FIELDS.items():
        out[_SET_COLUMNS[field]] = _text(getattr(q, attr))
    if q.provenance is not None:
        for field, attr in _PROVENANCE_FIELDS.items():
            out[_SET_COLUMNS[field]] = _text(getattr(q.provenance, attr))
    out["q_topical_domain"] = q.topical_domain
    out["q_vfrom_lo"], out["q_vfrom_hi"], out["q_vto_lo"], out["q_vto_hi"] = _periods(q)
    out["q_vopen"] = bool(q.valid_open_ended)
    out["q_vapprox"] = bool(q.valid_approximate)
    out["q_known"] = _us(q.known_at)
    out["q_trust"] = q.trust
    out["q_extraction_confidence"] = q.extraction_confidence
    out["q_support"] = q.independent_support_count
    out["q_sens"] = None if q.sensitivity is None else sorted(q.sensitivity)
    out["q_sens_checked"] = q.sensitivity_checked
    out["q_grants"] = None if q.grants is None else list(q.grants)
    return out


#: Filter fields :func:`compile_filter` compiles (every field of QualifierFilter today).
_COMPILED_FIELDS = frozenset(
    {
        *_SET_COLUMNS,
        *_EXCLUDE_COLUMNS,
        *_MODIFIER_FIELDS,
        "valid_at",
        "valid_window",
        "known_as_of",
        "topical_domains",
        "sensitivities",
        "min_trust",
        "required_grants",
        "min_extraction_confidence",
        "min_independent_support",
        "exclude_topical_domains",
        "exclude_origin_types",
        "exclude_sensitivities",
    }
)


class _Params:
    def __init__(self, params: dict[str, Any], prefix: str) -> None:
        self._params = params
        self._prefix = prefix

    def add(self, value: Any) -> str:
        name = f"{self._prefix}{len(self._params)}"
        self._params[name] = value
        return f"${name}"


def _sorted_text(values: frozenset[Any]) -> list[str]:
    return sorted(str(_text(v)) for v in values)


def _any_label(p: _Params, column: str, labels: frozenset[Any]) -> str:
    """``column`` (a list) holds one of ``labels`` (non-empty); null when the list is null."""
    return "(" + " OR ".join(f"{p.add(label)} IN {column}" for label in _sorted_text(labels)) + ")"


def _domain_match(p: _Params, column: str, domains: frozenset[str]) -> str:
    """RM-91: ``column`` is one of ``domains`` or a descendant (segment boundary)."""
    parts = [
        f"{column} = {p.add(d)} OR {column} STARTS WITH {p.add(d + '/')}" for d in sorted(domains)
    ]
    return "(" + " OR ".join(parts) + ")"


def compile_filter(
    qualifier_filter: QualifierFilter, var: str, params: dict[str, Any], prefix: str = "qf"
) -> str | None:
    """A Cypher boolean over rel variable ``var`` that is true exactly when
    ``qualifier_filter.matches`` admits the edge (parameters added to ``params``).

    None when the filter sets a field this module does not compile (the caller then runs
    the shared walk). Always includes the column-version check and the traversable check."""
    set_fields = qualifier_filter.model_dump(exclude_none=True)
    if set(set_fields) - _COMPILED_FIELDS:
        return None
    p = _Params(params, prefix)
    c = f"{var}."
    out = [
        f"{c}q_v = {p.add(QUALIFIER_COLUMNS_VERSION)}",
        f"({c}q_erasure_state IS NULL OR NOT {c}q_erasure_state IN "
        f"{p.add(['erased', 'erasure_pending'])})",
    ]
    f = qualifier_filter
    if f.is_empty:
        return " AND ".join(out)
    # RM-86 exclusions: a known excluded value drops the edge; unknown passes.
    for field, column in _EXCLUDE_COLUMNS.items():
        excluded: frozenset[Any] | None = getattr(f, field)
        if excluded:
            out.append(
                f"NOT ({c}{column} IS NOT NULL AND {c}{column} IN {p.add(_sorted_text(excluded))})"
            )
    if f.exclude_topical_domains:
        dom = f"{c}q_topical_domain"
        out.append(
            f"NOT ({dom} IS NOT NULL AND {_domain_match(p, dom, f.exclude_topical_domains)})"
        )
    if f.exclude_origin_types:
        origin = f"{c}q_prov_origin_type"
        out.append(
            f"NOT ({origin} IS NOT NULL AND {origin} IN {p.add(_sorted_text(f.exclude_origin_types))})"
        )
    if f.exclude_sensitivities is not None:
        sens = f"{c}q_sens"
        if f.exclude_sensitivities:
            out.append(
                f"NOT ({sens} IS NOT NULL AND {_any_label(p, sens, f.exclude_sensitivities)})"
            )
        if f.unchecked_sensitivity == "fail":
            out.append(f"({c}q_sens_checked IS NOT NULL AND {c}q_sens_checked = true)")
    if f.valid_at is not None:
        at = p.add(_us(f.valid_at))
        out.append(
            f"({c}q_vfrom_lo IS NOT NULL AND {at} >= {c}q_vfrom_lo "
            f"AND ({c}q_vto_hi IS NULL OR {at} < {c}q_vto_hi) "
            f"AND ({c}q_vto_lo IS NOT NULL OR {c}q_vopen = true) "
            f"AND NOT ({c}q_vapprox = true AND ({at} < {c}q_vfrom_hi "
            f"OR ({c}q_vto_lo IS NOT NULL AND {at} >= {c}q_vto_lo))))"
        )
    if f.valid_window is not None:
        start, end = p.add(_us(f.valid_window[0])), p.add(_us(f.valid_window[1]))
        out.append(
            f"({c}q_vfrom_lo IS NOT NULL AND {c}q_vfrom_lo < {end} "
            f"AND ({c}q_vto_hi IS NULL OR {c}q_vto_hi > {start}) "
            f"AND ({c}q_vto_lo IS NOT NULL OR {c}q_vopen = true) "
            f"AND ({c}q_vapprox = false OR ({c}q_vfrom_hi < {end} AND {start} < {end} "
            f"AND ({c}q_vto_lo IS NULL OR ({c}q_vfrom_hi < {c}q_vto_lo "
            f"AND {start} < {c}q_vto_lo)))))"
        )
    if f.known_as_of is not None:
        out.append(f"({c}q_known IS NOT NULL AND {c}q_known <= {p.add(_us(f.known_as_of))})")
    for field, column in _SET_COLUMNS.items():
        allowed: frozenset[Any] | None = getattr(f, field)
        if allowed is None:
            continue
        if not allowed:
            out.append("false")
        else:
            out.append(
                f"({c}{column} IS NOT NULL AND {c}{column} IN {p.add(_sorted_text(allowed))})"
            )
    if f.topical_domains is not None:
        dom = f"{c}q_topical_domain"
        out.append(
            f"({dom} IS NOT NULL AND {_domain_match(p, dom, f.topical_domains)})"
            if f.topical_domains
            else "false"
        )
    if f.sensitivities is not None:
        sens = f"{c}q_sens"
        out.append(
            f"({sens} IS NOT NULL AND {_any_label(p, sens, f.sensitivities)})"
            if f.sensitivities
            else "false"
        )
    for value, column in (
        (f.min_trust, "q_trust"),
        (f.min_extraction_confidence, "q_extraction_confidence"),
        (f.min_independent_support, "q_support"),
    ):
        if value is not None:
            out.append(f"({c}{column} IS NOT NULL AND {c}{column} >= {p.add(value)})")
    if f.required_grants is not None:
        grants = f"{c}q_grants"
        parts = [f"{grants} IS NOT NULL"]
        parts += [f"{p.add(g)} IN {grants}" for g in sorted(f.required_grants)]
        out.append("(" + " AND ".join(parts) + ")")
    return " AND ".join(out)
