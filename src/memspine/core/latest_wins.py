"""I17: read-side "latest wins" presentation for evidence that restates one fact.

When a user's fact changes ("I live in Chicago", later "I moved to Boston"), the
retrieved evidence holds both statements and the reader has to guess which is
current. This module makes the temporal order explicit in the rendered context
without dropping anything: the newest statement of a topic is marked ``latest``,
each older one ``earlier`` and pointing at the date of the newest later statement.
The older value stays in the context as history (the ``supersede`` operator keeps
the retired value), and the wording is hedged ("a later statement ... exists"), so a
pair that is not an update at all (two pets, ``coexist``) is never told that one
value replaced the other. Records already in conflict (tagged ``disputed``, the
``contest`` end state) get a ``disputed`` mark and no ordering claim.

Which records restate one topic, with no model and no language list:

- two keyed records (``entity`` + ``attribute``) are the same topic iff the keys match,
  and a keyed record is never grouped with an unkeyed one;
- two unkeyed records (raw episodic turns) are the same topic iff the same speaker
  said both and their content words overlap by at least ``min_overlap`` (overlap
  coefficient: shared / size of the smaller set), while each also carries words the
  other lacks (a differing value, not a repeat).

Both need distinct event times; records with equal ``valid_from`` are unordered and
left alone. Pure functions, no I/O. The relation is pairwise, not transitive, so a
long chain of loosely related turns cannot merge into one group.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from memspine.core.query_shape import content_words
from memspine.core.records import MemoryRecord, RecordStatus

__all__ = [
    "DISPUTED_TAG",
    "LatestWinsMode",
    "Mark",
    "apply_latest_wins",
    "mark_latest",
    "recent_first",
]

LatestWinsMode = Literal["off", "annotate", "annotate_recent_first"]

#: The tag the conflict ladder puts on both sides of a CONTEST.
DISPUTED_TAG = "disputed"

LATEST_MARKER = "[latest]"
DISPUTED_MARKER = "[disputed: conflicting statements, unresolved]"

#: A leading ``Speaker: `` label inside the first characters of a turn.
_SPEAKER = re.compile(r"^\s*([^\W\d_][\w .'-]{0,30}?):\s")

_ELIGIBLE_TYPES = frozenset({"episodic", "semantic"})


def _earlier_marker(newest: datetime) -> str:
    return f"[earlier statement; a later one on this topic is dated {newest:%Y-%m-%d}]"


@dataclass(frozen=True, slots=True)
class Mark:
    """What one record is in the temporal picture of its topic."""

    kind: Literal["latest", "earlier", "disputed"]
    #: ``earlier``: the event time of the newest later statement on the topic.
    newest: datetime | None = None


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _speaker(record: MemoryRecord) -> tuple[str, str]:
    match = _SPEAKER.match(record.content[:80])
    return (record.source.role, match.group(1).strip().casefold() if match else "")


def _body(record: MemoryRecord) -> str:
    match = _SPEAKER.match(record.content[:80])
    return record.content[match.end() :] if match else record.content


def _words(record: MemoryRecord) -> frozenset[str]:
    body = _body(record)
    words = content_words(body)
    return words or frozenset(w for w in re.findall(r"\w+", body.casefold()) if len(w) > 1)


def _non_fact(record: MemoryRecord) -> bool:
    """I39: a turn the perspective layer tagged as only a question, request or hypothetical
    states no value, so it neither supersedes nor is superseded."""
    mods = {t[4:] for t in record.tags if t.startswith("mod:")}
    return bool(mods) and not mods & {"fact", "plan", "wish", "opinion"}


def _subjects(record: MemoryRecord) -> frozenset[str]:
    return frozenset(t for t in record.tags if t.startswith("sub:"))


def _eligible(record: MemoryRecord) -> bool:
    return (
        record.memory_type in _ELIGIBLE_TYPES
        and not record.is_engine_block
        and not record.quarantined
        and record.status is RecordStatus.ACTIVATED
        and "retract" not in record.tags
        and not _non_fact(record)
    )


def _same_topic(
    a: MemoryRecord,
    b: MemoryRecord,
    words: dict[str, frozenset[str]],
    min_overlap: float,
) -> bool:
    a_keyed = bool(a.entity and a.attribute)
    b_keyed = bool(b.entity and b.attribute)
    if a_keyed or b_keyed:
        return (
            a_keyed
            and b_keyed
            and (a.entity or "").casefold() == (b.entity or "").casefold()
            and (a.attribute or "").casefold() == (b.attribute or "").casefold()
        )
    sub_a, sub_b = _subjects(a), _subjects(b)
    if sub_a and sub_b and sub_a != sub_b:
        return False  # I17 x I39: a value about another subject is not an update
    sa, sb = _speaker(a), _speaker(b)
    if sa[0] != sb[0] or (sa[1] and sb[1] and sa[1] != sb[1]):
        return False
    wa, wb = words[a.record_id], words[b.record_id]
    shared = wa & wb
    if not shared or not (wa - wb) or not (wb - wa):
        return False
    return len(shared) / min(len(wa), len(wb)) >= min_overlap


def mark_latest(records: Sequence[MemoryRecord], min_overlap: float = 0.5) -> dict[str, Mark]:
    """The temporal mark of every record that restates a topic another record also
    states at a different time, by ``record_id``. Records on no such topic get none."""
    pool = [r for r in records if _eligible(r)]
    words = {r.record_id: _words(r) for r in pool}
    related: dict[str, list[MemoryRecord]] = {r.record_id: [] for r in pool}
    for a, b in itertools.combinations(pool, 2):
        if _aware(a.valid_from) == _aware(b.valid_from):
            continue
        if _same_topic(a, b, words, min_overlap):
            related[a.record_id].append(b)
            related[b.record_id].append(a)
    marks: dict[str, Mark] = {}
    for r in pool:
        peers = related[r.record_id]
        if not peers:
            continue
        if DISPUTED_TAG in r.tags or any(DISPUTED_TAG in p.tags for p in peers):
            marks[r.record_id] = Mark("disputed")
            continue
        mine = _aware(r.valid_from)
        later = [_aware(p.valid_from) for p in peers if _aware(p.valid_from) > mine]
        marks[r.record_id] = Mark("earlier", max(later)) if later else Mark("latest")
    return marks


def _annotated(record: MemoryRecord, mark: Mark) -> MemoryRecord:
    if mark.kind == "earlier" and mark.newest is not None:
        label = _earlier_marker(mark.newest)
    elif mark.kind == "latest":
        label = LATEST_MARKER
    else:
        label = DISPUTED_MARKER
    return record.model_copy(update={"content": f"{label} {record.content}"})


def recent_first(records: Sequence[MemoryRecord], linked: set[str]) -> list[MemoryRecord]:
    """The ``linked`` records gathered where the first of them stood, newest first;
    every other record keeps its place and order."""
    group = sorted(
        (r for r in records if r.record_id in linked),
        key=lambda r: (_aware(r.valid_from), r.recorded_at, r.record_id),
        reverse=True,
    )
    out: list[MemoryRecord] = []
    placed = False
    for r in records:
        if r.record_id not in linked:
            out.append(r)
        elif not placed:
            out.extend(group)
            placed = True
    return out


def apply_latest_wins(
    records: Sequence[MemoryRecord],
    mode: LatestWinsMode,
    min_overlap: float = 0.5,
) -> tuple[list[MemoryRecord], set[str]]:
    """``records`` with each marked record's content prefixed by its mark (copies; the
    stored record is untouched), plus the ids of the marked records. ``off`` and a
    context with no restated topic return the records unchanged."""
    if mode == "off":
        return list(records), set()
    marks = mark_latest(records, min_overlap)
    if not marks:
        return list(records), set()
    out = [_annotated(r, marks[r.record_id]) if r.record_id in marks else r for r in records]
    return out, set(marks)
