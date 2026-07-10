"""FastAPI app over ONE Engine (D-06, ``[rest]``).

⚠️  NO AUTHENTICATION IN v0.1 (ADR-017 / ADR-016 open question 2, answered
"out of scope"). The caller's namespace comes from the ``X-Memspine-Namespace``
header (default ``"default"``) and is trusted verbatim: whoever can reach this
app can read and write EVERY namespace. Binding caller → namespace is the
DEPLOYER's job — put the app behind a reverse proxy / auth middleware and
override the :func:`resolve_namespace` dependency::

    app = create_app(engine)
    app.dependency_overrides[resolve_namespace] = my_authenticated_namespace

Never expose this app to an untrusted network without that seam filled.

Design notes:
- one Engine per app; its lifecycle (``start``/``stop``) is owned by the
  caller, not the app (documented on :func:`create_app`);
- responses are orjson (D-38) and reuse the universal ``MemoryRecord`` shape;
- errors map ``ConflictError``→409, ``MissingServiceError``→501,
  ``MemspineError``→400, anything else →500 with a generic body — stack
  traces and internal messages never leak on the unknown path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import ORJSONResponse

from memspine.config import constants
from memspine.core.audit import TaintReport
from memspine.core.namespace import validate_namespace
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.engine import Engine
from memspine.exceptions import ConflictError, MemspineError, MissingServiceError
from memspine.observability.logging import get_logger
from memspine.protocols.rest.models import (
    AssembleRequest,
    AssembleResponse,
    GrantRequest,
    GrantView,
    PlanRequest,
    PromoteRequest,
    ReflectRequest,
    RetrieveRequest,
    ScoredRecord,
    SearchRequest,
    SkillRequest,
    SubscriptionRequest,
    WatchRequest,
    WriteMessagesRequest,
    WriteRequest,
)

__all__ = ["build_app", "resolve_namespace"]

_log = get_logger(__name__)


async def resolve_namespace(
    x_memspine_namespace: Annotated[str, Header(alias="X-Memspine-Namespace")] = "default",
) -> str:
    """The caller→namespace binding seam (ADR-017). v0.1 trusts the header;
    deployers override this dependency with their auth middleware's verdict."""
    return validate_namespace(x_memspine_namespace)


Namespace = Annotated[str, Depends(resolve_namespace)]


