"""Derive: association graph verbs (reflect, associate, related, link evolution) (F-15)."""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.config import constants
from memspine.exceptions import MemspineError
from memspine.memories.associative.evolution import propose_links
from memspine.observability.logging import EVENT_LINK, get_logger
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.store.scope import validate_namespace

_log = get_logger(__name__)


async def reflect(
    eng: Any,
    content: str,
    source_record_ids: list[str],
    namespace: str = "default",
    actor: str = "assistant",
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """Write a reflection derived from existing records (M13.7); see :meth:`_reflect`."""
    written = await eng._reflect(content, source_record_ids, namespace, actor, source)
    assert written is not None  # require_live is off
    return written


async def _reflect(
    eng: Any,
    content: str,
    source_record_ids: list[str],
    namespace: str = "default",
    actor: str = "assistant",
    source: SourceInfo | None = None,
    *,
    require_live: bool = False,
) -> MemoryRecord | None:
    """Write a reflection derived from existing records (M13.7).

    ``require_live`` (I72, the sleep-cycle deposit): when any evidence record is
    no longer live under the namespace lock the reflection is skipped (audited)
    and None is returned.


    Depth is computed from the fetched parents and hard-capped at 2;
    quarantined/deleted parents and parents outside ``namespace`` are
    refused (no laundering, no tenant crossing — E1). Reflection content
    is caller-authored, so it carries the caller's role (never a blanket
    "system") and its trust is capped at the least-trusted parent.
    """
    eng._require_started()
    if eng._reflective is None:
        raise MemspineError("reflective memory not enabled — set memories.reflective.enabled: true")
    ns = validate_namespace(namespace)
    views = await eng._parent_trust_cap(ns, source_record_ids)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if require_live and await eng._derived_parents_gone(
            ns, source_record_ids, "reflect_profile"
        ):
            return None
        return await eng._reflective.reflect(
            ns,
            content,
            source_record_ids,
            source=source or SourceInfo(role=actor, channel="reflection"),
            parent_views=views,
            cap_trust=eng._integrity().deposit_trust,
        )


async def associate(
    eng: Any,
    src_id: str,
    dst_id: str,
    namespace: str = "default",
    rel: str = "related",
    weight: float = 1.0,
) -> None:
    """Link two records (M13.6): a budget-checked LINK event through the
    door (ADR-015); the graph projection materializes the edge. Both
    records must live in ``namespace`` — cross-namespace links refused."""
    eng._require_started()
    if eng._associative is None:
        raise MemspineError(
            "associative memory not enabled — set memories.associative.enabled: true"
        )
    ns = validate_namespace(namespace)
    # Same one-writer-per-namespace unit as write(): the budget read and
    # the LINK append must not interleave with a concurrent linker.
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        await eng._associative.link(ns, src_id, dst_id, rel=rel, weight=weight)


async def related(
    eng: Any,
    record_id: str,
    namespace: str = "default",
    k: int = 10,
    strategy: str | None = None,
) -> list[MemoryRecord]:
    """Records associated with ``record_id`` over the link graph (D-40).
    ``strategy`` (E1: ``ppr`` default | ``bfs`` | ``rrf``) overrides
    ``memories.associative.policies.related.strategy``. Same E1 gate as
    ``search``: only ACTIVATED, never quarantined, never cross-namespace."""
    eng._require_started()
    if eng._associative is None:
        raise MemspineError(
            "associative memory not enabled — set memories.associative.enabled: true"
        )
    ns = validate_namespace(namespace)
    return eng._inflate_all(
        await eng._associative.related(ns, record_id, k=k, strategy=strategy), ns
    )


async def _evolve_links(eng: Any, ns: str, record: MemoryRecord) -> None:
    """Bounded A-MEM hook (D-42/ADR-015): after a non-quarantined semantic/
    episodic write, propose links to the vector neighbourhood. Best-effort:
    a failure is logged loudly (never raised) — an auto-link must not fail
    the write it decorates."""
    if (
        eng._associative is None
        or record.quarantined
        or record.memory_type not in ("semantic", "episodic")
    ):
        return
    assert eng._graph is not None and eng._storage is not None
    if eng._embedder is None or eng._vector is None:
        return
    try:
        [vector] = await eng._embedder.embed([record.content])
        hits = await eng._vector.query(
            ns, vector, embedder_id=eng._embedder.embedder_id, top_k=constants.SEARCH_TOP_K
        )
        created = await propose_links(
            namespace=ns,
            record=record,
            hits=hits,
            storage=eng._storage,
            graph=eng._graph,
            append_event=eng._append_and_project,
        )
        if created:
            _log.info(
                EVENT_LINK,
                namespace=ns,
                record_id=record.record_id,
                links=created,
                reason="evolution",
            )
    except Exception as exc:
        _log.warning(
            "associative.evolution_failed",
            namespace=ns,
            record_id=record.record_id,
            error=str(exc),
            error_kind=exc.__class__.__name__,
            exc_info=True,
        )
