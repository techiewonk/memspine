"""M02: a reversible evidence-reference normaliser for any dataset with turn-id evidence.

A benchmark's gold evidence is a list of raw strings (``"D1:2"``). Real files contain malformed
entries (``"D"``, ``"D:11:26"``, ``"D30:05"``, ``"D9:1 D4:4 D4:6"``) and ids that name no turn of the
conversation. The official labels are immutable: this module never rewrites them. It builds a
*view* next to them: for every raw string, the ids it resolves to, a status, and how each token was
resolved, so that

* ``ok``         every token is a known turn id as written (``;`` / ``,`` separated lists are the
                 dataset's own list convention and count as written);
* ``repaired``   at least one token needed a mechanical repair (whitespace-joined list, a stray
                 colon, leading zeros, letter case) and every token resolves;
* ``unresolved`` at least one token names no turn of the same conversation, even after repair.

A repair is accepted only if the repaired id is a known turn id of the same conversation
(``known_ids``). Nothing is guessed: ``"D"`` and an id of a turn that does not exist stay
unresolved. The view is reversible: ``EvidenceRef.raw`` is the untouched string and each
``EvidenceToken`` keeps the raw token it came from.

Official score vs adjudicated diagnostic: ``recall_pair`` reports the official all-gold recall
(raw tokens, split the loaders' way, an unresolvable token can never be hit) next to a diagnostic
over the resolved ids only. The diagnostic is a separate column and never replaces the official
number; ``None`` when nothing resolves.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "STATUSES",
    "EvidenceRef",
    "EvidenceToken",
    "EvidenceView",
    "normalise_evidence",
    "normalise_ref",
    "official_tokens",
    "recall_pair",
    "status_counts",
]

STATUSES: tuple[str, ...] = ("ok", "repaired", "unresolved")
_LIST_SEP = re.compile(r"\s*[;,]\s*")  # the datasets' own list convention
_WS = re.compile(r"\s+")
_STRAY_COLON = re.compile(r"^([A-Za-z]+):(\d+):(\d+)$")  # "D:11:26"
_PARTS = re.compile(r"^([A-Za-z]*)(\d+):(\d+)$")  # "D30:05"


@dataclass(frozen=True)
class EvidenceToken:
    """One token of a raw reference. ``how`` is ``exact``, ``stray_colon``, ``leading_zero``,
    ``case`` or ``unresolved``; ``resolved`` is None only when unresolved."""

    raw: str
    resolved: str | None
    how: str


@dataclass(frozen=True)
class EvidenceRef:
    raw: str
    tokens: tuple[EvidenceToken, ...]
    status: str

    @property
    def resolved_ids(self) -> tuple[str, ...]:
        return tuple(t.resolved for t in self.tokens if t.resolved is not None)

    @property
    def unresolved_tokens(self) -> tuple[str, ...]:
        return tuple(t.raw for t in self.tokens if t.resolved is None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "status": self.status,
            "resolved": list(self.resolved_ids),
            "unresolved": list(self.unresolved_tokens),
            "tokens": [{"raw": t.raw, "resolved": t.resolved, "how": t.how} for t in self.tokens],
        }


@dataclass(frozen=True)
class EvidenceView:
    """The view of one question's evidence list. ``raw`` is exactly what the dataset holds."""

    raw: tuple[str, ...]
    refs: tuple[EvidenceRef, ...] = field(default_factory=tuple)

    @property
    def status(self) -> str:
        """Worst status over the refs (``ok`` for an empty list)."""
        order = {s: i for i, s in enumerate(STATUSES)}
        return max((r.status for r in self.refs), key=order.__getitem__, default="ok")

    @property
    def resolved_ids(self) -> tuple[str, ...]:
        """Resolved ids in first-seen order, duplicates dropped."""
        return tuple(dict.fromkeys(i for r in self.refs for i in r.resolved_ids))

    @property
    def unresolved_tokens(self) -> tuple[str, ...]:
        return tuple(t for r in self.refs for t in r.unresolved_tokens)

    def restore(self) -> list[str]:
        """The original list, unchanged (the view is reversible)."""
        return [r.raw for r in self.refs]


def official_tokens(raw: Iterable[str]) -> list[str]:
    """The tokens the official loaders and ``forensics_report.gold_turns`` use: split on ``;`` / ``,``
    only, no repair. This is the immutable label."""
    out: list[str] = []
    for e in raw:
        out += [p.strip() for p in re.split(r"[;,]", str(e)) if p.strip()]
    return out


