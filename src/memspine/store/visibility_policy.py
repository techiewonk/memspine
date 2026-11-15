"""Pure effective audience key shared by record reads and claim projections."""

from __future__ import annotations

from collections.abc import Iterable

from memspine.store.records import MemoryRecord

VISIBILITY_PREFIX = "visibility:"
VISIBILITIES: tuple[str, ...] = ("owner_shared", "participants", "private")


def normalize_participant(name: str) -> str:
    return " ".join(name.strip().casefold().split())


def visibility_of(tags: Iterable[str]) -> str | None:
    found = [
        tag[len(VISIBILITY_PREFIX) :]
        for tag in tags
        if tag.startswith(VISIBILITY_PREFIX) and tag[len(VISIBILITY_PREFIX) :] in VISIBILITIES
    ]
    return max(found, key=VISIBILITIES.index) if found else None


def audience_key(record: MemoryRecord) -> tuple[str, ...]:
    """Exact declared policy and viewers; equal keys are safe to compare.

    The policy label is retained even when two labels happen to admit the same
    current viewers. This conservatively avoids a conflict flag based on a
    differently governed peer.
    """
    policy = visibility_of(record.tags)
    names = [normalize_participant(name) for name in record.axes.participants]
    names = [name for name in names if name]
    if policy == "owner_shared":
        return ("owner_shared",)
    if policy == "private":
        return ("private", names[0]) if names else ("private",)
    if policy == "participants":
        return ("participants", *sorted(set(names)))
    return ("default", *sorted(set(names)))
