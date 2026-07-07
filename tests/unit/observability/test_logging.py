"""Logging vocabulary stays in lockstep with the event log (M11)."""

from __future__ import annotations

from memspine.core.events import EventKind
from memspine.observability import logging as obs_logging


def test_m11_vocabulary_matches_event_kinds() -> None:
    vocab = {
        obs_logging.EVENT_WRITE,
        obs_logging.EVENT_RETRIEVE,
        obs_logging.EVENT_CONSOLIDATE,
        obs_logging.EVENT_DECAY_TRANSITION,
        obs_logging.EVENT_CONFLICT,
        obs_logging.EVENT_MERGE,
        obs_logging.EVENT_LINK,
        obs_logging.EVENT_FORGET,
        obs_logging.EVENT_REBUILD,
    }
    assert vocab == {kind.value for kind in EventKind}


def test_configure_and_log_smoke() -> None:
    obs_logging.configure_logging(level="DEBUG")
    log = obs_logging.get_logger("test")
    log.info(obs_logging.EVENT_WRITE, namespace="ns/a")  # must not raise
