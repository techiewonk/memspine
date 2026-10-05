"""FastAPI app over ONE Engine (D-06, ``[rest]``).

#51: ``rest.auth.mode`` (``api_key`` | ``oidc_jwt``) turns on the reference auth
middleware in :mod:`memspine.protocols.rest.auth` (ADR-041): it binds a principal
and its allowed namespaces to each request and guards the admin routes. It is a
reference, not a production auth plane. The rest of this note describes the
default, ``mode: none``.

⚠️  NO AUTHENTICATION BY DEFAULT (ADR-017 / ADR-016 open question 2, answered
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
from memspine.config.schema import RestConfig
from memspine.core.audit import TaintReport
from memspine.core.namespace import validate_namespace
from memspine.core.privacy import principal_scope
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.engine import Engine
from memspine.exceptions import ConflictError, MemspineError, MissingServiceError
from memspine.observability.logging import get_logger
from memspine.protocols.rest.auth import (
    Authenticator,
    AuthError,
    Principal,
    RateLimiter,
    is_admin_path,
)
from memspine.protocols.rest.models import (
    AssembleRequest,
    AssembleResponse,
    CorrectRequest,
    GrantRequest,
    GrantView,
    PlanRequest,
    PromoteRequest,
    QuarantineDecision,
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


class _AuthState:
    """#51: the auth middleware's authenticator and rate limiter, built from
    ``rest`` config once the engine is started (or from an explicit config)."""

    def __init__(self, engine: Engine, rest: RestConfig | None) -> None:
        self._engine = engine
        self._rest = rest
        self.authenticator: Authenticator | None = None
        self.limiter: RateLimiter | None = None

    def ready(self) -> Authenticator:
        if self.authenticator is None:
            if self._rest is None and not self._engine.is_started:
                # Not started yet: no config to read. Pass through for now (the
                # verbs themselves refuse an unstarted engine); decide on start.
                return Authenticator(RestConfig())
            rest = self._rest if self._rest is not None else self._engine.config.rest
            self.authenticator = Authenticator(rest)
            self.limiter = RateLimiter(rest.rate_limit) if rest.rate_limit else None
        return self.authenticator


def _actor(request: Request, claimed: str) -> str:
    """The authenticated principal when auth is on, else the caller's claim."""
    principal = getattr(request.state, "principal", None)
    return principal.name if isinstance(principal, Principal) else claimed


