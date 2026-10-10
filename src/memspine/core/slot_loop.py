"""E02 (``read.agentic_mode: slot``): the pure parts of the missing-slot-driven read.

The engine owns the loop (LLM calls, searches, lookups); this module holds what needs no
service: the evidence view with turn handles, the repeat signature of an action, the
contract hint that seeds the slot, and the ``calculate`` action, which is CODE (never the
model): date differences and sums over values that are already resolved in the evidence.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from memspine.config import constants
from memspine.core.policies.assembly import estimate_tokens
from memspine.core.query_contract import QueryContract
from memspine.core.records import MemoryRecord
from memspine.core.temporal_resolve import resolve

__all__ = [
    "CalcError",
    "calculate",
    "slot_hint",
    "slot_view",
    "step_signature",
]


class CalcError(ValueError):
    """The calculate action could not be done from what it was given (fails safe)."""


def slot_view(
    records: Sequence[MemoryRecord],
    *,
    max_tokens: int = constants.AGENTIC_VIEW_TOKENS,
    line_chars: int = constants.AGENTIC_LINE_CHARS,
) -> tuple[str, dict[str, str]]:
    """Like :func:`~memspine.core.agentic.evidence_view`, each line led by a handle ``[T1]``,
    ``[T2]``...; returns ``(view, {handle: record id})`` for the lines shown. A handle is the
    only way ``neighbor_lookup`` names a turn, so the model can only point at evidence it saw."""
    lines: list[str] = []
    handles: dict[str, str] = {}
    used = 0
    for record in records:
        handle = f"T{len(lines) + 1}"
        line = f"[{handle}] " + " ".join(record.content.split())[:line_chars]
        cost = estimate_tokens(line)
        if lines and used + cost > max_tokens:
            break
        lines.append(line)
        handles[handle] = record.record_id
        used += cost
    return ("\n".join(lines) or "(none)"), handles


def slot_hint(contract: QueryContract) -> str:
    """The contract as one line the action step starts its slot from."""
    parts = [f"answer type: {contract.type_label}"]
    if contract.subjects:
        parts.append("about: " + ", ".join(contract.subjects))
    if contract.relation:
        parts.append(f"relation: {contract.relation}")
    if contract.time_scope:
        parts.append(f"time: {contract.time_scope}")
    parts.append(f"cardinality: {contract.cardinality}")
    return "; ".join(parts)


def step_signature(action: str, **args: Any) -> tuple[str, ...]:
    """What makes two actions the same one (a repeat): the action and its normalised
    arguments (case and whitespace folded); the slot and the why are not part of it."""
    norm = [" ".join(str(v).casefold().split()) for _, v in sorted(args.items()) if str(v).strip()]
    return (action, *norm)


# ------------------------------------------------------------------------------ calculate

_ISO = re.compile(r"^\s*(\d{4})-(\d{2})(?:-(\d{2}))?\s*$")
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_UNITS = {
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "month": "months",
    "months": "months",
    "year": "years",
    "years": "years",
}


def _day(text: str, anchor: str) -> date:
    m = _ISO.match(text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3] or 1))
        except ValueError as exc:
            raise CalcError(f"not a date: {text!r}") from exc
    if anchor:
        base = _ISO.match(anchor)
        if base and base[3]:
            found = resolve(text, date(int(base[1]), int(base[2]), int(base[3])))
            if len(found) == 1 and found[0].first == found[0].last:
                return found[0].first
    raise CalcError(f"not a resolved date: {text!r}")


def _number(text: str) -> float:
    m = _NUM.search(text)
    if not m:
        raise CalcError(f"not a number: {text!r}")
    return float(m[0].replace(",", ""))


def _months_between(a: date, b: date) -> int:
    months = (b.year - a.year) * 12 + (b.month - a.month)
    if months > 0 and b.day < a.day:
        months -= 1
    elif months < 0 and b.day > a.day:
        months += 1
    return months


def _add_months(d: date, months: int) -> date:
    year, month0 = divmod(d.year * 12 + (d.month - 1) + months, 12)
    month = month0 + 1
    following = date(year + (month == 12), month % 12 + 1, 1)
    return date(year, month, min(d.day, (following - timedelta(days=1)).day))


def _fmt(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:.4g}"


def calculate(
    op: str,
    arg_a: str,
    arg_b: str = "",
    amount: str = "",
    unit: str = "",
    anchor: str = "",
) -> str:
    """One calculation, done by code, as a short note the reader can cite.

    ``date_diff`` (``arg_a`` to ``arg_b``, in ``unit`` days / weeks / months / years, default
    days), ``date_add`` (``arg_a`` plus ``amount`` ``unit``; a negative amount subtracts),
    ``sum`` and ``difference`` over the numbers in ``arg_a`` and ``arg_b``. A date is
    ``YYYY-MM-DD`` (``YYYY-MM`` = the first of the month) or, with ``anchor`` (an ISO day), a
    relative phrase the H1 resolver reads. Anything else raises :class:`CalcError`."""
    name = op.strip().casefold().replace("-", "_").replace(" ", "_")
    name = {
        "days_between": "date_diff",
        "duration": "date_diff",
        "date_difference": "date_diff",
        "add_days": "date_add",
        "date_sub": "date_add",
        "add": "sum",
        "subtract": "difference",
        "minus": "difference",
    }.get(name, name)
    u = _UNITS.get(unit.strip().casefold(), "days") if unit.strip() else "days"
    if name == "date_diff":
        a, b = _day(arg_a, anchor), _day(arg_b, anchor)
        if u == "days":
            n = (b - a).days
            body = f"{abs(n)} days"
        elif u == "weeks":
            n = (b - a).days
            body = f"{abs(n) // 7} weeks and {abs(n) % 7} days"
        elif u == "months":
            n = _months_between(a, b)
            body = f"{abs(n)} months"
        else:
            n = _months_between(a, b)
            body = f"{abs(n) // 12} years and {abs(n) % 12} months"
        order = " (the second date is earlier)" if n < 0 else ""
        return f"from {a.isoformat()} to {b.isoformat()} = {body}{order}"
    if name == "date_add":
        a = _day(arg_a, anchor)
        k = int(_number(amount))
        if u == "days":
            out = a + timedelta(days=k)
        elif u == "weeks":
            out = a + timedelta(days=7 * k)
        elif u == "months":
            out = _add_months(a, k)
        else:
            out = _add_months(a, 12 * k)
        return f"{a.isoformat()} {k:+d} {u} = {out.isoformat()}"
    if name in ("sum", "difference"):
        x, y = _number(arg_a), _number(arg_b)
        value = x + y if name == "sum" else x - y
        sign = "+" if name == "sum" else "-"
        return f"{_fmt(x)} {sign} {_fmt(y)} = {_fmt(value)}"
    raise CalcError(f"unknown calculation {op!r}")
