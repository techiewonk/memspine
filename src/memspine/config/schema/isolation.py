"""Isolation config (F-16c; decisions M-05, G-12): scope keys and the mandatory viewer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IsolationConfig(BaseModel):
    """Tenant + owner scope in every key, and a viewer on every multi-party read (all off)."""

    model_config = ConfigDict(extra="forbid")

    #: F-16c / M-05: build the read-cache key and the extracted-graph edge key from the typed
    #: ``Scope`` (tenant + owner; the session is provenance, never part of a key). Off keeps
    #: today's keys (the graph key changes, so edges extracted with it on do not merge with
    #: older ones).
    scope_keys_v2: bool = False
    #: F-16c / G-12 B: a read of a multi-party namespace (listed below, declared by a
    #: ``Memory(..., multi_party=True)`` handle, or holding records with several participants or a
    #: visibility tag) without a ``viewer`` is refused with reason ``viewer_required``, on every
    #: engine read verb (search, read, assemble, retrieve, shared_search, get_record, brief,
    #: conversation, session_memories, timeline, sessions, namespace_stats). Off = not refused.
    viewer_required: bool = False
    #: F-16c: namespaces (and everything under them) that are multi-party whatever their
    #: records say.
    multi_party_namespaces: list[str] = Field(default_factory=list)
    #: Owner-bound handles (owner decision 2026-10-13, coordinator default): a single-owner
    #: ``Memory`` handle opened with an authenticated ``principal`` must belong to that principal: the
    #: namespace's owner segment (``tenant/owner``) must equal the principal. Reading someone else's
    #: memory goes through grants (``shared_search``, T5-01), never by naming their namespace. A
    #: tenant-level namespace (no owner segment) and multi-party / group handles are not affected;
    #: a handle with no principal (in-process library use) is not affected. Refused with reason
    #: ``cross_owner``. Off = today's behaviour (the namespace alone decides).
    owner_bound_handles: bool = False
