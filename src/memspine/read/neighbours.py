"""LIB-24: neighbour windows around reranked turn hits (retrieve step 15; wired later by R3-10).

Pure and deterministic: no I/O, no clock, no engine imports. The caller supplies the ranked hits, an
ordered per-conversation turn index and a visibility predicate (ER-22 / POL-2: every neighbour turn
must pass the viewer's predicate; a hit that fails it is dropped too).

Algorithm
---------
Hits are processed best rank first. For each hit, neighbours are taken nearest first (older side
on a tie), up to ``before`` turns older and ``after`` turns newer, inside the same conversation
and session (``cross_session`` lifts the session wall). A side stops at a boundary, at a hidden
turn under ``strict`` (no bridging across it), or at the first turn that does not fit the
neighbour token share. Lower-ranked hits are processed later, so they lose neighbours first when
the share runs out. A turn that is itself a hit is never counted as a neighbour (it costs no
neighbour budget) and a turn already taken by an earlier hit is free for later ones. Per-hit
selections whose position intervals overlap or touch merge into one window carrying the best
anchor's id and the best score.

``budget`` may be an ``int`` (tokens for neighbours) or a ``compose.budget.Budget``: the cap is
then ``block_cap("neighbours")`` when a share is configured, else ``remaining``. The Budget is not
mutated. Hit turns are not charged to the neighbour share; ``tokens`` on a window is the whole
window.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from memspine.compose.budget import Budget, TokenCounter
from memspine.schemas.steps.retrieve import NeighbourWindow

NEIGHBOURS_BLOCK = "neighbours"

REASON_INVISIBLE = "invisible"
REASON_SESSION_BOUNDARY = "session_boundary"
REASON_OVER_BUDGET = "over_budget"
REASON_ALREADY_HIT = "already_hit"
REASON_HIT_INVISIBLE = "hit_invisible"


@dataclass(frozen=True)
class Turn:
    """One turn of a conversation. Its index in the conversation sequence is its order."""

    turn_id: str
    session_id: str
    text: str = ""


@dataclass(frozen=True)
class Hit:
    """A reranked turn hit; rank is the input order (best first)."""

    turn_id: str
    conversation_id: str
    score: float = 0.0


@dataclass(frozen=True)
class NeighbourConfig:
    cross_session: bool = False
    strict: bool = True  # a hidden turn closes its side: nothing beyond it bridges in
    anchors: int | None = None  # only the top N hits get neighbours; the rest stay single turns


@dataclass(frozen=True)
class Skip:
    turn_id: str
    anchor_id: str
    reason: str


class ExpandedWindow(NeighbourWindow):
    """``NeighbourWindow`` plus anchors, hit/neighbour split, neighbour cost and a skip trace.

    ``anchor_id`` is the best-ranked anchor; ``member_ids`` are all turns in conversation order.
    """

    conversation_id: str
    anchor_ids: list[str]
    hit_ids: list[str]
    neighbour_ids: list[str]
    neighbour_tokens: int
    skipped: list[Skip]


def _cap(budget: Budget | int) -> int:
    if isinstance(budget, int):
        return max(0, budget)
    if NEIGHBOURS_BLOCK in budget.shares:
        return min(budget.block_cap(NEIGHBOURS_BLOCK), budget.remaining)
    return budget.remaining


@dataclass
class _Sel:
    conv: str
    rank: int
    hit_pos: int
    score: float
    positions: set[int]
    skipped: list[Skip]

    @property
    def lo(self) -> int:
        return min(self.positions)

    @property
    def hi(self) -> int:
        return max(self.positions)


def expand(
    hits: Sequence[Hit],
    conversation_index: Mapping[str, Sequence[Turn]],
    visibility: Callable[[Turn], bool],
    *,
    before: int,
    after: int,
    budget: Budget | int,
    counter: TokenCounter,
    config: NeighbourConfig | None = None,
) -> list[ExpandedWindow]:
    """Neighbour windows for ``hits`` (best first); hits absent from the index are ignored."""
    if before < 0 or after < 0:
        raise ValueError("before and after are non-negative")
    cfg = config or NeighbourConfig()
    left = _cap(budget)
    pos_of = {c: {t.turn_id: i for i, t in enumerate(ts)} for c, ts in conversation_index.items()}
    hit_ids = {h.turn_id for h in hits}
    taken: set[tuple[str, int]] = set()
    sels: list[_Sel] = []
    seen_hits: set[str] = set()

    for rank, hit in enumerate(hits):
        turns = conversation_index.get(hit.conversation_id)
        at = pos_of.get(hit.conversation_id, {}).get(hit.turn_id)
        if turns is None or at is None or hit.turn_id in seen_hits:
            continue
        seen_hits.add(hit.turn_id)
        if not visibility(turns[at]):
            continue  # ER-22: an invisible hit gets no window at all
        skipped: list[Skip] = []
        sel = _Sel(hit.conversation_id, rank, at, hit.score, {at}, skipped)
        taken.add((hit.conversation_id, at))
        sels.append(sel)
        if cfg.anchors is not None and rank >= cfg.anchors:
            continue
        open_ = {-1: before > 0, 1: after > 0}
        reach = {-1: before, 1: after}
        for d in range(1, max(before, after) + 1):
            for side in (-1, 1):  # older first on a tie
                if not open_[side] or d > reach[side]:
                    continue
                i = at + side * d
                if i < 0 or i >= len(turns):
                    open_[side] = False
                    continue
                t = turns[i]
                if t.session_id != turns[at].session_id and not cfg.cross_session:
                    skipped.append(Skip(t.turn_id, hit.turn_id, REASON_SESSION_BOUNDARY))
                    open_[side] = False
                    continue
                if t.turn_id in hit_ids:
                    if not visibility(t):
                        skipped.append(Skip(t.turn_id, hit.turn_id, REASON_INVISIBLE))
                        if cfg.strict:
                            open_[side] = False
                        continue
                    skipped.append(Skip(t.turn_id, hit.turn_id, REASON_ALREADY_HIT))
                    sel.positions.add(i)
                    continue
                if not visibility(t):
                    skipped.append(Skip(t.turn_id, hit.turn_id, REASON_INVISIBLE))
                    if cfg.strict:
                        open_[side] = False
                    continue
                if (hit.conversation_id, i) in taken:
                    sel.positions.add(i)  # free: an earlier window already paid for it
                    continue
                cost = counter.count(t.text)
                if cost > left:
                    skipped.append(Skip(t.turn_id, hit.turn_id, REASON_OVER_BUDGET))
                    open_[side] = False
                    continue
                left -= cost
                taken.add((hit.conversation_id, i))
                sel.positions.add(i)

    # merge overlapping or touching intervals within a conversation
    by_pos = sorted(sels, key=lambda s: (s.conv, s.lo, s.rank))
    groups: list[list[_Sel]] = []
    for s in by_pos:
        if groups and groups[-1][0].conv == s.conv and s.lo <= max(x.hi for x in groups[-1]) + 1:
            groups[-1].append(s)
        else:
            groups.append([s])

    out: list[tuple[int, ExpandedWindow]] = []
    for g in groups:
        conv = g[0].conv
        turns = conversation_index[conv]
        ranked = sorted(g, key=lambda s: s.rank)
        positions = sorted(set().union(*(s.positions for s in g)))
        member = [turns[i].turn_id for i in positions]
        hits_in = [m for m in member if m in hit_ids]
        neigh = [m for m in member if m not in hit_ids]
        text = {turns[i].turn_id: turns[i].text for i in positions}
        n_tok = sum(counter.count(text[m]) for m in neigh)
        h_tok = sum(counter.count(text[m]) for m in hits_in)
        out.append(
            (
                ranked[0].rank,
                ExpandedWindow(
                    anchor_id=turns[ranked[0].hit_pos].turn_id,
                    member_ids=member,
                    score=max(s.score for s in g),
                    tokens=n_tok + h_tok,
                    conversation_id=conv,
                    anchor_ids=[turns[s.hit_pos].turn_id for s in ranked],
                    hit_ids=hits_in,
                    neighbour_ids=neigh,
                    neighbour_tokens=n_tok,
                    skipped=[k for s in ranked for k in s.skipped],
                ),
            )
        )
    out.sort(key=lambda p: p[0])
    return [w for _, w in out]
