"""The scoped handle ``Memory`` and the scoped views (RESTRUCTURE section 1, G-16 D, S3-1, ER-29).

``Memory(engine, scope)`` fixes the tenant, the owner, the principal, the purpose and the viewer
once; every verb then delegates to the existing engine method with the namespace (and viewer,
purpose) filled in. The module-level functions are the engine's scoped views (``brief``,
``conversation`` ...) moved out of the facade (F-13a); the engine keeps one-line delegations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memspine.compose.pack import estimate_tokens
from memspine.config import constants
from memspine.core.escaping import escape_markers
from memspine.exceptions import MemspineError
from memspine.read.pipeline.scope_guard import declare_multi_party, viewer_required
from memspine.read.tiers import ReadRequestError
from memspine.schemas.durable import OwnerScope
from memspine.store.records import MemoryRecord, PiiTier, RecordStatus, chrono_key
from memspine.store.scope import validate_namespace

if TYPE_CHECKING:
    from memspine.api.engine import Engine


async def rating_profile(eng: Any, namespace: str = "default") -> dict[str, dict[str, float]]:
    """G12 (plan v3.2, LaMP / MemoryCD): the user's behaviour profile from logged
    records tagged ``rating:<number>`` (and ``domain:<name>``; untagged -> ``all``):
    per domain the mean, the standard deviation and the count of ratings. A
    recommender reads the mean as the user's level and the deviation as how
    selective they are. Read-only, no model."""
    ns = validate_namespace(namespace)
    values: dict[str, list[float]] = {}
    for record in await eng._records(ns):
        if record.status is not RecordStatus.ACTIVATED or record.quarantined:
            continue
        ratings = [t.split(":", 1)[1] for t in record.tags if t.startswith("rating:")]
        if not ratings:
            continue
        try:
            rating = float(ratings[0])
        except ValueError:
            continue
        domains = [t.split(":", 1)[1] for t in record.tags if t.startswith("domain:")]
        for domain in domains or ["all"]:
            values.setdefault(domain, []).append(rating)
    out: dict[str, dict[str, float]] = {}
    for domain, xs in sorted(values.items()):
        mean = sum(xs) / len(xs)
        std = (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5
        out[domain] = {"mean": round(mean, 4), "std": round(std, 4), "n": float(len(xs))}
    return out


async def brief(
    eng: Any,
    namespace: str = "default",
    budget_tokens: int = constants.ASSEMBLE_BUDGET_TOKENS,
    *,
    summaries: int = 3,
    recent_turns: int = 6,
) -> list[MemoryRecord]:
    """G-20 (SimpleMem session-start injection): context for the start of a new
    session, with no question. In order, while they fit ``budget_tokens``:
    - the profile block (``read.profile_slots_header``), when on and known;
    - the newest session summaries (``session_summary`` / consolidation records);
    - the last ``recent_turns`` episodic turns, oldest first.
    Every record passes the read gates (status, quarantine, consent, trust), and the
    blocks carry their own markers. No model call."""
    ns = validate_namespace(namespace)
    live = [r for r in await eng._require_started().list_records(ns) if eng._context_eligible(r)]
    blocks: list[MemoryRecord] = []
    used = 0
    profile = await eng._slots_section(ns, "", budget_tokens)
    if profile is not None:
        blocks.append(profile)
        used += estimate_tokens(profile.content)

    def block(marker: str, records: list[MemoryRecord]) -> MemoryRecord | None:
        nonlocal used
        lines: list[str] = []
        kept: list[MemoryRecord] = []
        for record in records:
            line = f"- {escape_markers(' '.join(record.content.split()))}"
            cost = estimate_tokens("\n".join([marker, *lines, line]))
            if used + cost > budget_tokens:
                break
            lines.append(line)
            kept.append(record)
        if not kept:
            return None
        used += estimate_tokens("\n".join([marker, *lines]))
        lead = eng._lead_record(ns, "\n".join([marker, *lines]), kept)
        return lead.model_copy(update={"tags": [constants.LEAD_TAG, constants.BRIEF_TAG]})  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    summaries_found = sorted(
        (r for r in live if r.source.channel == "consolidation"), key=chrono_key, reverse=True
    )[:summaries]
    turns = (
        sorted((r for r in live if r.memory_type == "episodic"), key=chrono_key)[-recent_turns:]
        if recent_turns > 0
        else []
    )
    for marker, records in (
        (constants.BRIEF_SUMMARIES_MARKER, summaries_found),
        (constants.BRIEF_RECENT_MARKER, turns),
    ):
        made = block(marker, records)
        if made is not None:
            blocks.append(made)
    return blocks


async def namespace_stats(eng: Any, namespace: str = "default") -> dict[str, Any]:
    """I5 (isolation review 2026-10-08): how big one user's memory is. Counts of
    live records by memory type and status, distinct conversations, quarantined
    and erased records, an estimate of stored tokens, and the time span covered.
    Read-only; no model call."""
    ns = validate_namespace(namespace)
    records = await eng._require_started().list_records(ns)
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    sessions: set[str] = set()
    tokens = 0
    for r in records:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        if r.status is not RecordStatus.ACTIVATED:
            continue
        by_type[r.memory_type] = by_type.get(r.memory_type, 0) + 1
        if r.source.message_id:
            sessions.add(r.source.message_id)
        tokens += estimate_tokens(r.content)
    live = [r for r in records if r.status is RecordStatus.ACTIVATED]
    return {
        "namespace": ns,
        "records": len(records),
        "live": len(live),
        "by_type": dict(sorted(by_type.items())),
        "by_status": dict(sorted(by_status.items())),
        "conversations": len(sessions),
        "quarantined": sum(1 for r in live if r.quarantined),
        "tokens_estimate": tokens,
        "first": min((r.valid_from for r in live), default=None),
        "last": max((r.valid_from for r in live), default=None),
    }


async def conversation(
    eng: Any,
    namespace: str = "default",
    session_id: str = "",
    roles: Sequence[str] | None = None,
) -> list[MemoryRecord]:
    """I6: the live turns of one conversation (``ingest_messages(session_id=...)``),
    in time order, read through the indexed ``session_key`` column; ``roles`` keeps
    only some speakers. Passes the same context gates as a read (erased, quarantined
    or untrusted turns are left out)."""
    ns = validate_namespace(namespace)
    storage = eng._require_started()
    listed = await storage.list_session_records(ns, session_id, roles)
    return sorted((r for r in listed if eng._context_eligible(r)), key=chrono_key)


async def session_memories(
    eng: Any, namespace: str = "default", session_record_ids: Sequence[str] = ()
) -> list[MemoryRecord]:
    """N19 (plan v3.2, HaluMem ``get_session_memories``): the live derived records
    (mined facts, summaries, reflections, cards) whose ``source.parents`` include
    any of ``session_record_ids`` — what memory extracted from those turns, oldest
    first. Read-only; the raw turns themselves are not listed."""
    ns = validate_namespace(namespace)
    wanted = set(session_record_ids)
    if not wanted:
        return []
    out = [
        record
        for record in await eng._records(ns)
        if record.status is RecordStatus.ACTIVATED
        and not record.quarantined
        and record.record_id not in wanted
        and wanted & set(record.source.parents)
    ]
    return sorted(out, key=chrono_key)


async def ingest_data(
    eng: Any,
    path: str | Path,
    namespace: str = "default",
    pii_tier: PiiTier = PiiTier.NONE,
) -> list[MemoryRecord]:
    """Ingest a document (D-29): extract → chunk → resource records."""
    eng._require_started()
    if eng._resource is None:
        raise MemspineError("resource memory not enabled ? set memories.resource.enabled: true")
    from memspine.ingest import gateway as intake_gateway

    ns = validate_namespace(namespace)
    return await intake_gateway.ingest_data(eng, path, ns, pii_tier)

_BOUND_KEYS = frozenset({"namespace", "viewer", "purpose"})


def _namespace_of(scope: OwnerScope | str) -> str:
    """Engine namespace of a scope: ``tenant/owner`` for an :class:`OwnerScope`, else a path."""
    if isinstance(scope, str):
        return validate_namespace(scope)
    return validate_namespace(f"{scope.tenant}/{scope.owner}")


class Memory:
    """A handle on one scope: tenant, owner, principal, purpose and viewer are bound at creation.

    A group-owned scope (or ``multi_party=True``) is a multi-party read and needs a ``viewer``
    bound to the ``principal``; a handle without one is rejected at construction, like
    :class:`ReadRequest`. A verb cannot be given ``namespace``, ``viewer`` or ``purpose``: the
    handle owns them.
    """

    __slots__ = ("_engine", "multi_party", "namespace", "principal", "purpose", "scope", "viewer")

    def __init__(
        self,
        engine: Engine,
        scope: OwnerScope | str,
        *,
        principal: str | None = None,
        purpose: str | None = None,
        viewer: str | None = None,
        multi_party: bool = False,
    ) -> None:
        group = isinstance(scope, OwnerScope) and scope.owner_kind == "group"
        multi = bool(multi_party) or group
        if viewer is not None:
            viewer = viewer.strip() or None
        if viewer is not None:
            if principal is None:
                raise ReadRequestError("a viewer needs an authenticated principal")
            if viewer != principal:
                raise ReadRequestError("the viewer must be the authenticated principal")
        if multi and viewer is None:
            raise ReadRequestError("a multi-party read requires a viewer bound to the principal")
        if principal is not None and not multi:
            _check_owner_bound(engine, _namespace_of(scope), principal)
        self._engine = engine
        self.scope = scope
        self.namespace = _namespace_of(scope)
        self.principal = principal
        self.purpose = purpose
        self.viewer = viewer
        self.multi_party = multi
        if multi:  # F-16c: the engine's viewer guard treats this namespace as multi-party
            declare_multi_party(engine, self.namespace)

    def __repr__(self) -> str:
        return (
            f"Memory(namespace={self.namespace!r}, principal={self.principal!r}, "
            f"viewer={self.viewer!r})"
        )

    @staticmethod
    def _check(kwargs: Mapping[str, Any]) -> None:
        clash = _BOUND_KEYS & set(kwargs)
        if clash:
            raise TypeError(f"{sorted(clash)} are fixed by the handle's scope and cannot be passed")

    def _viewer_kw(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """``kwargs`` plus the viewer, for the verbs that did not take one before F-16c. The viewer
        is passed only under ``isolation.viewer_required``, so a flag-off handle reads as before."""
        self._check(kwargs)
        if self.viewer is not None and viewer_required(self._engine):
            return {**kwargs, "viewer": self.viewer}
        return dict(kwargs)

    def _read_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        self._check(kwargs)
        out = dict(kwargs)
        out["namespace"] = self.namespace
        if self.viewer is not None:
            out["viewer"] = self.viewer
        if self.purpose is not None:
            out["purpose"] = self.purpose
        return out

    async def remember(self, content: str, **kwargs: Any) -> Any:
        """Write one memory into this scope (``Engine.write``)."""
        self._check(kwargs)
        return await self._engine.write(content, namespace=self.namespace, **kwargs)

    async def recall(self, query: str, **kwargs: Any) -> Any:
        """Scored retrieval inside this scope (``Engine.search``)."""
        return await self._engine.search(query, **self._read_kwargs(kwargs))

    async def read(self, query: str, **kwargs: Any) -> Any:
        """Mode-routed read inside this scope (``Engine.read``)."""
        return await self._engine.read(query, **self._read_kwargs(kwargs))

    async def compose(self, query: str, budget_tokens: int | None = None, **kwargs: Any) -> Any:
        """Retrieve and assemble a context inside this scope (``Engine.assemble``)."""
        if budget_tokens is not None:
            kwargs["budget_tokens"] = budget_tokens
        return await self._engine.assemble(query, **self._read_kwargs(kwargs))

    async def correct(self, target: Any, new_value: str, **kwargs: Any) -> Any:
        """Correct a memory in this scope (``Engine.correct``)."""
        self._check(kwargs)
        return await self._engine.correct(target, new_value, namespace=self.namespace, **kwargs)

    async def forget(self, record_id: str, *, hard: bool = False, **kwargs: Any) -> None:
        """Forget one memory in this scope (``Engine.forget``)."""
        self._check(kwargs)
        await self._engine.forget(record_id, namespace=self.namespace, hard=hard, **kwargs)

    async def sleep(self) -> Any:
        """Run the maintenance cycle (``Engine.sleep``; engine-wide, not scope-wide). A
        ``SleepHandle`` under ``offline.sleep_v2``."""
        return await self._engine.sleep()

    def cost_report(self) -> dict[str, Any]:
        """Cost ledger of this scope's writes (``Engine.cost_report``; ``economy.ledger_v2``)."""
        return self._engine.cost_report(self.namespace)

    async def resume_deferred(self) -> int:
        """Derive this scope's budget-paused windows (``Engine.resume_deferred``)."""
        return await self._engine.resume_deferred(self.namespace)

    async def brief(self, **kwargs: Any) -> Any:
        """The scope's standing brief (``Engine.brief``)."""
        return await self._engine.brief(self.namespace, **self._viewer_kw(kwargs))

    async def conversation(self, session_id: str, **kwargs: Any) -> Any:
        """One session's turns in this scope (``Engine.conversation``)."""
        return await self._engine.conversation(
            self.namespace, session_id, **self._viewer_kw(kwargs)
        )

    async def stats(self) -> dict[str, Any]:
        """Counts for this scope's namespace (``Engine.namespace_stats``)."""
        return await self._engine.namespace_stats(self.namespace, **self._viewer_kw({}))

    # ── verbs the remote protocols (REST, MCP, agent tools) call; scope filled in (F-13b) ──

    async def _scoped(self, verb: str, *args: Any, **kwargs: Any) -> Any:
        """Call engine ``verb`` with this scope's namespace (the handle owns it)."""
        self._check(kwargs)
        return await getattr(self._engine, verb)(*args, namespace=self.namespace, **kwargs)

    async def remember_ex(self, content: str, **kwargs: Any) -> Any:
        """Write one memory and return the outcome (``Engine.write_ex``)."""
        return await self._scoped("write_ex", content, **kwargs)

    async def ingest_messages(self, turns: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        """Write chat turns (``Engine.ingest_messages``)."""
        return await self._scoped("ingest_messages", turns, **kwargs)

    async def write_episode(self, turns: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        """Write chat turns as one episode (``Engine.write_episode``)."""
        return await self._scoped("write_episode", turns, **kwargs)

    async def retrieve(self, **kwargs: Any) -> Any:
        """List records of one type (``Engine.retrieve``); the handle's purpose applies."""
        out = self._viewer_kw(kwargs)  # checks the bound keys before the purpose is added
        if self.purpose is not None:
            out["purpose"] = self.purpose
        return await self._engine.retrieve(namespace=self.namespace, **out)

    async def feedback(self, record_id: str, signal: Any, **kwargs: Any) -> Any:
        """Utility feedback on one record (``Engine.feedback``)."""
        return await self._scoped("feedback", record_id, signal, **kwargs)

    async def list_quarantined(self) -> Any:
        """Records held by the firewall in this scope (``Engine.list_quarantined``)."""
        return await self._scoped("list_quarantined")

    async def approve_quarantined(self, record_id: str, **kwargs: Any) -> Any:
        """Release a held record (``Engine.approve_quarantined``)."""
        return await self._scoped("approve_quarantined", record_id, **kwargs)

    async def reject_quarantined(self, record_id: str, **kwargs: Any) -> Any:
        """Reject a held record (``Engine.reject_quarantined``)."""
        return await self._scoped("reject_quarantined", record_id, **kwargs)

    async def add_skill(self, content: str, name: str, **kwargs: Any) -> Any:
        """Add a procedural skill (``Engine.add_skill``)."""
        return await self._scoped("add_skill", content, name, **kwargs)

    async def promote_skill(self, record_id: str, **kwargs: Any) -> Any:
        """Promote a skill (``Engine.promote_skill``)."""
        return await self._scoped("promote_skill", record_id, **kwargs)

    async def deprecate_skill(self, record_id: str) -> Any:
        """Deprecate a skill (``Engine.deprecate_skill``)."""
        return await self._scoped("deprecate_skill", record_id)

    async def record_plan(self, task: str, content: str, **kwargs: Any) -> Any:
        """Record a plan (``Engine.record_plan``)."""
        return await self._scoped("record_plan", task, content, **kwargs)

    async def recall_plan(self, task: str, **kwargs: Any) -> Any:
        """Recall the closest plan (``Engine.recall_plan``)."""
        return await self._scoped("recall_plan", task, **kwargs)

    async def reflect(self, content: str, source_record_ids: Sequence[str], **kwargs: Any) -> Any:
        """Write a reflection (``Engine.reflect``)."""
        return await self._scoped("reflect", content, source_record_ids, **kwargs)

    async def watch(self, content: str, **kwargs: Any) -> Any:
        """Register a prospective watch (``Engine.watch``)."""
        return await self._scoped("watch", content, **kwargs)

    async def due(self, **kwargs: Any) -> Any:
        """Watches that are due (``Engine.due``)."""
        return await self._scoped("due", **kwargs)

    async def acknowledge_watch(self, record_id: str) -> Any:
        """Acknowledge a watch (``Engine.acknowledge_watch``)."""
        return await self._scoped("acknowledge_watch", record_id)

    async def grant(self, to_namespace: str, **kwargs: Any) -> Any:
        """Grant another namespace read access (``Engine.grant``)."""
        return await self._scoped("grant", to_namespace, **kwargs)

    async def revoke(self, to_namespace: str, **kwargs: Any) -> Any:
        """Revoke a grant (``Engine.revoke``)."""
        return await self._scoped("revoke", to_namespace, **kwargs)

    async def grants_from(self) -> Any:
        """Live grants this scope has issued (``Engine.grants_from``)."""
        return await self._scoped("grants_from")

    async def shared_search(self, query: str, **kwargs: Any) -> Any:
        """Search what other namespaces shared with this one (``Engine.shared_search``)."""
        self._check(kwargs)
        return await self._engine.shared_search(
            query, namespace=self.namespace, **self._viewer_kw(kwargs)
        )

    async def subscribe(self, query: str, **kwargs: Any) -> Any:
        """Subscribe to a query (``Engine.subscribe``)."""
        return await self._scoped("subscribe", query, **kwargs)

    async def subscriptions(self) -> Any:
        """This scope's subscriptions (``Engine.subscriptions``)."""
        return await self._scoped("subscriptions")

    async def export(self, **kwargs: Any) -> Any:
        """Subject-access export, JSONL lines (``Engine.export``)."""
        self._check(kwargs)
        return await self._engine.export(self.namespace, **kwargs)

    async def audit_taint(self, record_id: str) -> Any:
        """Taint report seeded in this scope (``Engine.audit_taint``)."""
        return await self._scoped("audit_taint", record_id)

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        """One record of this scope by id, else None (a foreign id looks missing)."""
        return await self._engine.get_record(
            record_id, namespace=self.namespace, **self._viewer_kw({})
        )

    async def list_records(self, memory_type: str | None = None) -> list[MemoryRecord]:
        """All stored records of this scope, optionally of one memory type."""
        return await self._engine.list_records(self.namespace, memory_type, **self._viewer_kw({}))

    async def audit_action(self, action: str, record_ids: list[str], **kwargs: Any) -> None:
        """Append a chained audit event for this scope (no-op unless ``audit.actions`` is on)."""
        await self._engine._audit_action(action, self.namespace, record_ids, **kwargs)

    async def rebuild(self) -> dict[str, int]:
        """Rebuild the derived indexes (``Engine.rebuild``; engine-wide)."""
        return await self._engine.rebuild()

    def describe(self) -> dict[str, Any]:
        """The engine's self-description (``Engine.describe``; engine-wide)."""
        return self._engine.describe()


def _check_owner_bound(engine: Engine, namespace: str, principal: str) -> None:
    """``isolation.owner_bound_handles``: a single-owner handle's principal owns its namespace."""
    isolation = getattr(engine._config(), "isolation", None)  # stub engines carry no config
    if not getattr(isolation, "owner_bound_handles", False):
        return
    from memspine.observability import tap
    from memspine.scope import Scope, ScopeRefused

    owner = Scope.from_namespace(namespace).owner
    if owner and owner != principal.strip():
        if tap.enabled():
            tap.event("scope.refused", reason="cross_owner", site="handle")
        raise ScopeRefused("cross_owner", "this handle's principal does not own the namespace")
