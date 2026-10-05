"""structlog configuration + the M11 event vocabulary.

Every lifecycle emission uses one of the ``EVENT_*`` names below so logs are
greppable and exporters (Langfuse/OTel, later phases) can map them 1:1.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

from memspine.core.events import EventKind
from memspine.core.redaction import redact

__all__ = [
    "EVENT_CONFLICT",
    "EVENT_CONSOLIDATE",
    "EVENT_DECAY_TRANSITION",
    "EVENT_EXPOSE",
    "EVENT_FEEDBACK",
    "EVENT_FORGET",
    "EVENT_LINK",
    "EVENT_MARKER",
    "EVENT_MERGE",
    "EVENT_REBUILD",
    "EVENT_RETRIEVE",
    "EVENT_SESSION",
    "EVENT_WRITE",
    "configure_logging",
    "get_logger",
    "redact_error",
    "redact_error_fields",
]

# M11 vocabulary — derived from EventKind so log names and event kinds
# cannot drift (adding a kind updates both automatically).
EVENT_WRITE = EventKind.WRITE.value
EVENT_RETRIEVE = EventKind.RETRIEVE.value
EVENT_CONSOLIDATE = EventKind.CONSOLIDATE.value
EVENT_DECAY_TRANSITION = EventKind.DECAY_TRANSITION.value
EVENT_CONFLICT = EventKind.CONFLICT.value
EVENT_MERGE = EventKind.MERGE.value
EVENT_LINK = EventKind.LINK.value
EVENT_FORGET = EventKind.FORGET.value
EVENT_REBUILD = EventKind.REBUILD.value
EVENT_EXPOSE = EventKind.EXPOSE.value
EVENT_MARKER = EventKind.MARKER.value
EVENT_FEEDBACK = EventKind.FEEDBACK.value
EVENT_SESSION = EventKind.SESSION.value


#: Log fields that carry exception or error text (third-party messages can echo
#: credentials, DSNs or record content back).
_ERROR_FIELDS = frozenset({"error", "detail", "exc", "exception"})
_URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)
#: Error text past this many characters is cut: a long message is usually an
#: echoed payload, not a diagnosis.
_ERROR_MAX_CHARS = 500


def redact_error(value: object) -> str:
    """Error text safe for a log line: secrets, PII and URL credentials masked
    (:func:`memspine.core.redaction.redact`), then cut at 500 characters."""
    text = _URL_USERINFO.sub(r"\1***@", str(value))
    text = redact(text, pii=True)[0]
    if len(text) > _ERROR_MAX_CHARS:
        text = text[:_ERROR_MAX_CHARS] + "...[truncated]"
    return text


def redact_error_fields(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: :func:`redact_error` on every error-text field."""
    for key in _ERROR_FIELDS & set(event_dict):
        value = event_dict[key]
        if isinstance(value, str | BaseException):
            event_dict[key] = redact_error(value)
    return event_dict


def _utf8_console_stream() -> TextIO:
    """A UTF-8 sink over stdout's file descriptor, so a non-ASCII log field
    (``—``, ``→``, or unicode record content) never raises ``UnicodeEncodeError``
    on a legacy-codepage console (Windows cp1252). ``closefd=False`` means this
    view never closes the underlying fd, so the host's ``sys.stdout`` is left
    untouched. Falls back to ``sys.stdout`` when there is no real fd (pytest
    capture, an in-memory ``StringIO`` — both already unicode-safe)."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, io.UnsupportedOperation, ValueError):
        return sys.stdout
    return open(
        fd, mode="w", encoding="utf-8", errors="backslashreplace", closefd=False, buffering=1
    )


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Idempotent structlog setup. ``json_output=True`` for production pipelines."""
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            redact_error_fields,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        # UTF-8 sink so unicode in log fields never crashes on a cp1252 console.
        logger_factory=structlog.PrintLoggerFactory(file=_utf8_console_stream()),
        # False (structlog's default): caching binds loggers to a fixed processor
        # chain, which defeats ``structlog.testing.capture_logs`` — this config now
        # installs on first use everywhere (get_logger), so caching would break
        # capture across the suite. Logging is not a hot path; the cost is nil.
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    # Install the UTF-8-safe default on first use so a unicode log field never
    # crashes on a cp1252 console (Windows). A host that has already called
    # ``structlog.configure`` is respected — we never override an explicit setup.
    if not structlog.is_configured():
        configure_logging()
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