def _error_response(status: int, exc: Exception) -> ORJSONResponse:
    return ORJSONResponse(
        status_code=status,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


def build_app(engine: Engine) -> FastAPI:
    app = FastAPI(
        title="memspine",
        summary="Cognitive-memory engine — REST protocol (D-06). "
        "NO AUTHN in v0.1: namespace binding is the deployer's job (ADR-017).",
        default_response_class=ORJSONResponse,
    )

    # ── request-body cap (SEC-M3/ADR-018): a cheap DoS guard ─────────────────
    # The no-authn app must not buffer an unbounded body. Reject anything over
    # the configured cap with 413 (Content-Length when present; otherwise a
    # streaming byte count so a lying/absent header cannot bypass it).

    @app.middleware("http")
    async def _limit_body_size(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cap = constants.REST_MAX_BODY_BYTES
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            return _error_response(413, MemspineError(f"request body exceeds {cap} bytes"))
        body = await request.body()
        if len(body) > cap:
            return _error_response(413, MemspineError(f"request body exceeds {cap} bytes"))
        return await call_next(request)

    # ── error mapping (never leak stack traces) ──────────────────────────────

    async def _conflict(_request: Request, exc: Exception) -> Response:
        # SF-6/ADR-018: typed 4xx map to a log line (M11 vocab), never the body.
        _log.info("rest.conflict", error=type(exc).__name__, detail=str(exc))
        return _error_response(409, exc)

    async def _missing_service(_request: Request, exc: Exception) -> Response:
        _log.warning("rest.missing_service", error=type(exc).__name__, detail=str(exc))
        return _error_response(501, exc)

    async def _memspine_error(_request: Request, exc: Exception) -> Response:
        _log.info("rest.bad_request", error=type(exc).__name__, detail=str(exc))
        return _error_response(400, exc)

    async def _unknown(_request: Request, exc: Exception) -> Response:
        _log.error("rest.internal_error", error=str(exc), exc_info=True)
        return ORJSONResponse(
            status_code=500, content={"error": "InternalServerError", "detail": "internal error"}
        )

    app.add_exception_handler(ConflictError, _conflict)
    app.add_exception_handler(MissingServiceError, _missing_service)
    app.add_exception_handler(MemspineError, _memspine_error)
    app.add_exception_handler(Exception, _unknown)

    # ── core verbs ───────────────────────────────────────────────────────────

    @app.post("/write")
    async def write(body: WriteRequest, ns: Namespace) -> MemoryRecord:
        # SEC-C1/ADR-018: REST is untrusted external input. Force the write onto
        # the "rest" channel (in TrustPolicy._EXTERNAL_CHANNELS) so TrustPolicy
        # caps trust at TRUST_RETRIEVED_CAP REGARDLESS of the caller-supplied
        # role — a caller cannot claim role="operator" to escalate trust or dodge
        # the firewall. The role is preserved for provenance only.
        source = body.source or SourceInfo(role=body.actor)
        source = source.model_copy(update={"channel": "rest"})
        return await engine.write(
            body.content,
            namespace=ns,
            memory_type=body.memory_type,
            source=source,
            pii_tier=body.pii_tier,
            actor=body.actor,
            entity=body.entity,
            attribute=body.attribute,
        )

    @app.post("/write_messages")
    async def write_messages(body: WriteMessagesRequest, ns: Namespace) -> list[MemoryRecord]:
        # C4: chat-transcript ingestion. Force channel="rest" (SEC-C1, same as
        # /write) so TrustPolicy caps each turn's trust regardless of the claimed
        # role — the per-turn role is preserved for provenance only.
        turns = [{"role": turn.role, "content": turn.content} for turn in body.messages]
        if body.as_episode:
            return await engine.write_episode(turns, namespace=ns, actor=body.actor, channel="rest")
        return await engine.write_messages(turns, namespace=ns, actor=body.actor, channel="rest")

    @app.post("/search")
    async def search(body: SearchRequest, ns: Namespace) -> list[ScoredRecord]:
        scored = await engine.search(body.query, namespace=ns, top_k=body.top_k)
        return [ScoredRecord(record=record, score=score) for record, score in scored]

    @app.post("/assemble")
    async def assemble(body: AssembleRequest, ns: Namespace) -> AssembleResponse:
        context = await engine.assemble(
            body.query, namespace=ns, budget_tokens=body.budget_tokens, top_k=body.top_k
        )
        return AssembleResponse(
            records=context.records,
            boundary_index=context.boundary_index,
            abstained=context.abstained,
            tokens_used=context.tokens_used,
        )

    @app.post("/retrieve")
    async def retrieve(body: RetrieveRequest, ns: Namespace) -> list[MemoryRecord]:
        return await engine.retrieve(namespace=ns, memory_type=body.memory_type)

    @app.delete("/records/{record_id}")
    async def forget(record_id: str, ns: Namespace, hard: bool = False) -> dict[str, Any]:
        await engine.forget(record_id, namespace=ns, hard=hard)
        return {"record_id": record_id, "forgotten": True, "hard": hard}

    @app.get("/describe")
    async def describe() -> dict[str, Any]:
        return engine.describe()

    # ── procedural (M13.4 / E6) ──────────────────────────────────────────────

    @app.post("/skills")
    async def add_skill(body: SkillRequest, ns: Namespace) -> MemoryRecord:
        return await engine.add_skill(body.content, body.name, namespace=ns, actor=body.actor)

    @app.post("/skills/{record_id}/promote")
    async def promote_skill(
        record_id: str, ns: Namespace, body: PromoteRequest | None = None
    ) -> MemoryRecord:
        dry_run_passed = body.dry_run_passed if body is not None else False
        return await engine.promote_skill(record_id, namespace=ns, dry_run_passed=dry_run_passed)

    @app.delete("/skills/{record_id}")
    async def deprecate_skill(record_id: str, ns: Namespace) -> MemoryRecord:
        return await engine.deprecate_skill(record_id, namespace=ns)

    @app.post("/plans")
    async def record_plan(body: PlanRequest, ns: Namespace) -> MemoryRecord:
        return await engine.record_plan(body.task, body.content, namespace=ns, actor=body.actor)

    @app.get("/plans/recall")
    async def recall_plan(
        task: str,
        ns: Namespace,
        min_similarity: float = constants.PLAN_RECALL_MIN_SIMILARITY,
    ) -> MemoryRecord | None:
        return await engine.recall_plan(task, namespace=ns, min_similarity=min_similarity)

    # ── reflective (M13.7) ───────────────────────────────────────────────────

    @app.post("/reflect")
    async def reflect(body: ReflectRequest, ns: Namespace) -> MemoryRecord:
        return await engine.reflect(
            body.content, body.source_record_ids, namespace=ns, actor=body.actor
        )

    # ── prospective (M13.8) ──────────────────────────────────────────────────

    @app.post("/watches")
    async def watch(body: WatchRequest, ns: Namespace) -> MemoryRecord:
        return await engine.watch(
            body.content,
            namespace=ns,
            due_at=body.due_at,
            entity=body.entity,
            attribute=body.attribute,
            actor=body.actor,
        )

    @app.get("/watches/due")
    async def due(ns: Namespace, now: datetime | None = None) -> list[MemoryRecord]:
        return await engine.due(namespace=ns, now=now)

    @app.post("/watches/{record_id}/ack")
    async def acknowledge_watch(record_id: str, ns: Namespace) -> MemoryRecord:
        return await engine.acknowledge_watch(record_id, namespace=ns)

    # ── shared (R2) ──────────────────────────────────────────────────────────

    @app.post("/grants")
    async def grant(body: GrantRequest, ns: Namespace) -> MemoryRecord:
        return await engine.grant(
            body.to_namespace, namespace=ns, memory_types=body.memory_types, actor=body.actor
        )

    @app.delete("/grants")
    async def revoke(to_namespace: str, ns: Namespace) -> MemoryRecord:
        return await engine.revoke(to_namespace, namespace=ns)

    @app.get("/shared_search")
    async def shared_search(
        query: str, ns: Namespace, top_k: int = constants.SEARCH_TOP_K
    ) -> list[ScoredRecord]:
        scored = await engine.shared_search(query, namespace=ns, top_k=top_k)
        return [ScoredRecord(record=record, score=score) for record, score in scored]

    @app.get("/grants")
    async def grants_from(ns: Namespace) -> list[GrantView]:
        """Live grants ``ns`` has issued (operator listing surface, ADR-016).
        Scoped to the caller — never another namespace's grants."""
        grants = await engine.grants_from(namespace=ns)
        return [
            GrantView(
                grantor=grant.grantor,
                grantee=grant.grantee,
                memory_types=sorted(grant.memory_types) if grant.memory_types is not None else None,
                record_id=grant.record_id,
            )
            for grant in grants
        ]

    @app.post("/subscriptions")
    async def subscribe(body: SubscriptionRequest, ns: Namespace) -> MemoryRecord:
        return await engine.subscribe(body.query, namespace=ns, actor=body.actor)

    @app.get("/subscriptions")
    async def subscriptions(ns: Namespace) -> list[MemoryRecord]:
        return await engine.subscriptions(namespace=ns)

    # ── maintenance + governance ─────────────────────────────────────────────
    # ⚠️  ADR-018: /sleep, /rebuild and /audit/taint are engine-global or
    # cross-cutting. Behind the no-authn seam they MUST sit on an internal-only
    # network boundary — never expose them to tenant callers.

    @app.post("/sleep")
    async def sleep() -> dict[str, dict[str, Any]]:
        return await engine.sleep()

    @app.post("/rebuild")
    async def rebuild() -> dict[str, int]:
        return await engine.rebuild()

    @app.get("/audit/taint/{record_id}")
    async def audit_taint(record_id: str, ns: Namespace) -> TaintReport:
        # SEC-C3/ADR-018: scope the seed to the caller's namespace so a leaked
        # record_id cannot be used to walk another tenant's derivation trail.
        return await engine.audit_taint(record_id, namespace=ns)

    return app
