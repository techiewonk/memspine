"""Shared read-path state moved out of the engine with the read path (F-10a): the per-read
context variables, the passive-session scope and the read result."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

import orjson

from memspine.compose.pack import (
    AssembledContext,
)
from memspine.config import constants
from memspine.core import trace_sink
from memspine.memories.episodic.lifecycle import session_of
from memspine.store.records import (
    MemoryRecord,
)

#: N3: tag stamped on a record archived by a taint rollback or repair, so the
#: C4' history filter still recognises it when the log no longer holds the event
#: (``event_log.mode: ephemeral`` or a rolling prune).
_TAINT_ARCHIVED_TAG = "taint_archived"


#: G-11 (Graphiti center node): the focal entity of the read in progress, if any.
_FOCAL_ENTITY: ContextVar[str | None] = ContextVar("memspine_focal_entity", default=None)


#: Search forensics (opt-in, evals/debugging): while a sink dict is installed by
#: :func:`search_forensics`, ``Engine._search`` records the ranked ``(record_id, score)``
#: list of every stage: each leg, the fusion, the gated candidate pool, the reranker's raw
#: scores and the final cut. Unset (the default) the hook is a single ``is not None`` test.
_FORENSICS = trace_sink.FORENSICS  # the same ContextVar, shared with the policies


#: R2-1b: the raw reranker score of the second-best candidate of the latest ``_search`` in
#: this task (None: no rerank ran, fewer than two candidates, or the rerank failed). The
#: ``bridge_hop_gate = "weak"`` signal: a lone confident anchor with no support.
_RERANK_SUPPORT: ContextVar[float | None] = ContextVar("memspine_rerank_support", default=None)


#: Whether the last ``_search`` of the current read reranked its candidates
#: (``read.rerank_floor="skip"`` needs it at assembly).
_RERANKED: ContextVar[bool] = ContextVar("memspine_reranked", default=False)


#: I30 (``read.abstain_on_raw``): record id -> the reranker's raw score (before min-max), merged
#: over the searches of the current read; None outside a read or when the option is off.
_RAW_SCORES: ContextVar[dict[str, float] | None] = ContextVar("memspine_raw_scores", default=None)


#: B1 (``read.list_mode``): True while the routed read searches for a list / set question;
#: the search then adds the speaker vote leg, widens the pool and skips ``rerank_keep``.
_LIST_MODE: ContextVar[bool] = ContextVar("memspine_list_mode", default=False)


@dataclass(frozen=True, slots=True)
class _PassiveScope:
    """#53: which PASSIVE-session records the running read may see."""

    include_all: bool = False
    sessions: frozenset[str] = frozenset()


#: #53: the passive-session scope of the read running in this task (None: default).
_PASSIVE_SCOPE: ContextVar[_PassiveScope | None] = ContextVar(
    "memspine_passive_scope", default=None
)


def _passive_hidden(record: MemoryRecord, group_id: str | None = None) -> bool:
    """#53: whether a PASSIVE-session record stays out of this read. A read that
    asks for passive records, names the session (``session_id``) or filters on the
    record's ``group_id`` sees it; every other read does not."""
    if not record.scoring.passive:
        return False
    if group_id is not None and record.group_id == group_id:
        return False
    scope = _PASSIVE_SCOPE.get()
    if scope is None:
        return True
    return not (scope.include_all or session_of(record) in scope.sessions)


@dataclass(frozen=True, slots=True)
class ReadResult:
    """C7': what :meth:`Engine.read` chose (``full``/``replay``/``retrieve``) and the context."""

    mode: str
    context: AssembledContext


#: C8': tag marking an anticipatory cue record (a retrieval key, never content).
CUE_TAG = constants.CUE_TAG


#: R1-2: how far :meth:`Engine.search` widens its leg windows (x1, x4, x16, x64) when
#: the gates leave fewer than ``top_k`` live candidates.
_SEARCH_MAX_WIDEN = 64


#: I2 (isolation review 2026-10-08): the records a read has listed, by (namespace,
#: memory_type, group_id). Set for the duration of one ``search`` / ``assemble`` /
#: ``read`` call (outermost wins), cleared by every append, so a read sees exactly what
#: an uncached read would see while listing each namespace at most once.
_READ_SNAPSHOT: ContextVar[dict[tuple[str, str | None, str | None], list[MemoryRecord]] | None] = (
    ContextVar("memspine_read_snapshot", default=None)
)


def _json_line(value: object) -> str:
    """#10: ``value`` as one line of JSON. orjson escapes quotes and control
    characters; the Unicode line and paragraph separators are escaped too, since
    a model may read them as line breaks."""
    text = orjson.dumps(value).decode()
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
