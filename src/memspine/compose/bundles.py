"""Complete evidence bundles and the whole-or-nothing packer (R-18 step 1, ER-24).

Pure and deterministic. A bundle is an assertion with all its support evidence, binding evidence and time
anchors. :func:`pack` includes a bundle whole or not at all, skips an oversize bundle and keeps trying
smaller later ones, renders evidence shared between bundles once (cited by both, counted once), honours
per-block shares and then hands unused share to the next-ranked evidence.

Every candidate is rendered from scratch and the *whole* text is counted, so the result is exact even for
tokenizers whose counts are not additive over pieces.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, get_args

from memspine.compose.budget import Accounting, Budget
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.evidence import Evidence
from memspine.schemas.public.read import ContextBlock, EvidenceBundle, RenderedLine

__all__ = [
    "BLOCK_ORDER",
    "BlockKind",
    "DropReason",
    "DroppedBundle",
    "PackResult",
    "TurnKey",
    "TurnText",
    "build_bundles",
    "pack",
]

BlockKind = Literal["contract", "profile", "raw", "notes", "public"]
BLOCK_ORDER: tuple[str, ...] = get_args(BlockKind)
DropReason = Literal["over_budget", "missing_turn_text", "early_break", "duplicate_record"]
TurnKey = tuple[str, int, int]  # (source_id, source_version, seg_index)


@dataclass(frozen=True)
class TurnText:
    """The renderable content of a cited turn, supplied by the caller (bundles hold references only)."""

    text: str
    speaker: str | None = None
    date: str | None = None


@dataclass(frozen=True)
class DroppedBundle:
    record_id: str
    reason: DropReason
    tokens_needed: int | None = None  # marginal cost when it was refused, if measured


@dataclass(frozen=True)
class PackResult:
    """Packed context. ``tokens`` counts ``text`` (the full render) with ``tokenizer``; with
    ``accounting == "exact"`` it never exceeds ``available``. ``bundle_tokens`` is each included
    bundle's marginal cost when it was admitted."""

    blocks: list[ContextBlock]
    text: str
    tokens: int
    available: int
    accounting: Accounting
    tokenizer: str
    included: list[EvidenceBundle]
    dropped: list[DroppedBundle]
    bundle_tokens: dict[str, int] = field(default_factory=dict)


def _key(e: Evidence) -> TurnKey:
    t = e.turn
    if t is None:  # support/binding evidence always cites a turn (schema invariant)
        raise ValueError("evidence without a turn cannot be rendered")
    return (t.source_id, t.source_version, t.seg_index)


def build_bundles(items: Iterable[AssertionEnvelope]) -> list[EvidenceBundle]:
    """One bundle per assertion, carrying all of its support and binding evidence (stored order) and its
    time anchors (event start and end). ``context`` evidence is not part of a bundle. Input order is
    kept; a repeated ``record_id`` keeps its first occurrence. An assertion without a ``record_id`` is an
    error (the write door has not assigned one)."""
    out: list[EvidenceBundle] = []
    seen: set[str] = set()
    for a in items:
        if a.record_id is None:
            raise ValueError("cannot bundle an assertion that has no record_id")
        if a.record_id in seen:
            continue
        seen.add(a.record_id)
        ev = a.time.event
        anchors = [r for r in ((ev.start, ev.end) if ev is not None else ()) if r is not None]
        out.append(
            EvidenceBundle(
                record_id=a.record_id,
                assertion=a,
                support=[e for e in a.evidence if e.role == "support"],
                binding=[e for e in a.evidence if e.role == "binding"],
                time_anchors=anchors,
            )
        )
    return out


def _when(b: EvidenceBundle) -> str | None:
    parts = [r.text or (r.at.isoformat() if r.at else "") for r in b.time_anchors]
    parts = [p for p in parts if p]
    if parts:
        return " to ".join(parts)
    st = b.assertion.time.source_time
    return st.date().isoformat() if st is not None else None


def _cited(b: EvidenceBundle) -> list[tuple[str, Evidence]]:
    return [(e.role, e) for e in (*b.support, *b.binding)]


