"""Optional OpenTelemetry export, request -> stage spans, with no content in them (T5-06; G-20).

Behind ``observability.otel.enabled`` (default off). The exporter is a wiretap sink: it turns each
finished tap trace into one request span with one child span per stage, and a child request (a
sleep stage's job trace) into a nested span. Span attributes carry only ids, hashes, counts, codes
and durations; :func:`validate_span_attributes` is the hard guard and runs on every attribute set
before it reaches the tracer. A string must be an identifier (the LIB-27 guard that the wiretap
uses: short, no whitespace, id alphabet) and, except for the ``error`` code and the model id, also
a lowercase code or a hex hash, so a name or a sentence cannot ride along. Tap event fields never
become attributes; only the number of events per stage and the integer counts in them do.

Needs the ``memspine[otel]`` extra (``opentelemetry-api``; add ``opentelemetry-sdk`` and an
exporter to ship the spans anywhere). Missing: :class:`~memspine.exceptions.MissingServiceError`.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.observability.stage_ledger import (
    ContentGuardError,
    StageEntry,
    _guard,
    to_otel_attributes,
)

__all__ = [
    "OtelExporter",
    "SpanContentError",
    "scrub_span_attributes",
    "validate_span_attributes",
]

_KEY_RE = re.compile(r"memspine\.[a-z][a-z0-9_.]{0,63}")
_HEX_RE = re.compile(r"[0-9a-f]{8,64}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_.:\-]{0,127}")
#: Keys whose string value only has to be an identifier (an exception class name, a model id).
_ID_KEYS = frozenset({"memspine.error", "memspine.model_id"})


class SpanContentError(ContentGuardError):
    """A span attribute looked like content rather than an id, hash, count, code or duration."""


def validate_span_attributes(attrs: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    """Return ``attrs`` unchanged when every item is allowed; raise :class:`SpanContentError`."""
    out: dict[str, str | int | float | bool] = {}
    for key, value in attrs.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise SpanContentError(f"span attribute key {key!r} is not a memspine.* name")
        if isinstance(value, (bool, int)):
            out[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise SpanContentError(f"{key} is not finite")
            out[key] = value
        elif isinstance(value, str):
            try:
                _guard(key, value)
            except ContentGuardError as exc:
                raise SpanContentError(str(exc)) from exc
            if key not in _ID_KEYS and not (
                value == "none" or _HEX_RE.fullmatch(value) or _CODE_RE.fullmatch(value)
            ):
                raise SpanContentError(f"{key} is not a lowercase code or a hash")
            out[key] = value
        else:
            raise SpanContentError(f"{key} has type {type(value).__name__}: not an allowed value")
    return out


def scrub_span_attributes(
    attrs: Mapping[str, Any],
) -> tuple[dict[str, str | int | float | bool], list[str]]:
    """Keep the allowed attributes; return them with the names of the rejected ones."""
    clean: dict[str, str | int | float | bool] = {}
    rejected: list[str] = []
    for key, value in attrs.items():
        try:
            clean.update(validate_span_attributes({key: value}))
        except SpanContentError:
            rejected.append(str(key))
    return clean, rejected


def _suffix_free(stage: str) -> str:
    for suffix in (".start", ".end"):
        if stage.endswith(suffix):
            return stage[: -len(suffix)]
    return stage


def _start_ns(trace: Mapping[str, Any]) -> int | None:
    try:
        return int(datetime.fromisoformat(str(trace["started"])).timestamp() * 1e9)
    except (KeyError, ValueError):
        return None


class OtelExporter:
    """A wiretap sink (``write(trace)`` / ``close()``) that emits request -> stage spans."""

    def __init__(self, *, tracer: Any = None, tracer_provider: Any = None) -> None:
        try:
            from opentelemetry import trace as otel_trace
        except ImportError as exc:
            raise MissingServiceError("observability.otel", extra="otel") from exc
        self._api = otel_trace
        if tracer is None:
            tracer = (
                tracer_provider.get_tracer("memspine")
                if tracer_provider is not None
                else otel_trace.get_tracer("memspine")
            )
        self._tracer = tracer
        self._lock = threading.Lock()
        self.spans = 0
        self.rejected_attributes = 0

    # --- tap sink protocol -----------------------------------------------------------------
    def write(self, trace: dict[str, Any]) -> None:
        self._request(trace, None)

    def close(self) -> None:
        return None

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"spans": self.spans, "rejected_attributes": self.rejected_attributes}

    # --- ledger entries --------------------------------------------------------------------
    def export_entries(self, entries: Iterable[StageEntry]) -> None:
        """One span per ops / cost ledger entry (the LIB-27 attribute set; ids and counts only)."""
        for entry in entries:
            span = self._start("memspine.stage", None, None)
            self._set(span, to_otel_attributes(entry))
            self._end(span, None)

    # --- internals -------------------------------------------------------------------------
    def _start(self, name: str, parent: Any, start_ns: int | None) -> Any:
        ctx = self._api.set_span_in_context(parent) if parent is not None else None
        span = self._tracer.start_span(name, context=ctx, start_time=start_ns)
        with self._lock:
            self.spans += 1
        return span

    def _set(self, span: Any, attrs: Mapping[str, Any]) -> None:
        clean, rejected = scrub_span_attributes(attrs)
        if rejected:
            with self._lock:
                self.rejected_attributes += len(rejected)
        for key, value in clean.items():
            span.set_attribute(key, value)

    @staticmethod
    def _end(span: Any, end_ns: int | None) -> None:
        span.end(end_time=end_ns) if end_ns is not None else span.end()

    def _request(self, trace: Mapping[str, Any], parent: Any) -> None:
        start = _start_ns(trace)
        kind = str(trace.get("kind", "request"))
        span = self._start(
            f"memspine.{kind}" if _CODE_RE.fullmatch(kind) else "memspine.request", parent, start
        )
        fields = trace.get("fields") or {}
        events = list(trace.get("events") or [])
        children = list(trace.get("children") or [])
        attrs: dict[str, Any] = {
            "memspine.request_id": trace.get("id"),
            "memspine.kind": kind,
            "memspine.scope_hash": trace.get("scope_hash"),
            "memspine.duration_ms": trace.get("duration_ms"),
            "memspine.events": len(events),
            "memspine.children": len(children),
        }
        for name in ("op", "stage"):
            if isinstance(fields.get(name), str):
                attrs[f"memspine.{name}"] = fields[name]
        self._set(span, attrs)
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for ev in events:
            groups.setdefault(_suffix_free(str(ev.get("stage", ""))), []).append(ev)
        for stage_name, evs in groups.items():
            self._stage(stage_name, evs, span, start)
        for child in children:
            self._request(child, span)
        end = None if start is None else start + int(float(trace.get("duration_ms") or 0) * 1e6)
        self._end(span, end)

    def _stage(
        self, name: str, evs: list[Mapping[str, Any]], parent: Any, root_ns: int | None
    ) -> None:
        offsets = [float(e.get("t_ms") or 0.0) for e in evs]
        start = None if root_ns is None else root_ns + int(min(offsets) * 1e6)
        end = None if root_ns is None else root_ns + int(max(offsets) * 1e6)
        span = self._start("memspine.stage", parent, start)
        attrs: dict[str, Any] = {"memspine.stage": name, "memspine.events": len(evs)}
        counts: dict[str, int] = {}
        for ev in evs:
            for fname, val in (ev.get("fields") or {}).items():
                if fname == "duration_ms" and isinstance(val, (int, float)):
                    attrs["memspine.duration_ms"] = val
                elif fname == "error" and isinstance(val, str):
                    attrs["memspine.error"] = val
                elif isinstance(val, int) and not isinstance(val, bool):
                    key = "memspine.n." + re.sub(r"[^a-z0-9_]", "_", str(fname).lower())
                    counts[key] = counts.get(key, 0) + val
        attrs.update(counts)
        self._set(span, attrs)
        self._end(span, end)
