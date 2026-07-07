"""structlog configuration + the M11 event vocabulary.

Every lifecycle emission uses one of the ``EVENT_*`` names below so logs are
greppable and exporters (Langfuse/OTel, later phases) can map them 1:1.
"""

from __future__ import annotations

import logging

import structlog

from memspine.core.events import EventKind

__all__ = [
    "EVENT_CONFLICT",
    "EVENT_CONSOLIDATE",
    "EVENT_DECAY_TRANSITION",
    "EVENT_FORGET",
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
EVENT_FORGET = EventKind.FORGET.value
EVENT_REBUILD = EventKind.REBUILD.value


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
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
