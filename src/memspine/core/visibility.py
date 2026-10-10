"""I53: participants, a per-record visibility, and the optional ``viewer`` read predicate.

The namespace stays the hard isolation boundary (``core/namespace.py``); nothing here
crosses it. Inside one namespace a conversation can have several speakers (a group chat, a
household assistant, a support thread). ``participant:<name>`` tags record who was present;
``visibility:<private|participants|owner_shared>`` says who may see the record:

* ``private``: only the record's speaker, the first participant (a message to the assistant
  that the speaker does not want shown to the others);
* ``participants``: every participant;
* ``owner_shared``: any viewer in the namespace.

A record with neither tag is unmarked and behaves as today. With a ``viewer`` set on a read,
a record is returned when the viewer may see it (:func:`visible_to`). A record with
participants but no visibility is a ``participants`` record. Derived records (a mined fact,
a summary) inherit the union of their parents' participants and the strictest visibility
(:func:`inherit_tags`), so a fact mined from a private turn is as private as the turn.

Tags are the carrier because the perspective layer (gaps I39-I47) tags speakers the same
way; if ``core/perspective.py`` defines participant/visibility constants, re-export them here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

__all__ = [
    "PARTICIPANT_PREFIX",
    "VISIBILITIES",
    "VISIBILITY_PREFIX",
    "inherit_tags",
    "participant_tags",
    "participants_of",
    "visibility_of",
    "visibility_tag",
    "visible_to",
]

PARTICIPANT_PREFIX = "participant:"
VISIBILITY_PREFIX = "visibility:"

#: Least to most restrictive.
VISIBILITIES: tuple[str, ...] = ("owner_shared", "participants", "private")


def _norm(name: str) -> str:
    return " ".join(name.strip().lower().split())


def participant_tags(names: Iterable[str]) -> list[str]:
    """``participant:<name>`` for each distinct non-empty name, in order (first = speaker)."""
    seen: dict[str, str] = {}
    for name in names:
        key = _norm(name)
        if key and key not in seen:
            seen[key] = f"{PARTICIPANT_PREFIX}{name.strip()}"
    return list(seen.values())


def visibility_tag(visibility: str) -> str:
    if visibility not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {VISIBILITIES}, got {visibility!r}")
    return f"{VISIBILITY_PREFIX}{visibility}"


def participants_of(tags: Iterable[str]) -> list[str]:
    """The participant names a record's tags carry, in order (the first is the speaker)."""
    return [t[len(PARTICIPANT_PREFIX) :] for t in tags if t.startswith(PARTICIPANT_PREFIX)]


def visibility_of(tags: Iterable[str]) -> str | None:
    """The most restrictive ``visibility:`` tag, None when the record is unmarked."""
    found = [
        t[len(VISIBILITY_PREFIX) :]
        for t in tags
        if t.startswith(VISIBILITY_PREFIX) and t[len(VISIBILITY_PREFIX) :] in VISIBILITIES
    ]
    return max(found, key=VISIBILITIES.index) if found else None


def visible_to(tags: Sequence[str], viewer: str) -> bool:
    """May ``viewer`` see a record carrying ``tags``? Unmarked records are visible."""
    visibility = visibility_of(tags)
    names = [_norm(n) for n in participants_of(tags)]
    who = _norm(viewer)
    if visibility == "owner_shared":
        return True
    if visibility == "private":
        return bool(names) and names[0] == who
    if visibility == "participants":
        return who in names
    # unmarked visibility: participants (when recorded) decide, else today's behaviour
    return not names or who in names


def inherit_tags(parent_tags: Sequence[Sequence[str]]) -> list[str]:
    """The participant and visibility tags a record derived from ``parent_tags`` carries:
    the union of the parents' participants, the strictest of their visibilities."""
    names: list[str] = []
    strictest: str | None = None
    for tags in parent_tags:
        names.extend(participants_of(tags))
        vis = visibility_of(tags)
        if vis is not None and (
            strictest is None or VISIBILITIES.index(vis) > VISIBILITIES.index(strictest)
        ):
            strictest = vis
    out = participant_tags(names)
    if strictest is not None:
        out.append(f"{VISIBILITY_PREFIX}{strictest}")
    return out
