"""structlog configuration + the M11 event vocabulary.

Every lifecycle emission uses one of the ``EVENT_*`` names below so logs are
greppable and exporters (Langfuse/OTel, later phases) can map them 1:1.
"""

from __future__ import annotations

import io
import logging
import sys
from typing import TextIO

import structlog

from memspine.core.events import EventKind

__all__ = [
    "EVENT_CONFLICT",
    "EVENT_CONSOLIDATE",
    "EVENT_DECAY_TRANSITION",
    "EVENT_FORGET",
    "EVENT_LINK",
    "EVENT_MERGE",
    "EVENT_REBUILD",
    "EVENT_RETRIEVE",
    "EVENT_WRITE",
    "configure_logging",
    "get_logger",
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