def build_app(engine: Engine, rest: RestConfig | None = None) -> FastAPI:
    """The app over ``engine``. ``rest`` overrides the engine's ``rest`` config
    (auth mode, API keys, rate limit); None reads it from the started engine."""
    auth_state = _AuthState(engine, rest)
    if rest is not None or engine.is_started:
        auth_state.ready()  # fail fast on a bad auth config (e.g. pyjwt missing)
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

    # ── #51 reference auth middleware (ADR-041) ──────────────────────────────
    # Registered after the body cap, so it runs first: unauthenticated callers
    # never get their body buffered. ``mode: none`` without a rate limit passes
    # every request through untouched (the default app is unchanged).

    @app.middleware("http")
    async def _authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        authenticator = auth_state.ready()
        limiter = auth_state.limiter
        if not authenticator.enabled and limiter is None:
            return await call_next(request)
        principal: Principal | None = None
        if authenticator.enabled:
            try:
                principal = authenticator.authenticate(request.headers)
                if is_admin_path(request.url.path) and not principal.admin:
                    raise AuthError(403, "this route needs the admin role")
                namespace = request.headers.get("x-memspine-namespace", "default")
                if not principal.may_use(namespace):
                    raise AuthError(403, f"principal may not use namespace {namespace!r}")
            except AuthError as exc:
                _log.info("rest.auth_denied", status=exc.status, path=request.url.path)
                return _error_response(exc.status, exc)
            request.state.principal = principal
        if limiter is not None:
            key = principal.name if principal else (request.client.host if request.client else "")
            if not limiter.allow(key):
                return _error_response(429, AuthError(429, "rate limit exceeded"))
        with principal_scope(principal.name if principal else None):
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
            group_id=body.group_id,
            tags=body.tags,
            purposes=body.purposes or None,
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
        scored = await engine.search(
            body.query, namespace=ns, top_k=body.top_k, purpose=body.purpose
        )
        return [ScoredRecord(record=record, score=score) for record, score in scored]

    @app.post("/assemble")
    async def assemble(body: AssembleRequest, ns: Namespace) -> AssembleResponse:
        context = await engine.assemble(
            body.query,
            namespace=ns,
            budget_tokens=body.budget_tokens,
            top_k=body.top_k,
            purpose=body.purpose,
        )
        return AssembleResponse(
            records=context.records,
            boundary_index=context.boundary_index,
            abstained=context.abstained,
            tokens_used=context.tokens_used,
        )

    @app.post("/retrieve")
    async def retrieve(body: RetrieveRequest, ns: Namespace) -> list[MemoryRecord]:
        return await engine.retrieve(
            namespace=ns, memory_type=body.memory_type, purpose=body.purpose
        )

    @app.delete("/records/{record_id}")
    async def forget(
        record_id: str,
        ns: Namespace,
        request: Request,
        hard: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        await engine.forget(
            record_id, namespace=ns, hard=hard, actor=_actor(request, "user"), reason=reason
        )
        return {"record_id": record_id, "forgotten": True, "hard": hard}

    @app.post("/correct")
    async def correct(body: CorrectRequest, ns: Namespace, request: Request) -> MemoryRecord:
        # #47 rectification: user-direct supersession, never CONTESTed. The new value
        # rides the "rest" channel like /write (SEC-C1): its trust stays capped.
        if body.record_id is not None:
            target: str | tuple[str, str] = body.record_id
        elif body.entity is not None and body.attribute is not None:
            target = (body.entity, body.attribute)
        else:
            raise MemspineError("correct needs record_id or entity + attribute")
        actor = _actor(request, body.actor)
        return await engine.correct(
            target,
            body.new_value,
            actor=actor,
            reason=body.reason,
            namespace=ns,
            source=SourceInfo(role=body.actor, channel="rest"),
        )

    # ── quarantine review (#3) ───────────────────────────────────────────────

    @app.get("/quarantine")
    async def list_quarantined(ns: Namespace) -> list[MemoryRecord]:
        return await engine.list_quarantined(namespace=ns)

    @app.post("/quarantine/{record_id}/approve")
    async def approve_quarantined(
        record_id: str, ns: Namespace, body: QuarantineDecision | None = None
    ) -> MemoryRecord:
        decision = body or QuarantineDecision()
        return await engine.approve_quarantined(
            record_id,
            namespace=ns,
            actor=decision.actor,
            reason=decision.reason or "operator_approved",
        )

    @app.post("/quarantine/{record_id}/reject")
    async def reject_quarantined(
        record_id: str, ns: Namespace, body: QuarantineDecision | None = None
    ) -> MemoryRecord:
        decision = body or QuarantineDecision()
        return await engine.reject_quarantined(
            record_id,
            namespace=ns,
            actor=decision.actor,
            reason=decision.reason or "operator_rejected",
        )

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

    @app.get("/export")
    async def export(
        ns: Namespace,
        subject: str | None = None,
        include_history: bool = True,
        include_events: bool = False,
    ) -> Response:
        # #46 subject-access export (JSONL). Operator route: admin role under auth.
        lines = await engine.export(
            ns, subject=subject, include_history=include_history, include_events=include_events
        )
        return Response(content="\n".join(lines) + "\n", media_type="application/x-ndjson")

    @app.get("/audit/taint/{record_id}")
    async def audit_taint(record_id: str, ns: Namespace) -> TaintReport:
        # SEC-C3/ADR-018: scope the seed to the caller's namespace so a leaked
        # record_id cannot be used to walk another tenant's derivation trail.
        return await engine.audit_taint(record_id, namespace=ns)

    return app