Rendered = dict[str, tuple[list[RenderedLine], str]]


def _render(
    sel: Mapping[str, Sequence[EvidenceBundle]],
    turns: Mapping[TurnKey, TurnText],
    preamble: str,
) -> tuple[Rendered, str]:
    """Deterministic render. Aliases are integers in rendering order; each distinct turn is rendered once,
    in the first bundle (block order, then rank) that cites it, and cited by alias elsewhere."""
    alias_of: dict[TurnKey, int] = {}
    n = 0
    blocks: Rendered = {}
    for kind in BLOCK_ORDER:
        bundles = sel.get(kind, ())
        if not bundles:
            continue
        lines: list[RenderedLine] = []
        for b in bundles:
            n += 1
            head_alias = n
            fresh: list[tuple[int, str, TurnKey]] = []
            cites: list[int] = []
            for role, e in _cited(b):
                k = _key(e)
                if k not in alias_of:
                    n += 1
                    alias_of[k] = n
                    fresh.append((n, role, k))
                if alias_of[k] not in cites:
                    cites.append(alias_of[k])
            a = b.assertion
            when = _when(b)
            meta = ", ".join(x for x in (a.speaker, when) if x)
            text = a.text + (f" ({meta})" if meta else "")
            if cites:
                text += " " + "".join(f"[{c}]" for c in cites)
            lines.append(
                RenderedLine(
                    alias=f"[{head_alias}]",
                    record_id=b.record_id,
                    speaker=a.speaker,
                    date=when,
                    modality=a.assessment.modality,
                    text=text,
                )
            )
            for al, role, k in fresh:
                t = turns[k]
                who = f"{t.speaker}: " if t.speaker else ""
                lines.append(
                    RenderedLine(
                        alias=f"[{al}]",
                        record_id=b.record_id,
                        speaker=t.speaker,
                        date=t.date,
                        text=f"{role} | {who}{t.text}" + (f" ({t.date})" if t.date else ""),
                    )
                )
        body = "\n".join(f"{ln.alias} {ln.text}" for ln in lines)
        blocks[kind] = (lines, f"## {kind}\n{body}")
    parts = ([preamble] if preamble else []) + [t for _, t in blocks.values()]
    return blocks, "\n\n".join(parts)


class _Packer:
    def __init__(
        self,
        uniq: dict[str, EvidenceBundle],
        ranked_ids: list[str],
        budget: Budget,
        turns: Mapping[TurnKey, TurnText],
        kinds: Mapping[str, str],
        preamble: str,
        max_skips: int | None,
    ) -> None:
        self.uniq = uniq
        self.rank = {r: i for i, r in enumerate(ranked_ids)}
        self.budget = budget
        self.counter = budget.counter
        self.avail = budget.available
        self.turns = turns
        self.kinds = kinds
        self.preamble = preamble
        self.max_skips = max_skips
        self.sel: dict[str, list[EvidenceBundle]] = {}
        self.marginal: dict[str, int] = {}
        self.dropped: list[DroppedBundle] = []
        self.skips = 0
        self.stopped = False
        self.cur = self.counter.count(preamble) if preamble else 0

    def kind_of(self, r: str) -> str:
        return self.kinds.get(r, "raw")

    def _with(self, b: EvidenceBundle) -> dict[str, list[EvidenceBundle]]:
        view = {k: list(v) for k, v in self.sel.items()}
        lst = view.setdefault(self.kind_of(b.record_id), [])
        lst.append(b)
        lst.sort(key=lambda x: self.rank[x.record_id])
        return view

    def attempt(self, r: str, *, enforce_cap: bool) -> bool | None:
        """True admitted, None deferred (block share only), False dropped."""
        b = self.uniq[r]
        if any(_key(e) not in self.turns for _, e in _cited(b)):
            self.dropped.append(DroppedBundle(r, "missing_turn_text"))
            return False
        view = self._with(b)
        blocks, full = _render(view, self.turns, self.preamble)
        total = self.counter.count(full)
        if total > self.avail:
            self.dropped.append(DroppedBundle(r, "over_budget", total - self.cur))
            self.skips += 1
            return False
        kind = self.kind_of(r)
        if (
            enforce_cap
            and self.budget.shares
            and self.counter.count(blocks[kind][1]) > self.budget.block_cap(kind)
        ):
            return None
        self.sel = view
        self.marginal[r] = total - self.cur
        self.cur = total
        return True

    def run(self, ids: list[str], *, enforce_cap: bool) -> list[str]:
        deferred: list[str] = []
        for i, r in enumerate(ids):
            if self.max_skips is not None and self.skips >= self.max_skips:
                self.dropped.extend(DroppedBundle(x, "early_break") for x in ids[i:])
                self.stopped = True
                return deferred
            if self.attempt(r, enforce_cap=enforce_cap) is None:
                deferred.append(r)
        return deferred


