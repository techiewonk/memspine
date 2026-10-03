"""The floor and the ceiling, plus the verbatim contender.

A4-8 calls these the quiet dependency of the whole harness, and they are:

* **Full context** is the ceiling that every memory system must beat to justify
  existing. ConvoMem's result — full context beats memory systems below roughly
  150 conversations — means a win reported without this baseline is
  unfalsifiable.
* **Naive RAG** is the floor. Chunk, embed, top-k, no memory at all.
* **Verbatim** is the contender from Plan C §1.1: store the raw words, retrieve
  them, extract nothing. MemPalace reports 96.6% on LongMemEval this way
  (R@5 recall, not QA accuracy) against 60.3% R@10 on LoCoMo — the advantage
  does not transfer, and gating experiment **C0-1** exists to find out which of
  those two shapes our own datasets have before MemSpine's deposit-centric
  emphasis is committed to print.

All three are deterministic and make zero model calls when run with the
lexical retriever.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import DepositResult, Evidence, RetrievedContext, Turn, visible_evidence
from ..tokens import HeuristicTokenCounter, TokenCounter, truncate_to_budget
from .retrievers import BM25Retriever, Retriever, Unit


def _format(turn: Turn) -> str:
    stamp = f" ({turn.timestamp})" if turn.timestamp else ""
    return f"[{turn.session_id}/{turn.turn_id}]{stamp} {turn.speaker}: {turn.text}"


class NoMemorySystem:
    """Condition 1 of the evaluation plan's five: no persistent memory at all.

    It answers from the question alone. Every number above it is the value of
    having *any* memory, and without it a benchmark cannot separate "the system
    remembered" from "the backbone already knew".
    """

    system_id = "no-memory"

    def describe(self) -> Mapping[str, Any]:
        return {
            "system_id": self.system_id,
            "version": "1.0",
            "kind": "control",
            "extraction": "none",
            "retrieval": "none",
        }

    async def reset(self, item_id: str) -> None:
        return None

    async def insert(self, turn: Turn) -> DepositResult:
        return DepositResult(n_records=0, meta={"discarded": True})

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        return RetrievedContext(text="", tokens=0, meta={"ranked": False, "n_units": 0})

    async def close(self) -> None:
        return None


class FullContextSystem:
    """No memory: replay the whole history, newest-first truncation to budget.

    Retrieval recall is reported as unavailable rather than 1.0. The turns it
    returns are ordered by recency, not by relevance, so R@k over them would be
    a category error — and a silently perfect R@k for the baseline would make
    every comparison against it meaningless.
    """

    system_id = "full-context"

    def __init__(self, counter: TokenCounter | None = None) -> None:
        self._counter = counter or HeuristicTokenCounter()
        self._turns: list[Turn] = []

    def describe(self) -> Mapping[str, Any]:
        return {
            "system_id": self.system_id,
            "version": "1.0",
            "kind": "baseline",
            "extraction": "none",
            "retrieval": "none (full replay)",
            "token_counter": dict(self._counter.describe()),
        }

    async def reset(self, item_id: str) -> None:
        self._turns = []

    async def insert(self, turn: Turn) -> DepositResult:
        self._turns.append(turn)
        return DepositResult(n_records=1, record_ids=(turn.turn_id,))

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        kept: list[Turn] = []
        tokens = 0
        for turn in reversed(self._turns):
            line = _format(turn)
            cost = self._counter.count(line) + 1
            if tokens + cost > budget_tokens and kept:
                break
            kept.append(turn)
            tokens += cost
        kept.reverse()
        body = "\n".join(_format(turn) for turn in kept)
        # A single turn can exceed the whole budget; the invariant is that a
        # system never hands back more than it was given, so cut the text too.
        body, tokens, cut = truncate_to_budget(body, budget_tokens, self._counter)
        return RetrievedContext(
            text=body,
            tokens=tokens,
            evidence=tuple(Evidence(turn_id=t.turn_id, score=1.0) for t in kept),
            truncated=cut or len(kept) < len(self._turns),
            meta={"ranked": False, "n_turns": len(kept), "n_turns_total": len(self._turns)},
        )

    async def close(self) -> None:
        self._turns = []


class VerbatimSystem:
    """Store the raw turn, retrieve it, extract nothing (MemPalace-shaped)."""

    def __init__(
        self,
        retriever: Retriever | None = None,
        counter: TokenCounter | None = None,
        system_id: str | None = None,
    ) -> None:
        self._retriever = retriever or BM25Retriever()
        self._counter = counter or HeuristicTokenCounter()
        self.system_id = system_id or f"verbatim-{self._retriever.retriever_id.split(':')[0]}"

    def describe(self) -> Mapping[str, Any]:
        return {
            "system_id": self.system_id,
            "version": "1.0",
            "kind": "baseline",
            "extraction": "none",
            "unit": "turn",
            "retriever": dict(self._retriever.describe()),
            "token_counter": dict(self._counter.describe()),
        }

    async def reset(self, item_id: str) -> None:
        self._retriever.clear()

    async def insert(self, turn: Turn) -> DepositResult:
        self._retriever.add(
            Unit(unit_id=turn.turn_id, text=_format(turn), turn_ids=(turn.turn_id,))
        )
        return DepositResult(n_records=1, record_ids=(turn.turn_id,))

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        hits = self._retriever.search(text, top_k)
        return _pack(hits, budget_tokens, self._counter)

    async def close(self) -> None:
        self._retriever.clear()


class NaiveRAGSystem:
    """Fixed-size chunking over the running transcript, then top-k.

    The pending tail chunk is indexed at query time, so the most recent turns are
    retrievable. Without that, every recency question in the benchmark would fail
    for a reason that has nothing to do with retrieval quality. It is **one**
    pending unit, replaced in place on each query and removed when the real chunk
    closes (R3-2): re-adding it per query left ~200 copies of the tail in the index,
    crowding top-k and handicapping the baseline.
    """

    PENDING_ID = "chunk-pending"

    def __init__(
        self,
        retriever: Retriever | None = None,
        chunk_chars: int = 1200,
        overlap_turns: int = 1,
        counter: TokenCounter | None = None,
        system_id: str | None = None,
    ) -> None:
        self._retriever = retriever or BM25Retriever()
        self._counter = counter or HeuristicTokenCounter()
        self.chunk_chars = chunk_chars
        self.overlap_turns = overlap_turns
        self.system_id = system_id or f"naive-rag-{self._retriever.retriever_id.split(':')[0]}"
        self._buffer: list[Turn] = []
        self._chunks = 0
        self._pending_id: str | None = None

    def describe(self) -> Mapping[str, Any]:
        return {
            "system_id": self.system_id,
            "version": "1.0",
            "kind": "baseline",
            "extraction": "none",
            "unit": f"chunk<={self.chunk_chars}chars",
            "overlap_turns": self.overlap_turns,
            "retriever": dict(self._retriever.describe()),
            "token_counter": dict(self._counter.describe()),
        }

    async def reset(self, item_id: str) -> None:
        self._retriever.clear()
        self._buffer = []
        self._chunks = 0
        self._pending_id = None

    def _unit(self, unit_id: str, final: bool) -> Unit:
        lines = [_format(turn) for turn in self._buffer]
        spans: dict[str, tuple[int, int]] = {}
        offset = 0
        for turn, line in zip(self._buffer, lines, strict=True):
            spans[turn.turn_id] = (offset, offset + len(line))
            offset += len(line) + 1
        return Unit(
            unit_id=unit_id,
            text="\n".join(lines),
            turn_ids=tuple(turn.turn_id for turn in self._buffer),
            meta={"final": final, "turn_spans": spans},
        )

    def _drop_pending(self) -> None:
        if self._pending_id is not None:
            self._retriever.remove(self._pending_id)
            self._pending_id = None

    def _flush(self) -> str | None:
        if not self._buffer:
            return None
        self._drop_pending()
        unit_id = f"chunk-{self._chunks:04d}"
        self._retriever.add(self._unit(unit_id, final=False))
        self._chunks += 1
        self._buffer = self._buffer[-self.overlap_turns :] if self.overlap_turns else []
        return unit_id

    async def insert(self, turn: Turn) -> DepositResult:
        self._buffer.append(turn)
        size = sum(len(_format(t)) + 1 for t in self._buffer)
        if size >= self.chunk_chars:
            unit_id = self._flush()
            return DepositResult(n_records=1, record_ids=(unit_id,) if unit_id else ())
        return DepositResult(n_records=0, meta={"buffered": len(self._buffer)})

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        if self._buffer:
            # Index the tail as the single pending unit, replacing the previous one.
            self._drop_pending()
            self._retriever.add(self._unit(self.PENDING_ID, final=True))
            self._pending_id = self.PENDING_ID
        hits = self._retriever.search(text, top_k)
        return _pack(hits, budget_tokens, self._counter)

    async def close(self) -> None:
        self._retriever.clear()
        self._buffer = []


class BudgetCappedSystem:
    """Wraps a system and caps its context budget below the protocol's (R4-6).

    The matched-token-budget arm: a naive baseline given the tokens memspine actually
    used (e.g. its mean context size), not the full protocol budget, so an accuracy
    gap cannot be a token-count gap. The cap is declared in ``describe()`` and the
    system id, so the arm cannot pass as the uncapped one.
    """

    def __init__(self, inner: Any, budget_tokens: int, system_id: str | None = None) -> None:
        if budget_tokens <= 0:
            raise ValueError("matched budget must be positive")
        self.inner = inner
        self.budget_tokens = budget_tokens
        self.system_id = system_id or f"{inner.system_id}-matched{budget_tokens}"

    def describe(self) -> Mapping[str, Any]:
        return {
            "system_id": self.system_id,
            "version": "1.0",
            "kind": "baseline",
            "matched_budget_tokens": self.budget_tokens,
            "inner": dict(self.inner.describe()),
        }

    async def reset(self, item_id: str) -> None:
        await self.inner.reset(item_id)

    async def insert(self, turn: Turn) -> DepositResult:
        return await self.inner.insert(turn)

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        return await self.inner.query(text, min(budget_tokens, self.budget_tokens), top_k)

    async def close(self) -> None:
        await self.inner.close()


def _pack(
    hits: Sequence[tuple[Unit, float]], budget_tokens: int, counter: TokenCounter
) -> RetrievedContext:
    """Join ranked hits under budget, keeping the ranking order.

    Each evidence row names its unit (``unit_id``, so R@k counts units) and its
    character span in the context (``span``, so evidence cut by truncation is dropped;
    turn-level spans when the unit records them).
    """
    kept: list[str] = []
    evidence: list[Evidence] = []
    tokens = 0
    truncated = False
    offset = 0
    for unit, score in hits:
        cost = counter.count(unit.text) + 1
        if tokens + cost > budget_tokens and kept:
            truncated = True
            break
        kept.append(unit.text)
        tokens += cost
        spans = (unit.meta or {}).get("turn_spans") or {}
        for turn_id in unit.turn_ids:
            start, end = spans.get(turn_id, (0, len(unit.text)))
            evidence.append(
                Evidence(
                    turn_id=turn_id,
                    score=score,
                    meta={"unit_id": unit.unit_id, "span": (offset + start, offset + end)},
                )
            )
        offset += len(unit.text) + 1
    body = "\n".join(kept)
    body, tokens, cut = truncate_to_budget(body, budget_tokens, counter) if body else ("", 0, False)
    return RetrievedContext(
        text=body,
        tokens=tokens,
        evidence=visible_evidence(evidence, len(body)) if cut else tuple(evidence),
        truncated=truncated or cut or len(kept) < len(hits),
        meta={"ranked": True, "n_units": len(kept)},
    )
