"""Derive: the name to pipeline table runners register from (F-15)."""

from __future__ import annotations

from memspine.derive.pipeline.stages.anticipate import anticipate
from memspine.derive.pipeline.stages.communities import reorganize
from memspine.derive.pipeline.stages.consolidate import consolidate
from memspine.derive.pipeline.stages.context import Pipeline
from memspine.derive.pipeline.stages.entity_summaries import summarize_entities
from memspine.derive.pipeline.stages.extract_graph import extract_graph
from memspine.derive.pipeline.stages.histories import histories
from memspine.derive.pipeline.stages.maintenance import (
    compress,
    decay_sweep,
    session_lifecycle,
    sleep_compute,
)
from memspine.derive.pipeline.stages.mining import mine_facts, project_facts
from memspine.derive.pipeline.stages.predict import predict_calibrate
from memspine.derive.pipeline.stages.reconcile import reconcile
from memspine.derive.pipeline.stages.reflect import reflect_profile
from memspine.derive.pipeline.stages.retention import event_log_prune, retention_expire
from memspine.derive.pipeline.stages.rule_edges import rule_edges
from memspine.derive.pipeline.stages.watches import check_watches

#: Name -> pipeline. Runners register from this table; the M11-adjacent names
#: are stable identifiers used in schedules and dead-letter reporting.
PIPELINES: dict[str, Pipeline] = {
    "retention_expire": retention_expire,
    "consolidate": consolidate,
    "reorganize": reorganize,
    "extract_graph": extract_graph,
    "rule_edges": rule_edges,
    "reconcile": reconcile,
    "histories": histories,
    "summarize_entities": summarize_entities,
    "mine_facts": mine_facts,
    "project_facts": project_facts,
    "predict_calibrate": predict_calibrate,
    "anticipate": anticipate,
    "reflect_profile": reflect_profile,
    "check_watches": check_watches,
    "session_lifecycle": session_lifecycle,
    "decay_sweep": decay_sweep,
    "compress": compress,
    "sleep_compute": sleep_compute,
    "event_log_prune": event_log_prune,
}
