"""Memory: shared moved out of the engine (F-12)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from memspine.config import constants
from memspine.exceptions import MemspineError
from memspine.memories.shared.grants import Grant, SharedMemory
from memspine.memories.shared.subscriptions import make_subscription_record
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.store.scope import validate_namespace
from memspine.trust.privacy import read_scope


async def _grants_cached(
    eng: Any, reader: str, cache: dict[str, Mapping[str, frozenset[str] | None]]
) -> Mapping[str, frozenset[str] | None]:
    """``reader``'s live grants, read once per caller-held ``cache``."""
    if reader not in cache:
        cache[reader] = await eng._shared.grants_to(reader) if eng._shared is not None else {}
    return cache[reader]


async def grant(
    eng: Any,
    to_namespace: str,
    namespace: str = "default",
    memory_types: list[str] | None = None,
    actor: str = "user",
) -> MemoryRecord:
    """Grant ``to_namespace`` read access to ``namespace``'s records (R2),
    optionally scoped to ``memory_types``. The grant record rides the log
    (D0.1); enforcement lives in ``core.namespace.grant_allows``. Grant
    content is engine-built canonical JSON — the stated firewall
    exemption shared with prompt sync (ADR-014 §2/ADR-016)."""
    eng._require_started()
    shared = eng._require_shared()
    grantor = validate_namespace(namespace)
    grantee = validate_namespace(to_namespace)
    # Write lock on the GRANTOR namespace: the read-diff-append unit must
    # not interleave with a concurrent grant/revoke or forget cascade.
    async with eng._write_locks.setdefault(grantor, asyncio.Lock()):
        record = await shared.grant(
            grantor,
            grantee,
            memory_types=memory_types,
            source=SourceInfo(role=actor, channel="grant"),
        )
    await eng._audit_action(
        "grant",
        grantor,
        [record.record_id],
        actor=actor,
        reason=None,
        grantee=grantee,
        memory_types=sorted(memory_types) if memory_types is not None else None,
    )
    return record


async def revoke(
    eng: Any, to_namespace: str, namespace: str = "default", *, actor: str = "user"
) -> MemoryRecord:
    """Revoke ``to_namespace``'s read access to ``namespace`` (R2): the
    grant record is archived via a delta event; raises when no grant is
    live (a typo'd grantee must not read as success). ``actor`` goes into a
    ``memory.audit`` event under ``audit.actions``."""
    eng._require_started()
    shared = eng._require_shared()
    grantor = validate_namespace(namespace)
    grantee = validate_namespace(to_namespace)
    async with eng._write_locks.setdefault(grantor, asyncio.Lock()):
        record = await shared.revoke(grantor, grantee)
    await eng._audit_action(
        "revoke", grantor, [record.record_id], actor=actor, reason=None, grantee=grantee
    )
    return record


async def shared_search(
    eng: Any,
    query: str,
    namespace: str = "default",
    top_k: int = constants.SEARCH_TOP_K,
    session_id: str | None = None,
    ledger_id: str | None = None,
    purpose: str | None = None,
) -> list[tuple[MemoryRecord, float]]:
    """Own-namespace search plus granted foreign results (see :meth:`_shared_search`).

    ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``
    for own and foreign records alike."""
    session_id = ledger_id if ledger_id is not None else session_id  # I9
    with read_scope(purpose) as outer:
        results = await eng._shared_search(query, namespace, top_k, session_id)
        results = [pair for pair in results if eng._consent_ok(pair[0])]
        if outer:
            ids = [r.record_id for r, _ in results]
            await eng._audit_read("shared_search", namespace, ids)
    return results


async def subscribe(
    eng: Any,
    query: str,
    namespace: str = "default",
    actor: str = "user",
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """Store a standing query (ADR-016, v0.1 minimal). Caller free text —
    it enters through the firewall gate like any write. Pull-based: feed
    ``record.content`` to ``shared_search`` yourself; push delivery is
    deferred to the taskiq build."""
    storage = eng._require_started()
    eng._require_shared()
    ns = validate_namespace(namespace)
    record = make_subscription_record(ns, query, source=source or SourceInfo(role=actor))
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        return await eng._write_locked(storage, ns, record, "shared", actor)


async def subscriptions(eng: Any, namespace: str = "default") -> list[MemoryRecord]:
    """Live standing-query records in ``namespace`` (ADR-016)."""
    eng._require_started()
    shared = eng._require_shared()
    ns = validate_namespace(namespace)
    return eng._inflate_all(await shared.subscriptions(ns), ns)


async def grants_from(eng: Any, namespace: str = "default") -> list[Grant]:
    """The live grants ``namespace`` has issued (operator listing surface,
    R2/ADR-016). Scoped to the grantor — a caller lists only its OWN
    grants, never another namespace's."""
    eng._require_started()
    shared = eng._require_shared()
    grantor = validate_namespace(namespace)
    return await shared.grants_from(grantor)


def _require_shared(eng: Any) -> SharedMemory:
    if eng._shared is None:
        raise MemspineError("shared memory not enabled — set memories.shared.enabled: true")
    return eng._shared
