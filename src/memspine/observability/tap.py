"""Wiretap: a per-request trace of every pipeline stage (WT-01). Debugging only, off by default.

API contract for wiring code (stable):

- ``event(stage, /, **fields)``: record one stage event in the current request trace. ``fields``
  hold ids, counts, scores, decisions and drop reasons. Never pass free text unless it is wrapped
  with ``content(...)``, which is dropped unless the content opt-in is on.
- ``span(stage, /, **fields)``: context manager timing a stage; emits start/end events.
- ``request(kind, /, **fields)``: context manager opening a request trace (read, write, sleep job).
- ``enabled()``: cheap check so callers can skip building expensive payloads.
- ``content(text)``: marks text as content (truncated, redacted, opt-in only).

Extras: ``configure(cfg)``, ``shutdown()``, ``traced(kind)`` (decorator that opens a request
around an async or sync callable), ``current()``,
``memory_traces()``, ``register_sink(name, factory)``, ``capture_only(sink)``.

Per-Engine state (ADR-079 decision 5): an Engine opens a :class:`TapState` for its own tap
settings (``open_state``; shared through the resource registry between Engines with identical
settings) and binds it (``observability.bindings``); every call of that Engine uses it.
``configure`` / ``shutdown`` manage the process default used by code outside any Engine.

Event names follow ``<pipeline>.<stage>.<what>`` (``read.gate.dropped``). Field values are guarded:
numbers, booleans, None, identifier-shaped strings (LIB-27 content guard) and lists / dicts
of those.
Anything else is dropped and its name is listed under the ``field_rejected`` field of that event.

Off path: with no tap state open anywhere in the process ``enabled()`` is one module-level bool
read; ``event``/``span``/``request`` test it first and return without building anything.
Sinks never raise into the engine.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import math
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from memspine.observability import bindings as _bindings
from memspine.observability.stage_ledger import ContentGuardError, _guard, scope_hash

if TYPE_CHECKING:
    from memspine.config.schema.observability import TapConfig

__all__ = [
    "Content",
    "RequestTrace",
    "TapConfigError",
    "TapEvent",
    "TapSink",
    "TapState",
    "add_sink",
    "capture_only",
    "close_state",
    "configure",
    "content",
    "content_enabled",
    "current",
    "enabled",
    "event",
    "memory_traces",
    "open_state",
    "register_sink",
    "request",
    "shutdown",
    "span",
    "traced",
]

MAX_LIST_ITEMS = 200
MAX_DEPTH = 3
_F = TypeVar("_F", bound=Callable[..., Any])

# ── module state ─────────────────────────────────────────────────────────────
#: True while any tap state is open in the process (the process default or an Engine's): the
#: off-path check. Which state applies is resolved per call (``_state``).
_ON = False
_OPEN_STATES = 0  # Engine-held states (open_state / close_state)
_STATE_LOCK = threading.Lock()
_SINK_LOCK = threading.Lock()
_CUR: ContextVar[RequestTrace | None] = ContextVar("memspine_tap_request", default=None)
_ERRORS = 0  # sink failures swallowed (never raised into the engine)
#: TI-1 (``observability.inspect``): correlation keys merged into every event (run_id, item,
#: turn_id, query_id, window_id ...). Set only by ``inspect.inspect_scope``; None otherwise.
_SCOPE: ContextVar[Mapping[str, Any] | None] = ContextVar("memspine_tap_scope", default=None)


class TapConfigError(RuntimeError):
    """The tap could not be enabled as configured (e.g. the otel sink without opentelemetry)."""


class TapSink(Protocol):
    def write(self, trace: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


@dataclass(slots=True, eq=False)
class TapState:
    """One tap configuration: its options and its configured sink."""

    content: bool
    max_chars: int
    sample: float
    kinds: frozenset[str]
    sink: TapSink | None
    #: T5-06: sinks next to the configured one added outside an Engine (``add_sink``)
    extra_sinks: list[TapSink] = field(default_factory=list)


_DEFAULT: TapState | None = None  # configure() / shutdown(): code outside any Engine
#: capture_only(): traces finished in this context go to these sinks only
_CAPTURE: ContextVar[tuple[TapSink, ...] | None] = ContextVar("memspine_tap_capture", default=None)


def _state() -> TapState | None:
    state: TapState | None = _bindings.resolve("tap", _DEFAULT)
    return state


_SINK_FACTORIES: dict[str, Callable[[Any], TapSink | None]] = {}


def register_sink(name: str, factory: Callable[[Any], TapSink | None]) -> None:
    """Register a sink factory ``factory(cfg) -> sink``; ``cfg`` is the ``TapConfig``."""
    _SINK_FACTORIES[name] = factory


class Content(str):
    """Marker for text that is allowed into a trace only under the content opt-in."""

    kept: bool = False


_NO_CONTENT = Content("")


@dataclass(slots=True)
class TapEvent:
    seq: int
    ts_offset_ms: float
    stage: str
    fields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "t_ms": round(self.ts_offset_ms, 3),
            "stage": self.stage,
            "fields": self.fields,
        }


@dataclass(slots=True)
class RequestTrace:
    id: str
    kind: str
    scope_hash: str
    started: str
    t0: float
    parent_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    events: list[TapEvent] = field(default_factory=list)
    children: list[RequestTrace] = field(default_factory=list)
    duration_ms: float = 0.0
    active: bool = True  # False: a sampled-out / filtered root; events are discarded
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "scope_hash": self.scope_hash,
            "started": self.started,
            "duration_ms": round(self.duration_ms, 3),
            "fields": self.fields,
            "events": [e.to_dict() for e in self.events],
            "children": [c.to_dict() for c in self.children],
        }


# ── field guard ──────────────────────────────────────────────────────────────
_MASKS = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "[number]"),
)


def _mask(text: str) -> str:
    try:
        from memspine.trust.redaction import redact

        text, _ = redact(text, secrets=True, pii=True)
    except Exception:  # redaction is best effort; fall back to simple masking
        pass
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text


def content(text: str) -> Content:
    """Mark ``text`` as content: kept (truncated, redacted) only when the content opt-in is on."""
    state = _state() if _ON else None
    if state is None or not state.content:
        return _NO_CONTENT
    out = Content(_mask(str(text))[: state.max_chars])
    out.kept = True
    return out


def content_enabled() -> bool:
    if not _ON:
        return False
    state = _state()
    return state is not None and state.content


class _Reject(Exception):
    pass


def _clean(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Content):
        if value.kept:
            return str(value)
        raise _Reject
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Reject
        return round(value, 6)
    if isinstance(value, str):
        try:
            return _guard("field", value)
        except ContentGuardError:
            raise _Reject from None
    if depth >= MAX_DEPTH:
        raise _Reject
    if isinstance(value, Mapping):
        return {
            _clean_key(k): _clean(v, depth + 1) for k, v in list(value.items())[:MAX_LIST_ITEMS]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:MAX_LIST_ITEMS]
        return [_clean(v, depth + 1) for v in items]
    if hasattr(value, "item") and callable(value.item) and not isinstance(value, Mapping):
        try:
            return _clean(value.item(), depth)
        except (ValueError, TypeError):
            raise _Reject from None
    if hasattr(value, "value") and isinstance(value.value, str):  # enums
        return _clean(value.value, depth)
    raise _Reject


def _clean_key(key: Any) -> str:
    if not isinstance(key, str):
        key = str(key)
    try:
        return _guard("key", key)
    except ContentGuardError:
        raise _Reject from None


def _guard_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rejected: list[str] = []
    for name, value in fields.items():
        try:
            if isinstance(value, Content) and not value.kept:
                continue  # content off: silently absent, not an error
            out[name] = _clean(value)
        except _Reject:
            rejected.append(name)
    if rejected:
        out["field_rejected"] = rejected
    return out


# ── public API ───────────────────────────────────────────────────────────────
def enabled() -> bool:
    return _ON and _state() is not None


def current() -> RequestTrace | None:
    return _CUR.get() if enabled() else None


def _append(trace: RequestTrace, stage: str, fields: Mapping[str, Any]) -> None:
    scope = _SCOPE.get()
    if scope:  # TI-1: correlation keys; an event's own field of the same name wins
        fields = {**scope, **fields}
    with trace._lock:
        trace.events.append(
            TapEvent(
                len(trace.events),
                (time.perf_counter() - trace.t0) * 1000.0,
                stage,
                _guard_fields(fields),
            )
        )


def event(stage: str, /, **fields: Any) -> None:
    if not enabled():
        return None
    trace = _CUR.get()
    if trace is None:  # an event outside any request: a one-event "adhoc" trace
        with request("adhoc"):
            event(stage, **fields)
        return None
    if trace.active:
        _append(trace, stage, fields)
    return None


@contextmanager
def span(stage: str, /, **fields: Any) -> Iterator[None]:
    if not enabled():
        yield
        return
    t0 = time.perf_counter()
    event(f"{stage}.start", **fields)
    error: str | None = None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        extra: dict[str, Any] = {"duration_ms": round((time.perf_counter() - t0) * 1000.0, 3)}
        if error:
            extra["error"] = error
        event(f"{stage}.end", **extra)


def _scope_of(raw: Any) -> str:
    if raw is None:
        return "none"
    if isinstance(raw, str):
        return raw if len(raw) <= 32 and raw.isalnum() else scope_hash_str(raw)
    try:
        return scope_hash(raw)
    except Exception:
        return scope_hash_str(str(raw))


def scope_hash_str(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@contextmanager
def request(kind: str, /, **fields: Any) -> Iterator[None]:
    state = _state() if _ON else None
    if state is None:
        yield
        return
    parent = _CUR.get()
    scope = fields.pop("scope", None)
    if parent is None and (
        (kind not in state.kinds and kind != "adhoc") or random.random() >= state.sample
    ):
        token = _CUR.set(
            RequestTrace("skipped", kind, "none", "", time.perf_counter(), active=False)
        )
        try:
            yield
        finally:
            _CUR.reset(token)
        return
    if parent is not None and not parent.active:
        yield
        return
    trace = RequestTrace(
        id=uuid.uuid4().hex[:16],
        kind=kind,
        scope_hash=_scope_of(scope),
        started=datetime.now(UTC).isoformat(timespec="milliseconds"),
        t0=time.perf_counter(),
        parent_id=parent.id if parent else None,
        fields=_guard_fields(fields),
    )
    token = _CUR.set(trace)
    bridge = _open_bridge(kind) if parent is None else None
    try:
        yield
    finally:
        _CUR.reset(token)
        trace.duration_ms = (time.perf_counter() - trace.t0) * 1000.0
        if bridge is not None:
            _close_bridge(trace, bridge)
        if parent is not None:
            with parent._lock:
                parent.children.append(trace)
        else:
            _emit(trace, state)


def traced(kind: str) -> Callable[[_F], _F]:
    """Decorator: open a ``kind`` request around a (sync or async) function. Off: one bool test."""

    def deco(fn: _F) -> _F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                if not enabled():
                    return await fn(*args, **kwargs)
                with request(kind, op=fn.__name__, **_request_fields(kwargs)):
                    return await fn(*args, **kwargs)

            return awrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not enabled():
                return fn(*args, **kwargs)
            with request(kind, op=fn.__name__, **_request_fields(kwargs)):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return deco


def _request_fields(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("namespace", "top_k", "memory_type", "session_id", "purpose", "viewer")
    return {k: kwargs[k] for k in keep if k in kwargs}


# ── forensics bridge: the existing core.trace_sink hooks feed the tap ─────────
def _open_bridge(kind: str) -> tuple[Any, Any, Any, Any] | None:
    try:
        from memspine.core import trace_sink
    except Exception:
        return None
    fx_token = w_token = None
    fx: dict[str, Any] | None = None
    wsink: list[dict[str, Any]] | None = None
    if trace_sink.FORENSICS.get() is None:
        fx = {}
        fx_token = trace_sink.FORENSICS.set(fx)
    if trace_sink.WRITE.get() is None:
        wsink = []
        w_token = trace_sink.WRITE.set(wsink)
    if fx_token is None and w_token is None:
        return None
    return trace_sink, (fx_token, fx), (w_token, wsink), kind


def _close_bridge(trace: RequestTrace, bridge: tuple[Any, Any, Any, Any]) -> None:
    trace_sink, (fx_token, fx), (w_token, wsink), _kind = bridge
    if fx_token is not None:
        trace_sink.FORENSICS.reset(fx_token)
    if w_token is not None:
        trace_sink.WRITE.reset(w_token)
    try:
        if fx:
            for key, val in fx.items():
                if key == "cuts":
                    for cut in val[:MAX_LIST_ITEMS]:
                        _append(trace, "read.forensics.cut", _cut_fields(cut))
                else:
                    _append(trace, f"read.forensics.{key}", _ranked_fields(val))
        for note in (wsink or [])[:MAX_LIST_ITEMS]:
            note = dict(note)
            nk = str(note.pop("kind", "note"))
            _append(trace, f"write.forensics.{nk}", note)
    except Exception:  # a bridge must never break the request
        pass


def _cut_fields(cut: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(cut)
    out.setdefault("id", None)
    return out


def _ranked_fields(val: Any) -> dict[str, Any]:
    """Summarise a forensics entry: a ranked ``[(id, score)]`` list becomes count + ids + scores."""
    if isinstance(val, (list, tuple)):
        ids: list[Any] = []
        scores: list[Any] = []
        ok = True
        for item in val[:MAX_LIST_ITEMS]:
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
                ids.append(item[0])
                scores.append(item[1])
            elif isinstance(item, str):
                ids.append(item)
            else:
                ok = False
                break
        if ok:
            out: dict[str, Any] = {"n": len(val), "ids": ids}
            if scores:
                out["scores"] = scores
            return out
        return {"n": len(val)}
    return {"value": val}


# ── sinks plumbing ───────────────────────────────────────────────────────────
def _sinks(state: TapState) -> list[TapSink]:
    captured = _CAPTURE.get()
    if captured is not None:
        return list(captured)
    sinks = [s for s in (state.sink, *state.extra_sinks) if s is not None]
    binding = _bindings.current()
    if binding is not None and binding.tap is state:
        sinks.extend(binding.tap_sinks)
    return sinks


def _emit(trace: RequestTrace, state: TapState) -> None:
    global _ERRORS
    sinks = _sinks(state)
    if not sinks:
        return
    try:
        data = trace.to_dict()
    except Exception:
        _ERRORS += 1
        return
    for sink in sinks:
        try:
            with _SINK_LOCK:
                sink.write(data)
        except Exception:
            _ERRORS += 1


def add_sink(sink: TapSink) -> None:
    """Add a sink that receives every finished trace next to the configured one (T5-06). It
    joins the current Engine's tap when that Engine has one (closed when the Engine stops), else
    the process default (closed by ``shutdown()``); a no-op with the tap off. Sinks never raise
    into the engine."""
    binding = _bindings.current()
    if binding is not None and binding.tap is not None:
        binding.tap_sinks.append(sink)
    elif _DEFAULT is not None:
        _DEFAULT.extra_sinks.append(sink)


@contextmanager
def capture_only(*sinks: TapSink) -> Iterator[None]:
    """Traces finished inside this block (and the tasks it spawns) go to ``sinks`` only."""
    token = _CAPTURE.set(tuple(sinks))
    try:
        yield
    finally:
        _CAPTURE.reset(token)


def memory_traces() -> list[dict[str, Any]]:
    """Traces held by the active ``memory`` sink (oldest first); empty for other sinks."""
    state = _state()
    traces = getattr(state.sink if state is not None else None, "traces", None)
    return traces() if callable(traces) else []


def sink_errors() -> int:
    return _ERRORS


def _build_state(cfg: TapConfig) -> TapState:
    from memspine.observability import tap_sinks  # registers the built-in sinks

    tap_sinks.register_builtin()
    sink: TapSink | None = None
    if cfg.sink != "none":
        factory = _SINK_FACTORIES.get(cfg.sink)
        if factory is None:
            raise TapConfigError(f"unknown tap sink {cfg.sink!r}")
        sink = factory(cfg)  # may raise TapConfigError (otel without opentelemetry)
    return TapState(
        content=bool(cfg.content),
        max_chars=int(cfg.max_content_chars),
        sample=float(cfg.sample_rate),
        kinds=frozenset(cfg.kinds),
        sink=sink,
    )


def _close_sinks(sinks: list[TapSink | None]) -> None:
    for closing in sinks:
        if closing is not None:
            with contextlib.suppress(Exception):
                closing.close()


def _recompute_on() -> None:
    global _ON
    _ON = _DEFAULT is not None or _OPEN_STATES > 0


def open_state(cfg: TapConfig) -> TapState:
    """An Engine's tap state for an enabled ``cfg`` (the caller binds it; ``close_state`` ends it)."""
    global _OPEN_STATES
    if not cfg.enabled:
        raise TapConfigError("open_state needs an enabled tap config")
    state = _build_state(cfg)
    with _STATE_LOCK:
        _OPEN_STATES += 1
        _recompute_on()
    return state


def close_state(state: TapState) -> None:
    """Close a state from ``open_state``: its sinks are closed."""
    global _OPEN_STATES
    with _STATE_LOCK:
        _OPEN_STATES = max(0, _OPEN_STATES - 1)
        _recompute_on()
    _close_sinks([state.sink, *state.extra_sinks])


def configure(cfg: TapConfig | None) -> None:
    """Apply a ``TapConfig`` as the process default (code outside any Engine). Disabled (or
    ``None``) switches the default off and closes its sink."""
    global _DEFAULT
    shutdown()
    if cfg is None or not cfg.enabled:
        return
    state = _build_state(cfg)
    with _STATE_LOCK:
        _DEFAULT = state
        _recompute_on()


def shutdown() -> None:
    """Switch the process default tap off and close its sinks (Engine states are untouched)."""
    global _DEFAULT
    with _STATE_LOCK:
        state, _DEFAULT = _DEFAULT, None
        _recompute_on()
    if state is not None:
        _close_sinks([state.sink, *state.extra_sinks])
