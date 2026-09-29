"""Stage-trace logging (A4-3): the loop, written down as it runs.

Both papers need the same per-turn record, and retrofitting it costs a full
re-run — so it is emitted from the first run rather than added once someone
asks for it. The tuple is MemSpine's own notation:

    $E_t$        retrieved evidence            ($\\mathcal{R}$)
    $M_{ctx,t}$  the assembled context         ($\\mathcal{C}$)
    $y_t$        the generated answer          ($\\mathcal{G}$)
    $\\Delta_t$   the deposit increment         ($\\mathcal{D}$)
    $P^u_t$      the profile state             ($\\mathcal{K}_P$)

Content is hashed always and stored only when ``include_content`` is set:
traces of a full tier-1 run are large, and a hash is enough to prove two runs
saw the same context.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import Evidence, RetrievedContext, sha256_text


@dataclass(frozen=True, slots=True)
class DepositTrace:
    r"""$\Delta_t$ at insert time — the write half of the loop."""

    item_id: str
    turn_id: str
    t: int
    n_records: int
    record_ids: tuple[str, ...]
    latency_ms: float
    model_calls: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "deposit",
            "item_id": self.item_id,
            "turn_id": self.turn_id,
            "t": self.t,
            "delta_t": {
                "n_records": self.n_records,
                "record_ids": list(self.record_ids),
                "model_calls": self.model_calls,
                **dict(self.meta),
            },
            "latency_ms": round(self.latency_ms, 3),
        }


@dataclass(frozen=True, slots=True)
class CycleTrace:
    r"""One query cycle: $\mathcal{R} \to \mathcal{C} \to \mathcal{G}$."""

    item_id: str
    query_id: str
    t: int
    evidence: tuple[Evidence, ...]
    context_tokens: int
    context_sha256: str
    context_truncated: bool
    boundary_index: int | None
    answer_sha256: str
    prompt_tokens: int
    completion_tokens: int
    latency_retrieve_ms: float
    latency_answer_ms: float
    model_calls: int
    profile_sha256: str | None = None
    context_text: str | None = None
    answer_text: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "cycle",
            "item_id": self.item_id,
            "query_id": self.query_id,
            "t": self.t,
            "E_t": [{"turn_id": e.turn_id, "score": round(e.score, 6)} for e in self.evidence],
            "M_ctx_t": {
                "tokens": self.context_tokens,
                "sha256": self.context_sha256,
                "truncated": self.context_truncated,
                "boundary_index": self.boundary_index,
            },
            "y_t": {
                "sha256": self.answer_sha256,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            },
            "P_u_t": {"sha256": self.profile_sha256},
            "latency_ms": {
                "retrieve": round(self.latency_retrieve_ms, 3),
                "answer": round(self.latency_answer_ms, 3),
            },
            "model_calls": self.model_calls,
        }
        if self.context_text is not None:
            payload["M_ctx_t"]["text"] = self.context_text
        if self.answer_text is not None:
            payload["y_t"]["text"] = self.answer_text
        if self.meta:
            payload["meta"] = dict(self.meta)
        return payload


def cycle_from_context(
    item_id: str,
    query_id: str,
    t: int,
    context: RetrievedContext,
    answer_text: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_retrieve_ms: float,
    latency_answer_ms: float,
    model_calls: int,
    profile_sha256: str | None = None,
    include_content: bool = False,
) -> CycleTrace:
    return CycleTrace(
        item_id=item_id,
        query_id=query_id,
        t=t,
        evidence=context.evidence,
        context_tokens=context.tokens,
        context_sha256=sha256_text(context.text),
        context_truncated=context.truncated,
        boundary_index=context.boundary_index,
        answer_sha256=sha256_text(answer_text),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_retrieve_ms=latency_retrieve_ms,
        latency_answer_ms=latency_answer_ms,
        model_calls=model_calls,
        profile_sha256=profile_sha256,
        context_text=context.text if include_content else None,
        answer_text=answer_text if include_content else None,
    )


class TraceWriter:
    """Append-only JSONL trace. One file per run, alongside its results."""

    def __init__(self, path: Path, include_content: bool = False) -> None:
        self.path = Path(path)
        self.include_content = include_content
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._count = 0

    def write(self, entry: DepositTrace | CycleTrace) -> None:
        self._fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._count += 1

    def write_many(self, entries: Sequence[DepositTrace | CycleTrace]) -> None:
        for entry in entries:
            self.write(entry)

    @property
    def count(self) -> int:
        return self._count

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
