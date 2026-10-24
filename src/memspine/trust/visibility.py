"""Participant visibility over canonical typed record axes.

Namespaces remain the hard isolation boundary. Within one namespace a record may be
private to its first participant, visible to all participants, or owner-shared.
Derived records inherit parent participants and the strictest visibility policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from memspine.store.records import MemoryRecord

VISIBILITY_PREFIX = "visibility:"
VISIBILITIES: tuple[str, ...] = ("owner_shared", "participants", "private")


def _norm(name: str) -> str:
    return " ".join(name.strip().casefold().split())


def _names(names: Iterable[str]) -> list[str]:
    unique: dict[str, str] = {}
    for name in names:
        key = _norm(name)
        if key:
            unique.setdefault(key, name.strip())
    return list(unique.values())


def visibility_tag(visibility: str) -> str:
    if visibility not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {VISIBILITIES}, got {visibility!r}")
    return f"{VISIBILITY_PREFIX}{visibility}"


def participants_of(record: MemoryRecord) -> list[str]:
    return list(record.axes.participants)


def visibility_of(tags: Iterable[str]) -> str | None:
    found = [
        t[len(VISIBILITY_PREFIX) :]
        for t in tags
        if t.startswith(VISIBILITY_PREFIX) and t[len(VISIBILITY_PREFIX) :] in VISIBILITIES
    ]
    return max(found, key=VISIBILITIES.index) if found else None


def visible_to(record: MemoryRecord, viewer: str) -> bool:
    visibility = visibility_of(record.tags)
    names = [_norm(n) for n in participants_of(record)]
    who = _norm(viewer)
    if visibility == "owner_shared":
        return True
    if visibility == "private":
        return bool(names) and names[0] == who
    if visibility == "participants":
        return who in names
    return not names or who in names


def restrict_to_parents(record: MemoryRecord, parents: Sequence[MemoryRecord]) -> MemoryRecord:
    """Use parent participants and the strictest parent/own visibility policy.

    A derived record cannot introduce viewers its parents did not name. With no
    parent participants the record's own explicit participant axes remain intact.
    """
    names = _names(name for parent in parents for name in parent.axes.participants)
    visibilities = [v for item in (record, *parents) if (v := visibility_of(item.tags))]
    tags = [t for t in record.tags if not t.startswith(VISIBILITY_PREFIX)]
    if visibilities:
        tags.append(visibility_tag(max(visibilities, key=VISIBILITIES.index)))
    axes = record.axes.model_copy(update={"participants": names}) if names else record.axes
    if tags == record.tags and axes == record.axes:
        return record
    return record.model_copy(update={"tags": tags, "axes": axes})
