"""W2-03: what derivation may use of a raw turn (``write.role_policy``).

Pure and tag-based, so the window extractor, the session-close builder and the gateway agree without
importing each other. The tags are written by ``ingest.role_wiring`` when the raw turn lands.
"""

from __future__ import annotations

from collections.abc import Iterable

from memspine.config import constants

__all__ = ["basis_of", "block_reason", "derive_use_of"]


def derive_use_of(tags: Iterable[str]) -> str | None:
    for tag in tags:
        if tag.startswith(constants.ROLE_USE_TAG_PREFIX):
            return tag[len(constants.ROLE_USE_TAG_PREFIX) :]
    return None


def block_reason(tags: Iterable[str]) -> str | None:
    """``echo`` / ``excluded`` when derivation must not read the turn, else None."""
    tags = list(tags)
    if constants.ECHO_TAG in tags:
        return "echo"
    if derive_use_of(tags) == "excluded":
        return "excluded"
    return None


def basis_of(tag_sets: Iterable[Iterable[str]]) -> str | None:
    """What backs a derived item: ``stated`` when any parent is user-stated (or untagged), else
    ``evidence`` (a tool parent) or ``proposal`` (assistant parents only); None with no parents."""
    uses = [derive_use_of(t) for t in tag_sets]
    if not uses:
        return None
    if any(u not in ("evidence", "proposal") for u in uses):
        return "stated"
    return "evidence" if "evidence" in uses else "proposal"
