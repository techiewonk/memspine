"""FastAPI app over ONE Engine (D-06, ``[rest]``).

#51: ``rest.auth.mode`` (``api_key`` | ``oidc_jwt``) turns on the reference auth
middleware in :mod:`memspine.api.rest.auth` (ADR-041): it binds a principal
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

import orjson
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from memspine.api.engine import Engine
from memspine.api.handle import Memory
from memspine.api.rest.auth import (
    Authenticator,
    AuthError,
    Principal,
    RateLimiter,
    is_admin_path,
    route_path,
)
from memspine.api.rest.models import (
    AssembleRequest,
    AssembleResponse,
    CorrectRequest,
    FeedbackRequest,
    GrantRequest,
    GrantView,
    IngestMessagesRequest,
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
    WriteRequest,
)
from memspine.config import constants
from memspine.config.schema import RestConfig, RestRateLimitConfig
from memspine.exceptions import ConflictError, MemspineError, MissingServiceError
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.read.tiers import ReadRequestError
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.store.scope import validate_namespace
from memspine.trust.audit import TaintReport
from memspine.trust.privacy import principal_scope

__all__ = ["build_app", "resolve_namespace", "resolve_operator"]

_log = get_logger(__name__)

_TRUE = frozenset({"1", "true", "yes"})
#: POST routes that only read (every GET reads too); the rest write or run maintenance.
_READ_POSTS = frozenset({"/search", "/assemble", "/retrieve"})


async def resolve_namespace(
    x_memspine_namespace: Annotated[str, Header(alias="X-Memspine-Namespace")] = "default",
) -> str:
    """The caller→namespace binding seam (ADR-017). v0.1 trusts the header;
    deployers override this dependency with their auth middleware's verdict."""
    return validate_namespace(x_memspine_namespace)


Namespace = Annotated[str, Depends(resolve_namespace)]


async def resolve_operator() -> str:
    """The operator seam for quarantine review (#3): who may release or reject a
    held record. A tenant caller must not review its own namespace's holds, so
    the default refuses every request (403); deployers override this dependency
    with their auth layer's operator identity::

        app.dependency_overrides[resolve_operator] = my_authenticated_operator

    The returned identity is the decision's actor and principal: a request body
    cannot name its own reviewer."""
    raise HTTPException(
        status_code=403,
        detail="quarantine review needs an operator: override resolve_operator (ADR-018)",
    )


Operator = Annotated[str, Depends(resolve_operator)]


class ORJSONResponse(JSONResponse):
    """JSON via orjson (D-38 hot path); FastAPI's own ``ORJSONResponse`` is deprecated."""

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS)


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
        #: Failed authentications per client address (only with auth on).
        self.failures: RateLimiter | None = None

    def ready(self) -> Authenticator:
        if self.authenticator is None:
            if self._rest is None and not self._engine.is_started:
                # Not started yet: no config to read. Pass through for now (the
                # verbs themselves refuse an unstarted engine); decide on start.
                return Authenticator(RestConfig())
            rest = self._rest if self._rest is not None else self._engine.config.rest
            self.authenticator = Authenticator(rest)
            self.limiter = RateLimiter(rest.rate_limit) if rest.rate_limit else None
            if self.authenticator.enabled:
                self.failures = RateLimiter(
                    rest.rate_limit
                    or RestRateLimitConfig(
                        requests_per_second=constants.REST_AUTH_FAILURE_PER_SECOND,
                        burst=constants.REST_AUTH_FAILURE_BURST,
                    )
                )
        return self.authenticator


