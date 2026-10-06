"""The three interfaces, and the value objects they exchange.

Everything in this module is stdlib-only and importable without memspine's
runtime dependencies, so the harness's contracts can be exercised in any
environment (A4-1). Concrete systems and datasets import lazily.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ── history + queries ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Turn:
    """One unit of history, in insertion order.

    A conversational turn, an agent trajectory step and a document chunk all
    reduce to this shape, which is why the adapter contract does not need a
    per-modality variant. ``turn_id`` must be stable across runs: retrieval
    recall (R@k) is scored by comparing retrieved ``turn_id`` s against a
    query's ``gold_turn_ids``.
    """

    turn_id: str
    session_id: str
    speaker: str
    text: str
    timestamp: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Query:
    """One question against the history inserted before it.

    ``gold_turn_ids`` carries the retrieval ground truth where the dataset has
    it (LongMemEval labels the gold session; LoCoMo labels evidence turns).
    Without it R@k is not computable and the harness reports it as ``None``
    rather than guessing — a missing metric is honest, a fabricated one is not.
    """

    query_id: str
    text: str
    gold: str | None = None
    gold_turn_ids: tuple[str, ...] = ()
    type_label: str | None = None
    after_turn: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvalItem:
    """One user / conversation / trajectory: a history stream and its queries."""

    item_id: str
    history: tuple[Turn, ...]
    queries: tuple[Query, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    """Identity of the exact bytes a run consumed.

    ``revision_id`` is mandatory and has no default. LongMemEval v1 was
    re-released in September 2025 with cleaned histories by its own first
    author, and nothing in the literature marks which revision a published
    score used; every row in ``_shared/score_matrix.csv`` that lacks this field
    is inadmissible under D16. The harness refuses to produce such a row.
    """

    dataset_id: str
    revision_id: str
    licence: str
    source_path: str
    content_sha256: str
    n_items: int
    n_queries: int
    subset: str = "full"
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.revision_id.strip():
            raise ValueError(
                f"dataset {self.dataset_id!r} has no revision_id — "
                "a score whose data revision is unknown is not comparable to any other score"
            )


@runtime_checkable
class DatasetAdapter(Protocol):
    """Yields evaluation items. Stateless; may stream from disk."""

    def info(self) -> DatasetInfo: ...

    def items(self) -> Iterator[EvalItem]: ...


# ── systems ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Evidence:
    """One retrieved unit — the observable half of the loop's $E_t$."""

    turn_id: str
    score: float
    text: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    """$M_{ctx,t}$: what the system hands the reader, already inside budget.

    ``truncated`` records whether the budget bit. A system that silently
    overflows the budget is not running the same protocol as one that does not,
    which is the whole reason LongMemEval-V2 truncates to a declared 200k and
    says so.
    """

    text: str
    tokens: int
    evidence: tuple[Evidence, ...] = ()
    truncated: bool = False
    boundary_index: int | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DepositResult:
    r"""$\Delta_t$: what one insert actually changed in the store."""

    n_records: int = 0
    record_ids: tuple[str, ...] = ()
    latency_ms: float = 0.0
    model_calls: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SystemAdapter(Protocol):
    """Sequential insert, then bounded query. Two verbs, deliberately.

    The contract is async because memspine is async-first (D-01) and because a
    real system under evaluation may do background work between inserts; the
    offline reference systems implement it trivially.
    """

    system_id: str

    def describe(self) -> Mapping[str, Any]:
        """Configuration material for the manifest's system config hash."""
        ...

    async def reset(self, item_id: str) -> None:
        """Start a fresh store for one item. Cross-item leakage is a silent
        inflation of every downstream number, so the runner calls this between
        items and the adapter must honour it."""
        ...

    async def insert(self, turn: Turn) -> DepositResult: ...

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext: ...

    async def close(self) -> None: ...


# ── readers ($\mathcal{G}$) ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReaderAnswer:
    """``truncated`` is not cosmetic: the failed native run lost 17 of 29
    responses to an output cap, and a truncated answer graded as a wrong answer
    is a measurement error, not a result."""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    model_calls: int = 0
    truncated: bool = False
    #: C9': the part of ``prompt_tokens`` served from the provider's prompt cache
    #: (0 when the provider reports none); fresh = prompt_tokens - cached.
    cached_prompt_tokens: int = 0
    finish_reason: str = ""
    #: The reader's raw reply when ``text`` was post-processed by answer extraction
    #: (``extract_answer``); None otherwise. Lands in the row's ``meta["reader_raw"]``.
    raw_text: str | None = None


@runtime_checkable
class Reader(Protocol):
    """The fixed backbone. One reader per run, recorded in the manifest."""

    reader_id: str
    model: str
    makes_model_calls: bool

    def describe(self) -> Mapping[str, Any]: ...

    async def answer(self, question: str, context: str) -> ReaderAnswer: ...


# ── helpers ──────────────────────────────────────────────────────────────────


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_mapping(payload: Mapping[str, Any]) -> str:
    """Order-independent hash of a flat-ish config mapping."""
    import json

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def visible_evidence(evidence: Sequence[Evidence], text_len: int) -> tuple[Evidence, ...]:
    """Evidence still inside a context cut to ``text_len`` characters (R3-7).

    Systems that know where each unit sits in the context put ``meta["span"] = (start,
    end)``; a unit whose span ends past the cut was truncated away and is dropped, so R@k
    cannot credit text the reader never saw. Evidence without a span is kept.
    """
    kept = []
    for row in evidence:
        span = (row.meta or {}).get("span")
        if span is not None and int(span[1]) > text_len:
            continue
        kept.append(row)
    return tuple(kept)


def join_turns(turns: Sequence[Turn]) -> str:
    return "\n".join(f"[{t.session_id}/{t.turn_id}] {t.speaker}: {t.text}" for t in turns)