def pack(
    bundles: Sequence[EvidenceBundle],
    budget: Budget,
    *,
    order: Sequence[str],
    turns: Mapping[TurnKey, TurnText] | None = None,
    kinds: Mapping[str, BlockKind] | None = None,
    early_break_after_n_skips: int | None = None,
    preamble: str = "",
) -> PackResult:
    """Pack bundles whole-or-nothing into ``budget``.

    ``order`` lists record ids best first; bundles it does not name follow, in input order. ``kinds`` maps
    a record id to its block (default ``raw``). ``turns`` supplies the text of every cited turn; a bundle
    with a missing turn is dropped (``missing_turn_text``), never shown partially. ``preamble`` (a contract
    header, say) is rendered first and counted.

    Pass 1 admits bundles in rank order within each block's guaranteed share (``Budget.shares``). A bundle
    that only fails its block share is deferred; pass 2 offers deferred bundles, in rank order, whatever
    capacity the blocks left unused. A bundle that does not fit the total is skipped and later, smaller
    ones are still tried; after ``early_break_after_n_skips`` such skips the rest are dropped
    (``early_break``). With no shares configured there are no block caps.
    """
    counter = budget.counter
    avail = budget.available
    uniq: dict[str, EvidenceBundle] = {}
    dup: list[DroppedBundle] = []
    for b in bundles:
        if b.record_id in uniq:
            dup.append(DroppedBundle(b.record_id, "duplicate_record"))
        else:
            uniq[b.record_id] = b
    named = [r for r in dict.fromkeys(order) if r in uniq]
    named_set = set(named)
    ranked = named + [r for r in uniq if r not in named_set]

    p = _Packer(uniq, ranked, budget, turns or {}, kinds or {}, preamble, early_break_after_n_skips)
    p.dropped.extend(dup)
    if p.cur > avail:
        p.dropped.extend(DroppedBundle(r, "over_budget") for r in ranked)
    else:
        deferred = p.run(ranked, enforce_cap=True)
        if deferred and not p.stopped:
            deferred = p.run(deferred, enforce_cap=False)
        p.dropped.extend(DroppedBundle(r, "over_budget") for r in deferred)

    blocks, full = _render(p.sel, p.turns, preamble)
    total = counter.count(full)
    out_blocks: list[ContextBlock] = []
    for kind, (lines, text) in blocks.items():
        used = counter.count(text)
        guaranteed = budget.block_cap(kind) if budget.shares else avail
        out_blocks.append(
            ContextBlock(
                kind=kind,  # type: ignore[arg-type]
                lines=lines,
                tokens=used,
                cap=guaranteed if used <= guaranteed else max(avail, used),
                accounting=counter.accounting,
                tokenizer=counter.name,
            )
        )
    return PackResult(
        blocks=out_blocks,
        text=full,
        tokens=total,
        available=avail,
        accounting=counter.accounting,
        tokenizer=counter.name,
        included=[b for k in BLOCK_ORDER for b in p.sel.get(k, ())],
        dropped=p.dropped,
        bundle_tokens=p.marginal,
    )