def _principal(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else None


def _actor(request: Request, claimed: str) -> str:
    """The authenticated principal when auth is on, else the caller's claim. A
    claim that differs from the bound principal is overridden and logged."""
    principal = _principal(request)
    if principal is None:
        return claimed
    if claimed != principal.name:
        _log.info("rest.actor_overridden", claimed=claimed, principal=principal.name)
    return principal.name


def _bound_source(request: Request, source: SourceInfo) -> SourceInfo:
    """``source`` on the ``rest`` channel, with ``source.principal`` set to the
    authenticated principal when auth is on (a body cannot name another author,
    nor omit itself to dodge the quarantine author check)."""
    update: dict[str, object] = {"channel": "rest"}
    principal = _principal(request)
    if principal is not None:
        if source.principal not in (None, principal.name):
            _log.info(
                "rest.source_principal_overridden",
                claimed=source.principal,
                principal=principal.name,
            )
        update["principal"] = principal.name
    return source.model_copy(update=update)


def build_app(engine: Engine, rest: RestConfig | None = None) -> FastAPI:
    """The app over ``engine``. ``rest`` overrides the engine's ``rest`` config
    (auth mode, API keys, rate limit); None reads it from the started engine."""
    auth_state = _AuthState(engine, rest)
    if rest is not None or engine.is_started:
        auth_state.ready()  # fail fast on a bad auth config (e.g. pyjwt missing)

    async def require_admin(request: Request) -> None:
        """ADR-041 addendum: the admin check ON the operator routes themselves, so
        no mount prefix, ``root_path`` or path spelling reaches them without it."""
        if not auth_state.ready().enabled:
            return
        principal = _principal(request)
        if principal is None or not principal.admin:
            raise HTTPException(status_code=403, detail="this route needs the admin role")

    admin_only = [Depends(require_admin)]
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

    # ── api.request trace (WT-01): one request trace per call, kind read or write ──
    # Registered before the auth middleware, so it runs inside it (the principal is bound).
    # Inert unless the wiretap is on; the tap hashes the namespace, it is never logged.

    @app.middleware("http")
    async def _trace(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = route_path(request.scope)
        kind = "read" if request.method == "GET" or path in _READ_POSTS else "write"
        namespace = request.headers.get("x-memspine-namespace", "default")
        with tap.request(kind, scope=namespace, via="rest", route=path, method=request.method):
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
        client = "ip:" + (request.client.host if request.client else "")
        if authenticator.enabled:
            failures = auth_state.failures
            # Throttle by client address BEFORE the credential is checked: a run
            # of bad keys or tokens ends in 429, not in unbounded 401s.
            if failures is not None and not failures.peek(client):
                _log.info("rest.auth_throttled", path=request.url.path)
                return _error_response(429, AuthError(429, "too many failed authentications"))
            try:
                principal = authenticator.authenticate(request.headers)
                if is_admin_path(route_path(request.scope)) and not principal.admin:
                    raise AuthError(403, "this route needs the admin role")
                namespace = request.headers.get("x-memspine-namespace", "default")
                if not principal.may_use(namespace):
                    raise AuthError(403, f"principal may not use namespace {namespace!r}")
            except AuthError as exc:
                if exc.status == 401 and failures is not None:
                    failures.allow(client)  # one failure taken from the address
                _log.info("rest.auth_denied", status=exc.status, path=request.url.path)
                return _error_response(exc.status, exc)
            request.state.principal = principal
        if limiter is not None:
            key = f"principal:{principal.name}" if principal else client
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
    app.add_exception_handler(ReadRequestError, _memspine_error)
    app.add_exception_handler(Exception, _unknown)

    # ── core verbs ───────────────────────────────────────────────────────────
    # Every route builds a scoped ``Memory`` handle per request (G-16 D, G-17 C): the
    # namespace comes from the checked header, the principal and the viewer from the
    # authentication middleware. A body cannot carry either (models forbid extras).

    def scope(request: Request, ns: str, *, purpose: str | None = None) -> Memory:
        principal = _principal(request)
        name = principal.name if principal is not None else None
        multi = request.headers.get("x-memspine-multi-party", "").strip().lower() in _TRUE
        return Memory(engine, ns, principal=name, viewer=name, purpose=purpose, multi_party=multi)

    @app.post("/write")
    async def write(body: WriteRequest, ns: Namespace, request: Request) -> MemoryRecord:
        # SEC-C1/ADR-018: REST is untrusted external input. Force the write onto
        # the "rest" channel (in TrustPolicy._EXTERNAL_CHANNELS) so TrustPolicy
        # caps trust at TRUST_RETRIEVED_CAP REGARDLESS of the caller-supplied
        # role — a caller cannot claim role="operator" to escalate trust or dodge
        # the firewall. The role is preserved for provenance only.
        # Under auth, source.principal and the actor are the bound principal.
        source = _bound_source(request, body.source or SourceInfo(role=body.actor))
        return await scope(request, ns).remember(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            body.content,
            memory_type=body.memory_type,
            source=source,
            pii_tier=body.pii_tier,
            actor=_actor(request, body.actor),
            entity=body.entity,
            attribute=body.attribute,
            group_id=body.group_id,
            tags=body.tags,
            purposes=body.purposes or None,
        )

    @app.post("/ingest_messages")
    async def ingest_messages(
        body: IngestMessagesRequest, ns: Namespace, request: Request
    ) -> list[MemoryRecord]:
        # C4: chat-transcript ingestion. Force channel="rest" (SEC-C1, same as
        # /write) so TrustPolicy caps each turn's trust regardless of the claimed
        # role — the per-turn role is preserved for provenance only.
        turns = [turn.model_dump(exclude_none=True) for turn in body.messages]
        actor = _actor(request, body.actor)
        mem = scope(request, ns)
        if body.as_episode:
            if body.session_id is not None or body.conversation_id is not None:
                raise HTTPException(
                    status_code=422,
                    detail="session_id and conversation_id cannot be set with as_episode",
                )
            try:
                return await mem.write_episode(turns, actor=actor, channel="rest")  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            return await mem.ingest_messages(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
                turns,
                actor=actor,
                channel="rest",
                session_id=body.session_id,
                conversation_id=body.conversation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/search")
    async def search(body: SearchRequest, ns: Namespace, request: Request) -> list[ScoredRecord]:
        scored = await scope(request, ns, purpose=body.purpose).recall(
            body.query,
            top_k=body.top_k,
            valid_from_after=body.valid_from_after,
            valid_from_before=body.valid_from_before,
            valid_to_after=body.valid_to_after,
            valid_to_before=body.valid_to_before,
            recorded_after=body.recorded_after,
            recorded_before=body.recorded_before,
            date_filter_mode=body.date_filter_mode,
        )
        return [ScoredRecord(record=record, score=score) for record, score in scored]

    @app.post("/assemble")
    async def assemble(body: AssembleRequest, ns: Namespace, request: Request) -> AssembleResponse:
        context = await scope(request, ns, purpose=body.purpose).compose(
            body.query,
            body.budget_tokens,
            top_k=body.top_k,
            as_of=body.as_of,
        )
        return AssembleResponse(
            records=context.records,
            boundary_index=context.boundary_index,
            abstained=context.abstained,
            tokens_used=context.tokens_used,
        )

    @app.post("/retrieve")
    async def retrieve(
        body: RetrieveRequest, ns: Namespace, request: Request
    ) -> list[MemoryRecord]:
        return await scope(request, ns, purpose=body.purpose).retrieve(memory_type=body.memory_type)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.post("/feedback")
    async def feedback(body: FeedbackRequest, ns: Namespace, request: Request) -> MemoryRecord:
        # #54: counts only; the bounded utility transform caps what a caller can move.
        return await scope(request, ns).feedback(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            body.record_id,
            body.signal,
            note=body.note,
            actor=_actor(request, body.actor),
        )

    @app.delete("/records/{record_id}")
    async def forget(
        record_id: str,
        ns: Namespace,
        request: Request,
        hard: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        await scope(request, ns).forget(
            record_id, hard=hard, actor=_actor(request, "user"), reason=reason
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
        return await scope(request, ns).correct(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            target,
            body.new_value,
            actor=actor,
            reason=body.reason,
            source=_bound_source(request, SourceInfo(role=body.actor)),
        )

    # ── quarantine review (#3) ───────────────────────────────────────────────

    @app.get("/quarantine", dependencies=admin_only)
    async def list_quarantined(ns: Namespace, request: Request) -> list[MemoryRecord]:
        return await scope(request, ns).list_quarantined()  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    # The decisions are operator-only (resolve_operator): the namespace header
    # is the tenant, and a tenant could otherwise release its own poison.

    @app.post("/quarantine/{record_id}/approve", dependencies=admin_only)
    async def approve_quarantined(
        record_id: str,
        ns: Namespace,
        operator: Operator,
        request: Request,
        body: QuarantineDecision | None = None,
    ) -> MemoryRecord:
        decision = body or QuarantineDecision()
        return await scope(request, ns).approve_quarantined(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            record_id,
            actor=operator,
            principal=operator,
            reason=decision.reason or "operator_approved",
        )

    @app.post("/quarantine/{record_id}/reject", dependencies=admin_only)
    async def reject_quarantined(
        record_id: str,
        ns: Namespace,
        operator: Operator,
        request: Request,
        body: QuarantineDecision | None = None,
    ) -> MemoryRecord:
        decision = body or QuarantineDecision()
        return await scope(request, ns).reject_quarantined(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            record_id,
            actor=operator,
            reason=decision.reason or "operator_rejected",
        )

    @app.get("/describe")
    async def describe(request: Request) -> dict[str, Any]:
        return scope(request, "default").describe()

    # ── procedural (M13.4 / E6) ──────────────────────────────────────────────

    @app.post("/skills")
    async def add_skill(body: SkillRequest, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).add_skill(body.content, body.name, actor=body.actor)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.post("/skills/{record_id}/promote")
    async def promote_skill(
        record_id: str, ns: Namespace, request: Request, body: PromoteRequest | None = None
    ) -> MemoryRecord:
        dry_run_passed = body.dry_run_passed if body is not None else False
        return await scope(request, ns).promote_skill(record_id, dry_run_passed=dry_run_passed)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.delete("/skills/{record_id}")
    async def deprecate_skill(record_id: str, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).deprecate_skill(record_id)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.post("/plans")
    async def record_plan(body: PlanRequest, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).record_plan(body.task, body.content, actor=body.actor)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.get("/plans/recall")
    async def recall_plan(
        task: str,
        ns: Namespace,
        request: Request,
        min_similarity: float = constants.PLAN_RECALL_MIN_SIMILARITY,
    ) -> MemoryRecord | None:
        return await scope(request, ns).recall_plan(task, min_similarity=min_similarity)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    # ── reflective (M13.7) ───────────────────────────────────────────────────

    @app.post("/reflect")
    async def reflect(body: ReflectRequest, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).reflect(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            body.content, body.source_record_ids, actor=body.actor
        )

    # ── prospective (M13.8) ──────────────────────────────────────────────────

    @app.post("/watches")
    async def watch(body: WatchRequest, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).watch(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            body.content,
            due_at=body.due_at,
            entity=body.entity,
            attribute=body.attribute,
            actor=body.actor,
        )

    @app.get("/watches/due")
    async def due(
        ns: Namespace, request: Request, now: datetime | None = None
    ) -> list[MemoryRecord]:
        return await scope(request, ns).due(now=now)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.post("/watches/{record_id}/ack")
    async def acknowledge_watch(record_id: str, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).acknowledge_watch(record_id)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    # ── shared (R2) ──────────────────────────────────────────────────────────

    @app.post("/grants")
    async def grant(body: GrantRequest, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).grant(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
            body.to_namespace,
            memory_types=body.memory_types,
            actor=_actor(request, body.actor),
        )

    @app.delete("/grants")
    async def revoke(to_namespace: str, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).revoke(to_namespace, actor=_actor(request, "user"))  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.get("/shared_search")
    async def shared_search(
        query: str, ns: Namespace, request: Request, top_k: int = constants.SEARCH_TOP_K
    ) -> list[ScoredRecord]:
        scored = await scope(request, ns).shared_search(query, top_k=top_k)
        return [ScoredRecord(record=record, score=score) for record, score in scored]

    @app.get("/grants")
    async def grants_from(ns: Namespace, request: Request) -> list[GrantView]:
        """Live grants ``ns`` has issued (operator listing surface, ADR-016).
        Scoped to the caller — never another namespace's grants."""
        grants = await scope(request, ns).grants_from()
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
    async def subscribe(body: SubscriptionRequest, ns: Namespace, request: Request) -> MemoryRecord:
        return await scope(request, ns).subscribe(body.query, actor=body.actor)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.get("/subscriptions")
    async def subscriptions(ns: Namespace, request: Request) -> list[MemoryRecord]:
        return await scope(request, ns).subscriptions()  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    # ── maintenance + governance ─────────────────────────────────────────────
    # ⚠️  ADR-018: /sleep, /rebuild and /audit/taint are engine-global or
    # cross-cutting. Behind the no-authn seam they MUST sit on an internal-only
    # network boundary — never expose them to tenant callers.

    @app.post("/sleep", dependencies=admin_only)
    async def sleep(request: Request) -> dict[str, dict[str, Any]]:
        return await scope(request, "default").sleep()  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    @app.get("/ops/report", dependencies=admin_only)
    async def ops_report() -> dict[str, Any]:
        # T5-06: engine-wide, ids / counts / durations only (no content, no namespaces).
        return engine.ops_report()

    @app.post("/rebuild", dependencies=admin_only)
    async def rebuild(request: Request) -> dict[str, int]:
        return await scope(request, "default").rebuild()

    @app.get("/export", dependencies=admin_only)
    async def export(
        ns: Namespace,
        request: Request,
        subject: str | None = None,
        include_history: bool = True,
        include_events: bool = False,
    ) -> Response:
        # #46 subject-access export (JSONL). Operator route: admin role under auth.
        lines = await scope(request, ns).export(
            subject=subject, include_history=include_history, include_events=include_events
        )
        return Response(content="\n".join(lines) + "\n", media_type="application/x-ndjson")

    @app.get("/audit/taint/{record_id}")
    async def audit_taint(record_id: str, ns: Namespace, request: Request) -> TaintReport:
        # SEC-C3/ADR-018: scope the seed to the caller's namespace so a leaked
        # record_id cannot be used to walk another tenant's derivation trail.
        return await scope(request, ns).audit_taint(record_id)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    return app
