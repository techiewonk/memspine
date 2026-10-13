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
``memory_traces()``, ``register_sink(name, factory)``.

Event names follow ``<pipeline>.<stage>.<what>`` (``read.gate.dropped``). Field values are guarded:
numbers, booleans, None, identifier-shaped strings (LIB-27 content guard) and lists / dicts
of those.
Anything else is dropped and its name is listed under the ``field_rejected`` field of that event.

Off path: ``enabled()`` is one module-level bool read; ``event``/``span``/``request`` test it
first and return without touching a ContextVar or building anything.
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

from memspine.observability.stage_ledger import ContentGuardError, _guard, scope_hash

if TYPE_CHECKING:
    from memspine.config.schema.observability import TapConfig

__all__ = [
    "Content",
    "RequestTrace",
    "TapConfigError",
    "TapEvent",
    "TapSink",
    "configure",
    "content",
    "content_enabled",
    "current",
    "enabled",
    "event",
    "memory_traces",
    "register_sink",
    "request",
    "shutdown",
    "span",
    "traced",
]

MAX_LIST_ITEMS = 200
MAX_DEPTH = 3
_F = TypeVar("_F", bound=Callable[..., Any])

# ── module state (set by configure) ──────────────────────────────────────────
_ON = False
_CONTENT = False
_MAX_CHARS = 200
_SAMPLE = 1.0
_KINDS: frozenset[str] = frozenset({"read", "write", "job"})
_SINK: TapSink | None = None
_SINK_LOCK = threading.Lock()
_CUR: ContextVar[RequestTrace | None] = ContextVar("memspine_tap_request", default=None)
_ERRORS = 0  # sink failures swallowed (never raised into the engine)


class TapConfigError(RuntimeError):
    """The tap could not be enabled as configured (e.g. the otel sink without opentelemetry)."""


class TapSink(Protocol):
    def write(self, trace: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


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
    if not _CONTENT:
        return _NO_CONTENT
    out = Content(_mask(str(text))[:_MAX_CHARS])
    out.kept = True
    return out


def content_enabled() -> bool:
    return _ON and _CONTENT


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
    return _ON


def current() -> RequestTrace | None:
    return _CUR.get() if _ON else None


def _append(trace: RequestTrace, stage: str, fields: Mapping[str, Any]) -> None:
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
    if not _ON:
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
    if not _ON:
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
    if not _ON:
        yield
        return
    parent = _CUR.get()
    scope = fields.pop("scope", None)
    if parent is None and ((kind not in _KINDS and kind != "adhoc") or random.random() >= _SAMPLE):
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
            _emit(trace)


def traced(kind: str) -> Callable[[_F], _F]:
    """Decorator: open a ``kind`` request around a (sync or async) function. Off: one bool test."""

    def deco(fn: _F) -> _F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                if not _ON:
                    return await fn(*args, **kwargs)
                with request(kind, op=fn.__name__, **_request_fields(kwargs)):
                    return await fn(*args, **kwargs)

            return awrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _ON:
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
def _emit(trace: RequestTrace) -> None:
    global _ERRORS
    sink = _SINK
    if sink is None:
        return
    try:
        data = trace.to_dict()
        with _SINK_LOCK:
            sink.write(data)
    except Exception:
        _ERRORS += 1


def memory_traces() -> list[dict[str, Any]]:
    """Traces held by the active ``memory`` sink (oldest first); empty for other sinks."""
    sink = _SINK
    traces = getattr(sink, "traces", None)
    return traces() if callable(traces) else []


def sink_errors() -> int:
    return _ERRORS


def configure(cfg: TapConfig | None) -> None:
    """Apply a ``TapConfig``. Disabled (or ``None``) switches the tap off and closes the sink."""
    global _ON, _CONTENT, _MAX_CHARS, _SAMPLE, _KINDS, _SINK
    shutdown()
    if cfg is None or not cfg.enabled:
        return
    from memspine.observability import tap_sinks  # registers the built-in sinks

    tap_sinks.register_builtin()
    sink: TapSink | None = None
    if cfg.sink != "none":
        factory = _SINK_FACTORIES.get(cfg.sink)
        if factory is None:
            raise TapConfigError(f"unknown tap sink {cfg.sink!r}")
        sink = factory(cfg)  # may raise TapConfigError (otel without opentelemetry)
    _SINK = sink
    _CONTENT = bool(cfg.content)
    _MAX_CHARS = int(cfg.max_content_chars)
    _SAMPLE = float(cfg.sample_rate)
    _KINDS = frozenset(cfg.kinds)
    _ON = True


def shutdown() -> None:
    """Switch the tap off and close the sink."""
    global _ON, _CONTENT, _SINK
    _ON = False
    _CONTENT = False
    sink, _SINK = _SINK, None
    if sink is not None:
        with contextlib.suppress(Exception):
            sink.close()
