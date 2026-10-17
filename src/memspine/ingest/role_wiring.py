"""W2-03: wiring of LIB-13 into the write path (``write.role_policy_v2``).

* :func:`decide` runs the :class:`~memspine.ingest.roles.RolePolicy` and the echo guard for one raw turn and
  returns the tags the raw record carries (``derive_use:*``, ``echo``, ``echo_of:*``, ``assent_candidate``),
  its provenance class and a trust ceiling. Raw keeps every role (W-06 B); derivation reads the tags
  through ``derive.role_filter``.
* :func:`remember_last_read` keeps, per (namespace, session), what the latest read returned, so a turn
  that repeats it is caught even when the host dropped the marker (I-8 A).
* :class:`TypeRouter` (W-09) decides where a turn goes: unkeyed content stays raw (episodic, or resource for
  a document or asset); a keyed entity/attribute turn, a skill or a plan also gets a typed record whose
  parent is the raw turn.

Metrics are plain counters on the engine (``eng._role_metrics``); every decision point also taps
``write.role.*``, ``write.echo.*`` and ``write.route.*``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from memspine.config import constants
from memspine.ingest.roles import (
    EchoConfig,
    EchoVerdict,
    RecallMarker,
    RolePolicy,
    RoleTurn,
    Routing,
    detect_echo,
    route,
)
from memspine.observability import tap
from memspine.store.records import MemoryRecord

__all__ = [
    "PENDING_ROLE",
    "RoleDecision",
    "Route",
    "TypeRouter",
    "apply_role",
    "decide",
    "last_read_markers",
    "metrics_of",
    "policy_of",
    "remember_last_read",
]

_READ_KEEP = 20  # records of the last read kept for the overlap check
_READ_CHARS = 2000


def policy_of(eng: Any) -> RolePolicy:
    return RolePolicy(echo=EchoConfig(threshold=eng._config().write.echo_threshold))


def metrics_of(eng: Any) -> Counter[str]:
    m = getattr(eng, "_role_metrics", None)
    if m is None:
        m = Counter()
        eng._role_metrics = m
    return m


# ------------------------------------------------------------------------------------------ last read


def remember_last_read(
    eng: Any, ns: str, session_id: str | None, results: Sequence[tuple[MemoryRecord, float]]
) -> None:
    """Replace the session's last-read memory (ids, text, source family)."""
    store: dict[tuple[str, str], list[tuple[str, str, str]]] | None = getattr(
        eng, "_last_read", None
    )
    if store is None:
        store = {}
        eng._last_read = store
    rows = [
        (
            r.record_id,
            r.content[:_READ_CHARS],
            f"{r.provenance_class}:{r.axes.speaker or r.source.role}",
        )
        for r, _ in results[:_READ_KEEP]
        if r.content
    ]
    store[(ns, session_id or "")] = rows
    tap.event("write.echo.last_read", records=len(rows), session=session_id)


def last_read_markers(eng: Any, ns: str, session_id: str | None) -> list[RecallMarker]:
    rows = (getattr(eng, "_last_read", None) or {}).get((ns, session_id or ""), [])
    return [RecallMarker(id=i, text=t, family=f) for i, t, f in rows]


# ---------------------------------------------------------------------------------------------- decide


@dataclass(frozen=True)
class RoleDecision:
    role: str
    routing: Routing
    verdict: EchoVerdict
    tags: tuple[str, ...]
    src_class: str
    trust_cap: float


#: set around ``eng.write`` for one raw turn; ``ingest.write.write`` applies it to the record.
PENDING_ROLE: ContextVar[RoleDecision | None] = ContextVar("memspine_pending_role", default=None)


def _looks_like_recall(content: str) -> bool:
    from memspine.ingest.message_helpers import _looks_like_recall

    return _looks_like_recall(content)


def decide(eng: Any, ns: str, session_id: str | None, role: str, content: str) -> RoleDecision:
    policy = policy_of(eng)
    use_role = role if role in policy.rules else "external"
    markers = last_read_markers(eng, ns, session_id)
    verdict = detect_echo(content, markers, policy.echo)
    if not verdict.is_echo and role != "user" and _looks_like_recall(content):
        # the engine's own context wrappers (H21) count as a marker even with no recorded read
        verdict = EchoVerdict(kind="marked")
    routing = route(RoleTurn(role=use_role, content=content), policy, verdict)  # type: ignore[arg-type]
    tags = [f"{constants.ROLE_USE_TAG_PREFIX}{routing.derive_use}"]
    if routing.echo:
        tags.append(constants.ECHO_TAG)
        tags.extend(f"{constants.ECHO_OF_PREFIX}{i}" for i in routing.inherits_family_of)
    if routing.assent_candidate:
        tags.append(constants.ASSENT_CANDIDATE_TAG)
    user_cap = policy.rule("user").base_trust
    cap = (
        routing.base_trust
        if use_role in ("user", "operator")
        else min(routing.base_trust, user_cap)
    )
    m = metrics_of(eng)
    m[f"role.{use_role}"] += 1
    if routing.derive_use == "excluded":
        m["role.excluded"] += 1
    tap.event(
        "write.role.turn",
        role=role,
        derive_use=routing.derive_use,
        src_class=routing.src_class,
        trust_cap=cap,
        session=session_id,
    )
    if verdict.is_echo:
        m[f"echo.{verdict.kind}"] += 1
        m["echo.blocked" if routing.echo else "echo.assent_candidate"] += 1
        tap.event(
            "write.echo.detected",
            kind=verdict.kind,
            role=role,
            matched=list(verdict.matched_ids),
            scores=dict(verdict.scores),
            blocked=routing.echo,
            families=list(verdict.inherited_families),
        )
    return RoleDecision(use_role, routing, verdict, tuple(tags), routing.src_class, cap)


def apply_role(record: MemoryRecord, d: RoleDecision) -> MemoryRecord:
    """Class from the role; trust only ever lowered (assistant, tool and echoes never above user-stated)."""
    update: dict[str, Any] = {}
    if not record.source.parents:
        update["provenance_class"] = d.src_class
    if record.trust > d.trust_cap:
        update["trust"] = d.trust_cap
    return record.model_copy(update=update) if update else record


# ------------------------------------------------------------------------------------------ TypeRouter


@dataclass(frozen=True)
class Route:
    raw_type: str  # episodic | resource: the raw landing, always
    typed: str | None  # semantic | skill | plan when keyed, else None
    entity: str | None = None
    attribute: str | None = None
    text: str = ""
    reason: str = ""


_TYPED_KINDS = ("skill", "plan")


def _clean(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


class TypeRouter:
    """W-09: unkeyed content stays raw; keyed entity/attribute, skill and plan also get a typed write."""

    def route(self, turn: Mapping[str, Any], adapter: str = "messages") -> Route:
        raw = "resource" if adapter in ("document", "asset") else "episodic"
        content = str(turn.get("content", ""))
        kind = _clean(turn.get("memory_type"))
        if kind in _TYPED_KINDS:
            return Route(raw, kind, text=content, reason=f"kind:{kind}")
        entity, attribute = _clean(turn.get("entity")), _clean(turn.get("attribute"))
        if entity and attribute:
            value = _clean(turn.get("value"))
            text = f"{entity} {attribute}: {value}" if value else content
            return Route(raw, "semantic", entity, attribute, text, "keyed")
        return Route(raw, None, reason="unkeyed")
