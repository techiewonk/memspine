"""structlog configuration + the M11 event vocabulary.

Every lifecycle emission uses one of the ``EVENT_*`` names below so logs are
greppable and exporters (Langfuse/OTel, later phases) can map them 1:1.
"""

from __future__ import annotations

import logging

import structlog

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

# M11 vocabulary — mirrors core.events.EventKind on purpose.
EVENT_WRITE = "memory.write"
EVENT_RETRIEVE = "memory.retrieve"
EVENT_CONSOLIDATE = "memory.consolidate"
EVENT_DECAY_TRANSITION = "memory.decay_transition"
EVENT_CONFLICT = "memory.conflict"
EVENT_MERGE = "memory.merge"
EVENT_FORGET = "memory.forget"
EVENT_REBUILD = "memory.rebuild"


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
    return structlog.get_logger(name)