def _resolve_token(tok: str, known: Collection[str], lower: Mapping[str, str]) -> EvidenceToken:
    if tok in known:
        return EvidenceToken(tok, tok, "exact")
    m = _STRAY_COLON.match(tok)
    if m:
        cand = f"{m.group(1)}{m.group(2)}:{m.group(3)}"
        if cand in known:
            return EvidenceToken(tok, cand, "stray_colon")
    m = _PARTS.match(tok)
    if m:
        cand = f"{m.group(1)}{int(m.group(2))}:{int(m.group(3))}"
        if cand != tok and cand in known:
            return EvidenceToken(tok, cand, "leading_zero")
    hit = lower.get(tok.lower())
    if hit is not None and hit != tok:
        return EvidenceToken(tok, hit, "case")
    return EvidenceToken(tok, None, "unresolved")


def normalise_ref(
    raw: str, known_ids: Collection[str], *, _lower: Mapping[str, str] | None = None
) -> EvidenceRef:
    """Resolve one raw evidence string against the turn ids of ITS OWN conversation."""
    raw = str(raw)
    lower = _lower if _lower is not None else _lowercase_index(known_ids)
    listed = [p for p in _LIST_SEP.split(raw.strip()) if p]
    whitespace_joined = False
    tokens: list[str] = []
    for part in listed:
        if part in known_ids:
            tokens.append(part)
            continue
        pieces = [p for p in _WS.split(part) if p]
        whitespace_joined |= len(pieces) > 1
        tokens += pieces
    if not tokens:
        return EvidenceRef(raw, (EvidenceToken(raw, None, "unresolved"),), "unresolved")
    toks = tuple(_resolve_token(t, known_ids, lower) for t in tokens)
    if any(t.resolved is None for t in toks):
        status = "unresolved"
    elif whitespace_joined or any(t.how != "exact" for t in toks):
        status = "repaired"
    else:
        status = "ok"
    return EvidenceRef(raw, toks, status)


def _lowercase_index(known_ids: Collection[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in known_ids:
        out.setdefault(k.lower(), k)
    return out


def normalise_evidence(raw: Sequence[str], known_ids: Collection[str]) -> EvidenceView:
    """The view of one question's raw evidence list (a ``str`` is taken as a one-element list)."""
    items = [raw] if isinstance(raw, str) else list(raw)
    known = known_ids if isinstance(known_ids, (set, frozenset, dict)) else set(known_ids)
    lower = _lowercase_index(known)
    return EvidenceView(
        tuple(str(r) for r in items), tuple(normalise_ref(r, known, _lower=lower) for r in items)
    )


def recall_pair(
    retrieved: Collection[str], view: EvidenceView, *, k: int | None = None
) -> dict[str, Any]:
    """Official all-gold recall (R_all) beside the adjudicated diagnostic.

    ``official``: fraction of the raw-split gold tokens found in the top ``k`` retrieved ids (an
    unresolvable token is a miss, as in the official metric). ``adjudicated``: the same over the
    resolved ids, unresolved tokens left out; ``None`` when no id resolves. ``differs`` flags a
    question the repair would move; the official number stays the reported score.
    """
    pool = set(list(retrieved)[:k] if k is not None else retrieved)
    gold = official_tokens(view.raw)
    adj = list(view.resolved_ids)

    def frac(ids: Sequence[str]) -> float | None:
        return sum(1 for g in ids if g in pool) / len(ids) if ids else None

    off, dia = frac(gold), frac(adj)
    return {
        "status": view.status,
        "official": off,
        "adjudicated": dia,
        "differs": off != dia,
    }


def status_counts(views: Iterable[EvidenceView]) -> dict[str, int]:
    """Count of *references* (raw strings) per status, plus the number of questions per worst status."""
    refs = dict.fromkeys(STATUSES, 0)
    qs = dict.fromkeys(STATUSES, 0)
    n_q = 0
    for v in views:
        n_q += 1
        qs[v.status] += 1
        for r in v.refs:
            refs[r.status] += 1
    return {
        **{f"ref_{s}": n for s, n in refs.items()},
        **{f"question_{s}": n for s, n in qs.items()},
        "questions": n_q,
    }
