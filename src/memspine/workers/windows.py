"""W1: call 1 of the adaptive-window write (``write.window_extract``).

``docs/ENGINE_COMPARISON_AND_WRITE_PATH_DESIGN.html`` ("Decided design"): a store's turns are
cut into adaptive windows (:mod:`memspine.core.window`) and ONE structured ``window_extract``
call per window yields independent per-layer sections:

* ``facts``: atomic, dated, grounded facts (a verbatim ``span`` of a cited turn), deposited as
  semantic ``atomic_fact`` records through the mined-fact door (``parents`` = the cited turns,
  so the forget cascade, the firewall, the trust cap and the I72 liveness re-check all apply);
* ``edges``: typed entity relationships, handed to the sleep stage ``extract_graph`` (call 3),
  which resolves, invalidates and summarises them like its own extraction;
* ``episode``: a narrative fragment of the window, citing its turns.

Each section is validated on its own and fails closed alone: a malformed section is dropped and
counted, the others still land. Split mode turns the one call into two (facts+edges / episode)
when the JSON of the combined call keeps failing. Calls are cached by the window's text, so a
re-run on the same history is free.

Plain async step functions and one small manager; no runner imports (D-17). Nothing here reads
gold or a benchmark; the topic level is the store's own (see :class:`~memspine.core.window.
WindowSegmenter`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol

from memspine.config import constants
from memspine.core.window import (
    Window,
    WindowSegmenter,
    WindowSettings,
    WindowTurn,
    estimate_tokens,
)
from memspine.derive import role_filter
from memspine.derive.backpressure import BackpressureRejected
from memspine.derive.call_batcher import BatchItem, CallBatcher
from memspine.derive.pipeline.stages.context import SessionClosed, WindowEdgeBatch
from memspine.derive.pipeline.stages.mining import _fact_date
from memspine.derive.segmenter import SegmentationSettings, StreamSegmenter
from memspine.economy import ledger as _costs
from memspine.models.concurrency import Priority, priority_scope
from memspine.models.providers.llm.base import LLMService
from memspine.models.providers.llm.structured import structured_call
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.prompts.base import Prompt
from memspine.prompts.models import (
    ExtractedEdge,
    WindowEpisode,
    WindowFact,
    WindowPayload,
    fact_guard,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.store.events import fingerprint_payload
from memspine.store.records import MemoryRecord, RecordStatus

__all__ = [
    "SECTION_MODES",
    "WINDOW_STAGE",
    "PromptWindowExtractor",
    "WindowCallFailed",
    "WindowExtractor",
    "WindowPipeline",
    "build_transcript",
    "grounded",
    "split_batch_section",
    "validate_edges",
    "validate_episode",
    "validate_facts",
]

_log = get_logger(__name__)

#: ``write.window_extract`` -> the sections one window call fills.
SECTION_MODES: dict[str, tuple[str, ...]] = {
    "off": (),
    "facts": ("facts",),
    "facts_graph": ("facts", "edges"),
    "all": ("facts", "edges", "episode"),
}

#: F-04: the outbox stage (and read-barrier capability) of the window pipeline.
WINDOW_STAGE = "window"
#: F-04: the tenant of a window job's ``OwnerScope`` (the namespace is the owner).
_TENANT = "default"
_LINE_CHARS = 2000
#: ``stop()`` waits this long for the window call in hand before cancelling it.
_STOP_GRACE_S = 5.0
_TOKENS = re.compile(r"\w+", re.UNICODE)
_NAME_PREFIX = re.compile(r"^\s*([A-Z][\w'-]{1,30}(?: [A-Z][\w'-]{1,30})?):\s+\S")


class WindowExtractor(Protocol):
    """The call-1 model call: ``(sections, prompt variables) -> raw section payload``.

    ``identity`` names the model and prompt version; it is part of the cache key. Raising
    means the whole call failed (unparseable reply, transport error)."""

    identity: str

    async def __call__(
        self, sections: Sequence[str], variables: Mapping[str, object]
    ) -> Mapping[str, Any]: ...


class PromptWindowExtractor:
    """:class:`WindowExtractor` over the ``window_extract`` prompt and a structured call."""

    def __init__(self, llm: LLMService, prompt: Prompt, model: str = "") -> None:
        self._llm = llm
        self._prompt = prompt
        self.identity = f"{model}|{prompt.prompt_version}"

    async def __call__(
        self, sections: Sequence[str], variables: Mapping[str, object]
    ) -> Mapping[str, Any]:
        options: dict[str, Any] = {"temperature": 0.0}
        if self._prompt.token_budget:
            options["max_tokens"] = self._prompt.token_budget
        out = await structured_call(
            self._llm,
            self._prompt,
            {**variables, "sections": list(sections)},
            WindowPayload,
            **options,
        )
        # A section the reply omitted is absent (not null): the writer fails it closed.
        return {name: getattr(out, name) for name in sections if name in out.model_fields_set}


#: ``(namespace, text, entity, attribute, parents, valid_from, session_key, **views)``: the
#: engine's mined-fact door (:class:`~memspine.derive.pipeline.stages.context.DepositFact`).
DepositFactFn = Callable[..., Awaitable[object]]
#: ``(namespace, text, parents, valid_from, window_id, require_live)``: the episode door.
DepositEpisodeFn = Callable[..., Awaitable[object]]
#: ``(namespace, session_id, info)``: the session-closed hook (call 2 interface).
SessionClosedFn = Callable[[SessionClosed], Awaitable[object]]
GetRecord = Callable[[str], Awaitable[MemoryRecord | None]]
Embed = Callable[[list[str]], Awaitable[list[list[float]]]]


# --- pure helpers (transcript, grounding, per-section validation) ---------------------------


def _norm(text: str) -> str:
    return " ".join(_TOKENS.findall(text.casefold()))


def grounded(span: str, line_texts: Sequence[str]) -> bool:
    """True when ``span`` (a model-quoted excerpt) occurs in one of ``line_texts``, compared
    on lower-cased word tokens (case, punctuation and spacing ignored, word order kept)."""
    needle = _norm(span)
    if not needle:
        return False
    return any(f" {needle} " in f" {_norm(text)} " for text in line_texts)


def _speaker_label(record: MemoryRecord) -> str:
    for tag in record.tags:
        if tag.startswith("spk:") and tag[4:]:
            return tag[4:]
    return record.source.role or "speaker"


def build_transcript(records: Sequence[MemoryRecord]) -> tuple[str, list[str], list[str]]:
    """``(transcript, line texts, participants)`` of a window: ``[n] [YYYY-MM-DD] Speaker:
    text`` lines (1-based n). A turn whose text already starts with ``Name:`` keeps it;
    otherwise the speaker tag (else the role) is prefixed."""
    lines: list[str] = []
    texts: list[str] = []
    people: list[str] = []
    for n, record in enumerate(records, 1):
        flat = " ".join(record.content.split())[:_LINE_CHARS]
        match = _NAME_PREFIX.match(flat)
        if match:
            who, shown = match.group(1), flat
        else:
            who = _speaker_label(record)
            shown = f"{who}: {flat}"
        if who not in people:
            people.append(who)
        texts.append(shown)
        lines.append(f"[{n}] [{record.valid_from:%Y-%m-%d}] {shown}")
    return "\n".join(lines), texts, people


def _valid_lines(cited: Sequence[int], count: int) -> list[int]:
    return [n for n in dict.fromkeys(cited) if 1 <= n <= count]


@dataclass
class FactsOutcome:
    facts: list[tuple[WindowFact, list[int]]] = field(default_factory=list)
    returned: int = 0
    invalid: int = 0
    ungrounded: int = 0
    uncited: int = 0


def validate_facts(raw: object, line_texts: Sequence[str]) -> FactsOutcome | None:
    """The ``facts`` section: None (section failed closed) unless it is a list; otherwise each
    item is guarded and validated alone. An item with no valid cited line, or whose ``span``
    is not in a cited line, is dropped as ungrounded. Returns kept ``(fact, valid lines)``."""
    if not isinstance(raw, list):
        return None
    out = FactsOutcome(returned=len(raw))
    for item in raw:
        guarded = fact_guard(item)
        if guarded is None or not isinstance(guarded, dict):
            out.invalid += 1
            continue
        try:
            fact = WindowFact.model_validate(guarded)
        except Exception:
            out.invalid += 1
            continue
        lines = _valid_lines(fact.turns, len(line_texts))
        if not lines:
            out.uncited += 1
            out.ungrounded += 1
            continue
        if not grounded(fact.span, [line_texts[n - 1] for n in lines]):
            out.ungrounded += 1
            continue
        out.facts.append((fact, lines))
    return out


@dataclass
class EdgesOutcome:
    edges: list[tuple[ExtractedEdge, list[int]]] = field(default_factory=list)
    returned: int = 0
    invalid: int = 0
    ungrounded: int = 0


def validate_edges(raw: object, count: int) -> EdgesOutcome | None:
    """The ``edges`` section: None unless a list. An edge needs all four text fields and at
    least one valid cited line (``turns``), else it is dropped."""
    if not isinstance(raw, list):
        return None
    out = EdgesOutcome(returned=len(raw))
    for item in raw:
        if not isinstance(item, dict):
            out.invalid += 1
            continue
        data = dict(item)
        data.setdefault("episode_indices", data.get("turns"))
        try:
            edge = ExtractedEdge.model_validate(data)
        except Exception:
            out.invalid += 1
            continue
        parts = (edge.src_entity, edge.rel, edge.dst_entity, edge.fact)
        if not all(p.strip() for p in parts) or any(
            len(p) > constants.MULTIVIEW_FIELD_MAX_CHARS * 4 for p in parts
        ):
            out.invalid += 1
            continue
        lines = _valid_lines(edge.episode_indices, count)
        if not lines:
            out.ungrounded += 1
            continue
        out.edges.append((edge, lines))
    return out


@dataclass
class EpisodeOutcome:
    episode: WindowEpisode | None = None
    lines: list[int] = field(default_factory=list)
    invalid: int = 0
    uncited: int = 0
    truncated: int = 0


def validate_episode(raw: object, count: int) -> EpisodeOutcome | None:
    """The ``episode`` section: None (failed closed) unless an object (a one-element list is
    unwrapped). An empty narrative is a valid "nothing to report"; a narrative over the cap is
    cut at a word; an episode citing no valid line is kept with the whole window as evidence."""
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if raw is None or raw == "" or raw == []:
        return EpisodeOutcome()
    if not isinstance(raw, dict):
        return None
    out = EpisodeOutcome()
    try:
        episode = WindowEpisode.model_validate(raw)
    except Exception:
        out.invalid += 1
        return out
    if not episode.narrative:
        return out
    if len(episode.narrative) > constants.WINDOW_EPISODE_MAX_CHARS:
        cut = episode.narrative[: constants.WINDOW_EPISODE_MAX_CHARS]
        space = cut.rfind(" ")
        episode = episode.model_copy(
            update={"narrative": (cut[:space] if space > len(cut) // 2 else cut).rstrip()}
        )
        out.truncated += 1
    lines = _valid_lines(episode.turns, count)
    if not lines:
        out.uncited += 1
        lines = list(range(1, count + 1))
    out.episode = episode
    out.lines = lines
    return out


def _int_lines(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [n for n in value if isinstance(n, int) and not isinstance(n, bool)]


def split_batch_section(
    section: str, raw: object, spans: Sequence[tuple[int, int]]
) -> tuple[list[Any], int]:
    """Split the reply of a batched call into one reply per window (W2-05).

    ``spans[w] = (lines before window w, its line count)``: the batched transcript numbers lines
    on across the windows, so a cited line names exactly one window. Each item of a ``facts`` /
    ``edges`` list goes to the window all its cited lines fall in, with the citations renumbered
    to that window's own 1..n (so :func:`validate_facts` / :func:`validate_edges` check its span
    against that window's lines only). An item citing lines of two windows is dropped and counted
    (second value); one citing no valid line goes to the first window, where validation counts it
    once. A reply that is not a list goes to every window unchanged (each fails closed alone)."""
    if not isinstance(raw, list):
        return [raw for _ in spans], 0
    parts: list[list[Any]] = [[] for _ in spans]
    cross = 0

    def window_of(line: int) -> int | None:
        for w, (base, count) in enumerate(spans):
            if base < line <= base + count:
                return w
        return None

    for item in raw:
        if not isinstance(item, dict):
            parts[0].append(item)
            continue
        keys = [k for k in ("turns", "episode_indices") if k in item]
        cited = [n for k in keys for n in _int_lines(item[k])]
        owners = {w for n in cited if (w := window_of(n)) is not None}
        if len(owners) > 1:
            cross += 1
            continue
        if not owners:
            parts[0].append(item)
            continue
        w = owners.pop()
        base = spans[w][0]
        local = dict(item)
        for k in keys:
            local[k] = [n - base for n in _int_lines(item[k]) if window_of(n) == w]
        parts[w].append(local)
    return parts, cross


# --- the manager ----------------------------------------------------------------------------


def _cost_scope(namespace: str, layer: str) -> contextlib.AbstractContextManager[None]:
    """W2-12: attribute a window call to its namespace, layer and the ``window_extract`` stage;
    a plain null context when no cost ledger is installed (flag off: nothing changes)."""
    if _costs.installed() is None:
        return nullcontext()
    return _costs.cost_scope(namespace=namespace, layer=layer, stage="window_extract")


class WindowCallFailed(RuntimeError):
    """Every model call of a window failed (outbox mode): the offset becomes a hole and is
    retried within the per-hole budget instead of being dropped."""


@dataclass
class _Prepared:
    """Output of the lock-free phase of one window (F-04): validated inputs for the commit."""

    job: _Job
    records: list[MemoryRecord]
    merged: dict[str, Any]
    line_texts: list[str]
    window_id: str
    groups: int
    failed: bool


@dataclass
class _Job:
    namespace: str
    window: Window | None = None
    #: a session-closed sentinel (processed in queue order, after the session's windows)
    closed_session: str | None = None
    cancelled: bool = False
    members: frozenset[str] = frozenset()
    #: W2-05: what the batcher needs (estimated tokens, when the window closed)
    tokens: int = 0
    queued_at: float = 0.0


@dataclass
class _Store:
    segmenter: WindowSegmenter | StreamSegmenter
    queue: deque[_Job] = field(default_factory=deque)
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker: asyncio.Task[None] | None = None
    idle: asyncio.TimerHandle | None = None
    touch: int = 0
    session: str | None = None
    pending: list[WindowEdgeBatch] = field(default_factory=list)
    inflight: _Job | None = None
    #: W2-05: the jobs of a batched call in hand (a forget cancels the one it touches)
    inflight_batch: list[_Job] = field(default_factory=list)
    batch_timer: asyncio.TimerHandle | None = None
    session_windows: Counter[str] = field(default_factory=Counter)


class WindowPipeline:
    """The window manager of one engine: buffers turns into windows, runs call 1 per window
    (async in live mode, at ``flush`` in batch mode), counts everything (G-W13)."""

    def __init__(
        self,
        *,
        settings: WindowSettings,
        sections: Sequence[str],
        extractor: WindowExtractor,
        get_record: GetRecord,
        deposit_fact: DepositFactFn | None,
        deposit_episode: DepositEpisodeFn | None = None,
        embed: Embed | None = None,
        lock: Callable[[str], AbstractAsyncContextManager[None]] | None = None,
        cache: Any = None,
        use_cache: bool = True,
        split: str = "auto",
        asynchronous: bool = True,
        on_session_closed: SessionClosedFn | None = None,
        outbox: Any | None = None,  # runtime.runner.InlineRunner; untyped (workers is below runtime)
        segmentation: SegmentationSettings | None = None,
        clock: Callable[[], float] = time.time,
        role_policy: bool = False,
    ) -> None:
        self._role_policy = role_policy  # W2-03: write.role_policy_v2
        self.settings = settings
        self.sections = tuple(sections)
        self._extract = extractor
        self._get = get_record
        self._deposit_fact = deposit_fact
        self._deposit_episode = deposit_episode
        self._embed = embed
        self._lock = lock or (lambda _ns: nullcontext())
        self._cache = cache if use_cache else None
        self._split = split
        self._async = asynchronous
        self._on_closed = on_session_closed
        self._stores: dict[str, _Store] = {}
        self._counts: Counter[str] = Counter()
        self._reasons: Counter[str] = Counter()
        self._calls_by_layer: Counter[str] = Counter()
        self._dropped_by_layer: Counter[str] = Counter()
        self._recent: deque[bool] = deque(maxlen=constants.WINDOW_SPLIT_RECENT)
        self._auto_split = False
        self._keys_by_record: dict[str, set[str]] = {}
        self._stopped = False
        #: F-04 (``workers.outbox``): the durable queue and its runner; None = in-memory deque.
        self._outbox = outbox
        self._recover_task: asyncio.Task[Any] | None = None
        self._staged: list[tuple[str, dict[str, Any]]] = []
        self._persist_retry: asyncio.Task[None] | None = None  # RT-02 backpressure retry
        self._persist_lock = asyncio.Lock()
        self._recovered = outbox is None
        #: W2-05 (``write.segmentation_v2``): time-gap/adaptive segmenter and the call batcher.
        self._seg = segmentation
        self._clock = clock
        self._batch_sections = tuple(s for s in self.sections if s != "episode")
        self._batcher: CallBatcher | None = None
        if segmentation is not None and segmentation.batching and self._batch_sections:
            self._batcher = CallBatcher(
                max_tokens=segmentation.batch_max_tokens,
                max_windows=segmentation.batch_max_windows,
                max_age_s=segmentation.batch_max_age_s,
            )
        #: outbox mode: results of a batched call for the offsets after the one that made it
        self._prefetched: dict[tuple[str, int], _Prepared | None] = {}
        self._batch_reason = ""
        if outbox is not None:
            outbox.register(WINDOW_STAGE, prepare=self._outbox_prepare, commit=self._outbox_commit)
        #: (namespace, session) -> what the session-closed hook returned (the last 100).
        self.closed_results: dict[tuple[str, str], object] = {}

    @property
    def asynchronous(self) -> bool:
        """True when window calls run in background tasks (live mode), False when they run
        only at ``flush`` / ``close_session`` / ``sleep`` (batch mode)."""
        return self._async

    # -- feeding ------------------------------------------------------------------------

    def _store(self, namespace: str) -> _Store:
        store = self._stores.get(namespace)
        if store is None:
            if self._seg is not None:
                store = _Store(segmenter=StreamSegmenter(self.settings, segmentation=self._seg))
            else:
                store = _Store(segmenter=WindowSegmenter(self.settings))
            self._stores[namespace] = store
        return store

    async def observe(self, record: MemoryRecord, session: str | None) -> None:
        """Add one just-written episodic turn. Quarantined or instruction-flagged turns never
        enter a window (text from them is excluded from every extraction input)."""
        if self._stopped or record.quarantined or record.instruction_flag:
            return
        if record.status is not RecordStatus.ACTIVATED:
            return
        self._recover_in_background()
        ns = record.namespace
        store = self._store(ns)
        vector: list[float] | None = None
        if self._embed is not None and self.settings.topic_k > 0.0:
            try:
                vector = (await self._embed([record.content]))[0]
            except Exception as exc:  # the topic boundary is an enhancer, never a gate
                _log.warning("window.embed_failed", error=str(exc))
        turn = WindowTurn(
            record_id=record.record_id,
            text=record.content,
            session=session,
            when=record.valid_from,
            vector=vector,
            tokens=estimate_tokens(record.content),
        )
        self._counts["turns"] += 1
        # A new session started: the old one is closed. Its open window is cut by the
        # segmenter below; the sentinel runs after it in queue order.
        closing = store.session if store.session is not None and session != store.session else None
        for window in store.segmenter.add(turn):
            self._enqueue(ns, store, window)
        if closing is not None:
            self._enqueue_closed(ns, store, closing)
        store.session = session
        if session is not None:
            store.session_windows.setdefault(session, 0)
        self._arm_idle(ns, store)
        await self._persist_staged()

    def _scope(self, ns: str) -> OwnerScope:
        return OwnerScope(tenant=_TENANT, owner=ns)

    def _enqueue(self, ns: str, store: _Store, window: Window) -> None:
        if self._outbox is not None:
            self._staged.append(
                (
                    ns,
                    {
                        "kind": "window",
                        "session": window.session,
                        "reason": window.reason,
                        "record_ids": window.record_ids,
                        **(
                            {"tokens": window.tokens, "staged_at": self._clock()}
                            if self._batcher is not None
                            else {}
                        ),
                    },
                )
            )
        else:
            job = _Job(
                ns,
                window=window,
                members=frozenset(window.record_ids),
                tokens=window.tokens,
                queued_at=self._clock(),
            )
            store.queue.append(job)
        self._counts["windows"] += 1
        self._reasons[window.reason] += 1
        if self._seg is not None and tap.enabled():
            tap.event(
                "write.segment.cut",
                reason=window.reason,
                turns=len(window.turns),
                tokens=window.tokens,
                grown=len(window.turns) > self.settings.max_turns,
            )
        if tap.enabled():
            tap.event(
                "write.window.cut",
                reason=window.reason,
                turns=len(window.turns),
                tokens=window.tokens,
                session=window.session,
                queued=len(store.queue),
            )
        if window.session is not None:
            store.session_windows[window.session] += 1
        if self._async and self._outbox is None:
            self._ensure_worker(ns, store)

    def _enqueue_closed(self, ns: str, store: _Store, session: str) -> None:
        if self._outbox is not None:
            self._staged.append(
                (
                    ns,
                    {
                        "kind": "closed",
                        "session": session,
                        # the count travels with the item: the in-memory tally dies with the process
                        "windows": store.session_windows.pop(session, 0),
                    },
                )
            )
        else:
            store.queue.append(_Job(ns, closed_session=session))
        if self._async and self._outbox is None:
            self._ensure_worker(ns, store)

    async def _persist_staged(self, *, force: bool = False) -> None:
        """Outbox mode: write the items ``_enqueue`` staged (in order), then wake the workers.
        Staging keeps the segmenter code synchronous and in its old order; the database write
        happens here, off the event loop (``InlineRunner.enqueue``)."""
        if self._outbox is None or not self._staged:
            return
        deferred_s: float | None = None
        async with self._persist_lock:
            staged, self._staged = self._staged, []
            for at, (ns, payload) in enumerate(staged):
                try:
                    if force:  # flush / session close: accept past the hard cap, never defer
                        await self._outbox.enqueue(
                            self._scope(ns), WINDOW_STAGE, payload, force=True
                        )
                    else:
                        await self._outbox.enqueue(self._scope(ns), WINDOW_STAGE, payload)
                except BackpressureRejected as rejected:
                    # RT-02 (W-25 B): refused BEFORE it was accepted, so nothing was written; keep
                    # this item and the rest staged, in order, and offer them again later.
                    self._staged = staged[at:] + self._staged
                    staged = staged[:at]
                    self._counts["windows_deferred_backpressure"] += 1
                    deferred_s = rejected.receipt.retry_after_s
                    break
        if self._async and not self._stopped:
            for ns in dict.fromkeys(n for n, _ in staged):
                self._ensure_worker(ns, self._store(ns))
            if deferred_s is not None:
                self._retry_persist_later(deferred_s)

    def _retry_persist_later(self, delay_s: float) -> None:
        """Offer the deferred (backpressured) items again after the receipt's retry time."""
        if self._persist_retry is not None and not self._persist_retry.done():
            return
        loop = asyncio.get_running_loop()

        async def again() -> None:
            await asyncio.sleep(max(delay_s, 0.01))
            self._persist_retry = None
            if not self._stopped:
                await self._persist_staged()

        self._persist_retry = loop.create_task(again())

    # -- timers and workers ---------------------------------------------------------------

    def _arm_idle(self, ns: str, store: _Store) -> None:
        if not self._async or self.settings.idle_s <= 0.0:
            return
        if store.idle is not None:
            store.idle.cancel()
        store.touch += 1
        token = store.touch
        loop = asyncio.get_running_loop()
        store.idle = loop.call_later(
            self.settings.idle_s, lambda: loop.create_task(self._idle_cut(ns, store, token))
        )

    async def _idle_cut(self, ns: str, store: _Store, token: int) -> None:
        if self._stopped or token != store.touch:
            return  # a later write re-armed the timer
        window = store.segmenter.close("idle")
        if window is not None:
            self._enqueue(ns, store, window)
            await self._persist_staged()

    def _ensure_worker(self, ns: str, store: _Store) -> None:
        if store.worker is not None and not store.worker.done():
            return
        with priority_scope(Priority.DERIVE):  # RT-02: window work is background derive
            store.worker = asyncio.get_running_loop().create_task(self._drain(ns, store))

    async def _drain(self, ns: str, store: _Store, *, final: bool = False) -> None:
        """Run what is queued. ``final`` (a flush, a session close) never holds a window back
        for company (W2-05 batching); the background worker does, up to the batch age."""
        if self._outbox is not None:
            if self._batcher is not None and not final and not self._outbox_batch_ready(ns, store):
                return
            await self._outbox.drain(self._scope(ns))
            return
        async with store.run_lock:
            while store.queue and not self._stopped:
                if self._batcher is not None:
                    jobs = self._take_batch(ns, store, final)
                    if jobs is None:
                        return  # held: the timer (or the next window) wakes the worker
                    if len(jobs) > 1:
                        await self._drain_batch(ns, store, jobs)
                        continue
                    job = jobs[0]
                    store.queue.remove(job)
                else:
                    job = store.queue.popleft()
                store.inflight = job
                try:
                    if job.closed_session is not None:
                        with tap.request("job", stage="window_session_closed"):
                            await self._session_closed(ns, store, job.closed_session)
                    else:
                        with tap.request("job", stage="window_extract"):
                            await self._run_job(ns, store, job)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad window never stops the queue
                    self._counts["jobs_failed"] += 1
                    tap.event("write.window.job_failed", error=type(exc).__name__)
                    _log.warning("window.job_failed", namespace=ns, error=str(exc), exc_info=True)
                finally:
                    store.inflight = None

    async def flush(self, namespace: str | None = None, *, final: bool = True) -> dict[str, Any]:
        """Process every closed window now. ``final`` also closes the open ones (and fires the
        session-closed hook for the sessions still open): the end of a stream or a batch."""
        if self._outbox is not None:
            await self._await_recovery()
        names = [namespace] if namespace is not None else list(self._stores)
        for ns in names:
            store = self._stores.get(ns)
            if store is None:
                continue
            if store.idle is not None:
                store.idle.cancel()
                store.idle = None
            if final:
                window = store.segmenter.close("flush")
                if window is not None:
                    self._enqueue(ns, store, window)
                if store.session is not None:
                    self._enqueue_closed(ns, store, store.session)
                    store.session = None
                await self._persist_staged(force=True)  # an explicit flush defers nothing
            if store.worker is not None and not store.worker.done():
                await asyncio.shield(store.worker)
            await self._drain(ns, store, final=True)
        if self._outbox is not None and namespace is None:
            await self._outbox.drain()  # leftovers of namespaces not seen since start
        return self.stats()

    async def close_session(self, namespace: str, session_id: str | None = None) -> dict[str, Any]:
        """The session-closed hook: cut the open window of ``session_id`` (default: the
        namespace's current session), process the namespace's queue in order, fire the call-2
        interface once. Returns what the hook returned."""
        store = self._stores.get(namespace)
        if store is None:
            return {"status": "skipped", "reason": "no window state for this namespace"}
        session = session_id if session_id is not None else store.session
        if session is None:
            return {"status": "skipped", "reason": "no open session"}
        open_turns = store.segmenter.open_turns
        if open_turns and open_turns[-1].session == session:
            window = store.segmenter.close("session_end")
            if window is not None:
                self._enqueue(namespace, store, window)
        if store.session == session:
            store.session = None
        before = self._counts["sessions_closed"]
        self._enqueue_closed(namespace, store, session)
        await self._persist_staged(force=True)
        if store.worker is not None and not store.worker.done():
            await asyncio.shield(store.worker)
        await self._drain(namespace, store, final=True)
        return {
            "status": "ok",
            "session_id": session,
            "closed": self._counts["sessions_closed"] - before,
            "hook": self.closed_results.get((namespace, session)),
        }

    async def _session_closed(
        self, ns: str, store: _Store, session: str, windows: int | None = None
    ) -> None:
        info = SessionClosed(
            namespace=ns,
            session_id=session,
            windows=store.session_windows.pop(session, 0) if windows is None else windows,
        )
        self._counts["sessions_closed"] += 1
        if self._on_closed is not None:
            self.closed_results[(ns, session)] = await self._on_closed(info)
            while len(self.closed_results) > 100:  # the last few, for close_session's reply
                self.closed_results.pop(next(iter(self.closed_results)))

    async def stop(self) -> None:
        """Cancel timers and workers. Windows still queued are abandoned (counted)."""
        self._stopped = True
        if self._outbox is not None:
            # ER-10: bounded drain. The offset in hand finishes (or its job is released after the
            # grace); everything else stays in the outbox and resumes at the next start.
            if self._recover_task is not None and not self._recover_task.done():
                self._recover_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._recover_task
            await self._outbox.stop(_STOP_GRACE_S)
        self._prefetched.clear()
        for store in self._stores.values():
            if store.idle is not None:
                store.idle.cancel()
            if store.batch_timer is not None:
                store.batch_timer.cancel()
                store.batch_timer = None
            if store.worker is not None and not store.worker.done():
                # let the job in hand finish its writes (the drain loop stops after it), but
                # never wait on a stuck model call for long
                await asyncio.wait({store.worker}, timeout=_STOP_GRACE_S)
                if not store.worker.done():
                    store.worker.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await store.worker
            self._counts["jobs_abandoned"] += sum(1 for j in store.queue if j.window is not None)
            store.queue.clear()
        if self._outbox is not None:
            self._counts["jobs_persisted"] += self._outbox.depth()
            self._outbox.close()

    # -- outbox (F-04) ----------------------------------------------------------------------

    def _recover_in_background(self) -> None:
        """First write of a process: resume leftovers of the last run (async mode only; in batch
        mode ``flush`` drains everything, as for a fresh queue)."""
        if self._recovered or self._outbox is None:
            return
        self._recovered = True
        if self._async:
            self._recover_task = asyncio.get_running_loop().create_task(self._outbox.start())

    async def _await_recovery(self) -> None:
        self._recovered = True
        task = self._recover_task
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def readiness(
        self,
        namespace: str,
        capabilities: Sequence[str] = (WINDOW_STAGE,),
        timeout: float = 0.0,  # noqa: ASYNC109 - the barrier budget
    ) -> Any:
        """Read barrier over the outbox (see ``InlineRunner.wait``); None when it is off."""
        if self._outbox is None:
            return None
        await self._await_recovery()
        return await self._outbox.wait(self._scope(namespace), list(capabilities), timeout)

    def _forget_in_item(self, payload: dict[str, Any], record_id: str) -> dict[str, Any] | None:
        """A queued item loses a forgotten turn; an item left with no turn becomes a no-op."""
        ids = payload.get("record_ids")
        if payload.get("kind") != "window" or not isinstance(ids, list) or record_id not in ids:
            return None
        self._counts["turns_dropped_forget"] += 1
        kept = [r for r in ids if r != record_id]
        if not kept:
            self._counts["windows_cancelled_forget"] += 1
            return {**payload, "kind": "empty", "record_ids": []}
        return {**payload, "record_ids": kept}

    async def _outbox_prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        """Outbox handler, phase 1 (no lock held): the window's model calls, or the session
        hook (call 2, a model call in waiting)."""
        ns = scope.owner
        store = self._store(ns)
        kind = payload.get("kind")
        if kind == "closed":
            return ("closed", payload)
        if kind != "window":
            return None
        ledger = _costs.installed()
        if ledger is not None and ledger.budget is not None and ledger.budget_exhausted(ns):
            # W2-12 (W-26 D): the namespace's budget is spent. No model call; the commit phase
            # parks the item underived in the outbox, and ``resume_deferred`` derives it later.
            ledger.budget.note_paused(ns)
            self._counts["windows_deferred_budget"] += 1
            return ("deferred", payload)
        window = Window(
            turns=[WindowTurn(record_id=r, text="", tokens=1) for r in payload["record_ids"]],
            reason=str(payload.get("reason", "")),
            session=payload.get("session"),
        )
        if self._batcher is not None:  # W2-05: a batched call may have prepared this offset already
            if (ns, offset) in self._prefetched:
                ready = self._prefetched.pop((ns, offset))
                store.inflight = ready.job if ready is not None else None
                return ("window", ready)
            batched = await self._outbox_prepare_batch(ns, store, offset)
            if batched is not _MISSING:
                return ("window", batched)
        job = _Job(ns, window=window, members=frozenset(window.record_ids))
        store.inflight = job  # a forget during the call cancels it
        try:
            prepared = await self._prepare_job(ns, job, strict=True)
        except BaseException:
            store.inflight = None
            raise
        return ("window", prepared)

    # -- batching (W2-05) ---------------------------------------------------------------------

    def _outbox_items(self, ns: str, start: int | None) -> tuple[list[BatchItem], bool]:
        """The leading run of queued window items (from offset ``start``) and whether a
        non-window item (a session close) follows them, which makes the run final."""
        assert self._outbox is not None
        scope = self._scope(ns)
        items: list[BatchItem] = []
        sentinel = False
        for offset, payload in self._outbox.pending_items(scope, WINDOW_STAGE):
            if start is not None and offset < start:
                continue
            if payload.get("kind") != "window":
                sentinel = True
                break
            items.append(
                BatchItem(
                    (scope.tenant, scope.owner),
                    (offset, payload),
                    max(int(payload.get("tokens", 1)), 1),
                    float(payload.get("staged_at", 0.0)),  # no stamp (old row): old enough
                )
            )
        return items, sentinel

    async def _outbox_prepare_batch(self, ns: str, store: _Store, offset: int) -> Any:
        """Outbox mode: if ``offset`` opens a batch of 2+ queued windows, make ONE call for all of
        them, keep the other windows' results for their own offsets and return this one's.
        ``_MISSING`` when the window is to run alone. A crash loses nothing: every window is
        still its own durable row, the stash is only a cache."""
        assert self._batcher is not None
        items, _ = self._outbox_items(ns, offset)
        if not items or items[0].ref[0] != offset:  # type: ignore[index]
            return _MISSING
        plans = self._batcher.plan(items, self._clock(), final=True)
        batch = plans[0].items if plans else ()
        if len(batch) < 2:
            return _MISSING
        jobs: list[_Job] = []
        for item in batch:
            payload = item.ref[1]  # type: ignore[index]
            window = Window(
                turns=[WindowTurn(record_id=r, text="", tokens=1) for r in payload["record_ids"]],
                reason=str(payload.get("reason", "")),
                session=payload.get("session"),
            )
            jobs.append(_Job(ns, window=window, members=frozenset(window.record_ids)))
        store.inflight_batch = jobs
        try:
            prepared = await self._prepare_batch(ns, jobs, strict=True, reason=plans[0].reason)
        finally:
            store.inflight_batch = []
        for item, ready in zip(batch[1:], prepared[1:], strict=True):
            self._prefetched[(ns, item.ref[0])] = ready  # type: ignore[index]
        first = prepared[0]
        store.inflight = first.job if first is not None else None
        return first

    def _outbox_batch_ready(self, ns: str, store: _Store) -> bool:
        """Background drain gate (outbox mode): False holds the queued windows for company."""
        assert self._batcher is not None
        items, sentinel = self._outbox_items(ns, None)
        if not items:
            return True
        now = self._clock()
        if self._batcher.plan(items, now, final=sentinel):
            return True
        self._hold(ns, store, items, now)
        return False

    def _take_batch(self, ns: str, store: _Store, final: bool) -> list[_Job] | None:
        """In-memory mode: the next jobs to run (one job, a session-closed sentinel, or a batch of
        windows), or None when the queued windows are held for company."""
        assert self._batcher is not None
        head = store.queue[0]
        if head.closed_session is not None:
            return [head]
        items: list[BatchItem] = []
        sentinel = False
        for job in store.queue:
            if job.closed_session is not None:
                sentinel = True
                break
            items.append(BatchItem((ns,), job, max(job.tokens, 1), job.queued_at))
        now = self._clock()
        plans = self._batcher.plan(items, now, final=final or sentinel)
        if plans:
            self._batch_reason = plans[0].reason
            return [i.ref for i in plans[0].items]  # type: ignore[misc]
        self._hold(ns, store, items, now)
        return None

    def _hold(self, ns: str, store: _Store, items: list[BatchItem], now: float) -> None:
        assert self._batcher is not None
        wait = self._batcher.next_wake(items, now) or 0.0
        self._counts["batch_held"] += 1
        tap.event(
            "write.batch.held",
            windows=len(items),
            tokens=sum(i.tokens for i in items),
            wait_s=round(wait, 3),
        )
        if not self._async or self._stopped:
            return
        if store.batch_timer is not None:
            store.batch_timer.cancel()
        loop = asyncio.get_running_loop()

        def wake() -> None:
            store.batch_timer = None
            if not self._stopped:
                self._ensure_worker(ns, store)

        store.batch_timer = loop.call_later(max(wait, 0.01), wake)

    async def _drain_batch(self, ns: str, store: _Store, jobs: list[_Job]) -> None:
        for job in jobs:
            store.queue.remove(job)
        store.inflight_batch = list(jobs)
        try:
            with tap.request("job", stage="window_extract"):
                prepared = await self._prepare_batch(
                    ns, jobs, strict=False, reason=self._batch_reason
                )
                for ready in prepared:
                    if ready is not None:
                        await self._commit_job(ns, store, ready)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one bad batch never stops the queue
            self._counts["jobs_failed"] += 1
            tap.event("write.window.job_failed", error=type(exc).__name__)
            _log.warning("window.job_failed", namespace=ns, error=str(exc), exc_info=True)
        finally:
            store.inflight_batch = []

    async def _prepare_batch(
        self, ns: str, jobs: list[_Job], *, strict: bool, reason: str = ""
    ) -> list[_Prepared | None]:
        """ONE extraction call for several windows of one namespace. The windows keep their own
        numbered blocks (line numbers run on across the blocks, so a cited line names one window),
        and each window's items are mapped back to its own turns and validated against its own
        lines. Episodes stay one call per window. Result aligned with ``jobs``."""
        out: list[_Prepared | None] = [None] * len(jobs)
        live: list[tuple[int, _Job, list[MemoryRecord]]] = []
        for i, job in enumerate(jobs):
            if job.cancelled or job.window is None:
                continue
            records = await self._live_turns(job.window)
            if not records:
                self._counts["windows_empty"] += 1
                tap.event(
                    "write.window.skipped", reason="no_live_turns", turns=len(job.window.turns)
                )
                continue
            live.append((i, job, records))
        if len(live) == 1:
            i, job, records = live[0]
            out[i] = await self._prepare_records(ns, job, records, strict=strict)
            return out
        if not live:
            return out
        lines: list[str] = []
        spans: list[tuple[int, int]] = []
        texts_by_window: list[list[str]] = []
        people: list[str] = []
        base = 0
        for w, (_i, _job, records) in enumerate(live):
            _, texts, who = build_transcript(records)
            people.extend(p for p in who if p not in people)
            lines.append(f"=== window {w + 1} of {len(live)} ===")
            lines.extend(
                f"[{base + n}] [{rec.valid_from:%Y-%m-%d}] {text}"
                for n, (rec, text) in enumerate(zip(records, texts, strict=True), 1)
            )
            spans.append((base, len(records)))
            texts_by_window.append(texts)
            base += len(records)
        transcript = "\n".join(lines)
        all_ids = [r.record_id for _, _, records in live for r in records]
        batch_id = fingerprint_payload({"ns": ns, "turns": all_ids})[:12]
        variables = {"content": transcript, "participants": ", ".join(people), "turn_count": base}
        self._counts["windows_processed"] += len(live)
        self._counts["batches"] += 1
        self._counts["batched_windows"] += len(live)
        tap.event(
            "write.batch.planned",
            id=batch_id,
            windows=len(live),
            turns=base,
            tokens=estimate_tokens(transcript),
            reason=reason,
            sections=list(self._batch_sections),
        )
        merged_all, failed = await self._run_groups(
            ns, [self._batch_sections], variables, transcript, all_ids, batch_id
        )
        if strict and failed and not merged_all:
            tap.event("write.batch.failed", id=batch_id, windows=len(live))
            raise WindowCallFailed("the batched window call failed")
        per_window: list[dict[str, Any]] = [{} for _ in live]
        for section, raw in merged_all.items():
            if raw is _MISSING:
                for merged in per_window:
                    merged[section] = _MISSING
                continue
            parts, cross = split_batch_section(section, raw, spans)
            for merged, part in zip(per_window, parts, strict=True):
                merged[section] = part
            if cross:
                self._counts["batch_cross_window"] += cross
                if section == "facts":  # an item that cites two windows is grounded in neither
                    self._counts["facts_returned"] += cross
                    self._counts["facts_ungrounded"] += cross
                tap.event("write.batch.cross_window", section=section, dropped=cross, id=batch_id)
        for w, (i, job, records) in enumerate(live):
            member_ids = [r.record_id for r in records]
            window_id = fingerprint_payload({"ns": ns, "turns": member_ids})[:12]
            window_failed = failed
            if "episode" in self.sections:
                ep_transcript, _, who = build_transcript(records)
                ep_vars = {
                    "content": ep_transcript,
                    "participants": ", ".join(who),
                    "turn_count": len(records),
                }
                got, ep_failed = await self._run_groups(
                    ns, [("episode",)], ep_vars, ep_transcript, member_ids, window_id
                )
                per_window[w].update(got)
                window_failed = window_failed or ep_failed
            tap.event(
                "write.batch.window",
                id=window_id,
                batch=batch_id,
                first_line=spans[w][0] + 1,
                turns=len(records),
            )
            if job.cancelled:
                self._counts["windows_cancelled_forget"] += 1
                await self._purge_window_cache(member_ids)
                continue
            out[i] = _Prepared(
                job,
                records,
                per_window[w],
                texts_by_window[w],
                window_id,
                2 if "episode" in self.sections else 1,
                window_failed,
            )
        return out

    async def _outbox_commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        """Outbox handler, phase 2: the deposits (each locks the namespace itself)."""
        if prepared is None:
            return
        ns = scope.owner
        store = self._store(ns)
        kind, body = prepared
        try:
            if kind == "closed":
                await self._session_closed(ns, store, body["session"], int(body.get("windows", 0)))
            elif kind == "deferred":
                assert self._outbox is not None
                await self._outbox.park(scope, WINDOW_STAGE, body)
            elif body is not None:
                await self._commit_job(ns, store, body)
        finally:
            store.inflight = None

    async def resume_deferred(self, namespace: str) -> int:
        """Derive the windows a spent budget parked (W2-12). Does nothing while the namespace's
        budget is still spent. Returns the number of windows put back and drained."""
        if self._outbox is None:
            return 0
        ledger = _costs.installed()
        if ledger is not None and ledger.budget is not None and not ledger.budget.resume(namespace):
            return 0
        scope = self._scope(namespace)
        n = await self._outbox.resume_parked(scope, WINDOW_STAGE)
        if n:
            await self._outbox.drain(scope)
        return n

    def deferred_count(self, namespace: str) -> int:
        if self._outbox is None:
            return 0
        return int(self._outbox.parked(self._scope(namespace), WINDOW_STAGE))

    # -- forget -----------------------------------------------------------------------------

    async def on_forget(self, namespace: str, record_id: str) -> None:
        """A turn was forgotten (any forget, hard or soft): it leaves the open window and every
        queued window (a window left with no turn is dropped), so no call ever sees its text; a
        window whose call is running is cancelled (its reply may restate the turn); pending
        call-1 edges that cite or cover it are dropped; cached replies for windows that held it
        are deleted."""
        if self._outbox is not None:
            await self._persist_staged()  # an item still staged is as queued as one stored
            await self._outbox.rewrite_pending(
                self._scope(namespace), WINDOW_STAGE, lambda p: self._forget_in_item(p, record_id)
            )
        store = self._stores.get(namespace)
        if store is not None:
            if store.segmenter.discard(record_id):
                self._counts["turns_dropped_forget"] += 1
            for job in list(store.queue):
                if job.window is None or record_id not in job.members:
                    continue
                job.window.turns = [t for t in job.window.turns if t.record_id != record_id]
                job.members = frozenset(job.window.record_ids)
                self._counts["turns_dropped_forget"] += 1
                if not job.window.turns:
                    store.queue.remove(job)
                    self._counts["windows_cancelled_forget"] += 1
            if store.inflight is not None and record_id in store.inflight.members:
                store.inflight.cancelled = True
            for job in store.inflight_batch:
                if record_id in job.members:
                    job.cancelled = True
            for batch in list(store.pending):
                batch.edges = [(ids, e) for ids, e in batch.edges if record_id not in ids]
                if record_id in batch.covered:
                    batch.covered.remove(record_id)
                if not batch.covered and not batch.edges:
                    store.pending.remove(batch)
        for ready in self._prefetched.values():  # W2-05: a batched call's results, not committed
            if ready is not None and record_id in ready.job.members:
                ready.job.cancelled = True
        keys = self._keys_by_record.pop(record_id, set())
        if self._cache is not None:
            for key in keys:
                try:
                    await self._cache.delete(key)
                except Exception as exc:  # best effort; the entry is only a cached reply
                    _log.warning("window.cache_purge_failed", error=str(exc))

    def take_edges(self, namespace: str) -> list[WindowEdgeBatch]:
        """Hand the pending call-1 edge batches of ``namespace`` to the sleep stage."""
        store = self._stores.get(namespace)
        if store is None or not store.pending:
            return []
        out, store.pending = store.pending, []
        return out

    # -- one window -------------------------------------------------------------------------

    def _groups(self) -> list[tuple[str, ...]]:
        secs = self.sections
        first = tuple(s for s in secs if s != "episode")
        second = tuple(s for s in secs if s == "episode")
        if len(secs) < 2 or not first or not second:
            return [secs]
        if self._split == "always" or (self._split == "auto" and self._auto_split):
            return [first, second]
        return [secs]

    def _note_outcome(self, failed: bool, group_count: int) -> None:
        """Auto split: remember whether a joint call failed; split once failures pile up."""
        if group_count > 1 or self._split != "auto" or self._auto_split:
            return
        self._recent.append(failed)
        if (
            len(self._recent) >= constants.WINDOW_SPLIT_RECENT
            and sum(self._recent) / len(self._recent) >= constants.WINDOW_SPLIT_FAIL_RATE
        ):
            self._auto_split = True
            self._counts["split_activated"] += 1
            _log.warning("window.split_activated", failures=sum(self._recent))

    async def _call(
        self,
        group: tuple[str, ...],
        variables: Mapping[str, object],
        transcript: str,
        member_ids: Sequence[str],
    ) -> Mapping[str, Any]:
        key = ""
        if self._cache is not None:
            key = "window:" + fingerprint_payload(
                {
                    "id": self._extract.identity,
                    "sections": list(group),
                    "content": transcript,
                    "participants": variables.get("participants"),
                }
            )
            try:
                hit = await self._cache.get(key)
            except Exception:
                hit = None
            if hit is not None:
                for rid in member_ids:
                    self._keys_by_record.setdefault(rid, set()).add(key)
                self._counts["cache_hits"] += 1
                tap.event("write.extract.call", sections=list(group), cached=True, outcome="ok")
                cached: Mapping[str, Any] = json.loads(hit)
                return cached
        self._counts["calls"] += 1
        if len(group) < len(self.sections):
            self._counts["split_calls"] += 1
        for section in group:
            self._calls_by_layer[section] += 1
        with tap.span("write.extract.call", sections=list(group), cached=False):
            raw = await self._extract(group, variables)
        if self._cache is not None and key:
            try:
                await self._cache.set(key, json.dumps(dict(raw), default=str).encode())
                for rid in member_ids:
                    self._keys_by_record.setdefault(rid, set()).add(key)
            except Exception as exc:
                _log.warning("window.cache_write_failed", error=str(exc))
        return raw

    async def _live_turns(self, window: Window) -> list[MemoryRecord]:
        live: list[MemoryRecord] = []
        for turn in window.turns:
            record = await self._get(turn.record_id)
            if (
                record is None
                or record.status is not RecordStatus.ACTIVATED
                or record.quarantined
                or record.instruction_flag
            ):
                self._counts["turns_excluded"] += 1
                continue
            if self._role_policy:  # W2-03: system turns and echoes are raw only
                why = role_filter.block_reason(record.tags)
                if why is not None:
                    self._counts[f"turns_excluded_{why}"] += 1
                    tap.event("write.role.derive_skip", reason=why, id=record.record_id)
                    continue
            live.append(record)
        return live

    async def _run_job(self, ns: str, store: _Store, job: _Job) -> None:
        prepared = await self._prepare_job(ns, job)
        if prepared is not None:
            await self._commit_job(ns, store, prepared)

    async def _prepare_job(self, ns: str, job: _Job, *, strict: bool = False) -> _Prepared | None:
        """The lock-free half of one window: live turns, then the model calls. ``strict`` (outbox
        mode) raises :class:`WindowCallFailed` when every call failed, so the offset is a hole."""
        window = job.window
        assert window is not None
        if job.cancelled:
            return None
        records = await self._live_turns(window)
        if not records:
            self._counts["windows_empty"] += 1
            tap.event("write.window.skipped", reason="no_live_turns", turns=len(window.turns))
            return None
        return await self._prepare_records(ns, job, records, strict=strict)

    async def _prepare_records(
        self, ns: str, job: _Job, records: list[MemoryRecord], *, strict: bool
    ) -> _Prepared | None:
        """The model calls of one window whose live turns are known (``_prepare_job`` and the
        single-window case of a batch)."""
        window = job.window
        assert window is not None
        member_ids = [r.record_id for r in records]
        window_id = fingerprint_payload({"ns": ns, "turns": member_ids})[:12]
        transcript, line_texts, people = build_transcript(records)
        variables = {
            "content": transcript,
            "participants": ", ".join(people),
            "turn_count": len(records),
        }
        self._counts["windows_processed"] += 1
        tap.event(
            "write.window.planned",
            id=window_id,
            reason=window.reason,
            turns=len(records),
            excluded=len(window.turns) - len(records),
            tokens=window.tokens,
            groups=len(self._groups()),
        )
        groups = self._groups()
        merged, failed = await self._run_groups(
            ns, groups, variables, transcript, member_ids, window_id
        )
        if job.cancelled:
            self._counts["windows_cancelled_forget"] += 1
            await self._purge_window_cache(member_ids)
            return None
        if strict and failed and not merged:
            self._note_outcome(True, len(groups))
            raise WindowCallFailed(f"all {len(groups)} window call(s) failed")
        return _Prepared(job, records, merged, line_texts, window_id, len(groups), failed)

    async def _run_groups(
        self,
        ns: str,
        groups: Sequence[tuple[str, ...]],
        variables: Mapping[str, object],
        transcript: str,
        member_ids: Sequence[str],
        window_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Run the model call of each section group; a failed group is counted and dropped alone."""
        merged: dict[str, Any] = {}
        failed = False
        for group in groups:
            try:
                with _cost_scope(ns, "+".join(group)):
                    raw = await self._call(group, variables, transcript, member_ids)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                kind = "parse_failures" if "failed validation" in str(exc) else "call_errors"
                self._counts[kind] += 1
                for section in group:
                    self._dropped_by_layer[section] += 1
                    self._counts["sections_dropped"] += 1
                _log.warning("window.call_failed", namespace=ns, kind=kind, error=str(exc)[:200])
                tap.event(
                    "write.extract.rejected" if kind == "parse_failures" else "write.extract.failed",
                    reason=kind,
                    sections=list(group),
                    error=type(exc).__name__,
                    id=window_id,
                )
                continue
            for section in group:
                merged[section] = raw.get(section, _MISSING)
        return merged, failed

    async def _commit_job(self, ns: str, store: _Store, prepared: _Prepared) -> None:
        """The deposit half: each deposit takes the namespace lock itself (``_write_locked``)."""
        broken = await self._deposit_sections(
            ns,
            store,
            prepared.job,
            prepared.records,
            prepared.merged,
            prepared.line_texts,
            prepared.window_id,
        )
        self._note_outcome(prepared.failed or broken, prepared.groups)

    async def _purge_window_cache(self, member_ids: Sequence[str]) -> None:
        if self._cache is None:
            return
        for rid in member_ids:
            for key in self._keys_by_record.pop(rid, set()):
                with contextlib.suppress(Exception):
                    await self._cache.delete(key)

    def _drop(self, section: str) -> None:
        tap.event("write.extract.rejected", reason="section_malformed", section=section)
        self._dropped_by_layer[section] += 1
        self._counts["sections_dropped"] += 1

    async def _deposit_sections(
        self,
        ns: str,
        store: _Store,
        job: _Job,
        records: list[MemoryRecord],
        merged: Mapping[str, Any],
        line_texts: list[str],
        window_id: str,
    ) -> bool:
        """Validate and deposit each section on its own. True when a section was malformed."""
        broken = False
        member_ids = [r.record_id for r in records]
        count = len(records)
        for section in self.sections:
            if section not in merged:
                continue  # its call failed; already counted
            raw = merged[section]
            if raw is _MISSING:
                self._drop(section)
                broken = True
                continue
            if job.cancelled:
                break
            if section == "facts":
                outcome = validate_facts(raw, line_texts)
                if outcome is None:
                    self._drop(section)
                    broken = True
                    continue
                self._counts["facts_returned"] += outcome.returned
                self._counts["facts_invalid"] += outcome.invalid
                self._counts["facts_ungrounded"] += outcome.ungrounded
                self._counts["facts_uncited"] += outcome.uncited
                tap.event(
                    "write.extract.facts",
                    returned=outcome.returned,
                    accepted=len(outcome.facts),
                    invalid=outcome.invalid,
                    ungrounded=outcome.ungrounded,
                    uncited=outcome.uncited,
                )
                await self._store_facts(ns, job, records, outcome, window_id, member_ids)
            elif section == "edges":
                edge_out = validate_edges(raw, count)
                if edge_out is None:
                    self._drop(section)
                    broken = True
                    continue
                self._counts["edges_returned"] += edge_out.returned
                self._counts["edges_invalid"] += edge_out.invalid
                self._counts["edges_ungrounded"] += edge_out.ungrounded
                tap.event(
                    "write.extract.edges",
                    returned=edge_out.returned,
                    accepted=len(edge_out.edges),
                    invalid=edge_out.invalid,
                    ungrounded=edge_out.ungrounded,
                )
                batch = WindowEdgeBatch(
                    window_id=window_id,
                    covered=list(member_ids),
                    edges=[
                        ([records[n - 1].record_id for n in lines], edge)
                        for edge, lines in edge_out.edges
                    ],
                )
                store.pending.append(batch)
                self._counts["edges_pending"] += len(batch.edges)
                while len(store.pending) > constants.WINDOW_PENDING_EDGES_MAX:
                    store.pending.pop(0)
                    self._counts["edge_batches_dropped"] += 1
            elif section == "episode":
                ep_out = validate_episode(raw, count)
                if ep_out is None:
                    self._drop(section)
                    broken = True
                    continue
                self._counts["episodes_invalid"] += ep_out.invalid
                self._counts["episodes_uncited"] += ep_out.uncited
                self._counts["episodes_truncated"] += ep_out.truncated
                tap.event(
                    "write.extract.episode",
                    invalid=ep_out.invalid,
                    uncited=ep_out.uncited,
                    truncated=ep_out.truncated,
                    present=ep_out.episode is not None,
                )
                if ep_out.episode is not None and self._deposit_episode is not None:
                    await self._store_episode(ns, job, records, ep_out, window_id, member_ids)
        if job.cancelled:
            self._counts["windows_cancelled_forget"] += 1
            await self._purge_window_cache(member_ids)
        return broken

    async def _store_facts(
        self,
        ns: str,
        job: _Job,
        records: list[MemoryRecord],
        outcome: FactsOutcome,
        window_id: str,
        member_ids: list[str],
    ) -> None:
        if self._deposit_fact is None:
            return
        start, latest = records[0].valid_from, records[-1].valid_from
        for fact, lines in outcome.facts:
            if job.cancelled:
                return
            parents = [records[n - 1].record_id for n in lines]
            speakers = {
                t
                for n in lines
                for t in records[n - 1].tags
                if t.startswith("spk:") and t != "spk:"
            }
            tags = ["window_fact", f"window:{window_id}"]
            if self._role_policy:  # W2-03: a fact no user line backs is marked
                basis = role_filter.basis_of(records[n - 1].tags for n in lines)
                if basis in ("evidence", "proposal"):
                    tags.append(f"{constants.ROLE_USE_TAG_PREFIX}{basis}")
                    tap.event("write.role.fact_basis", basis=basis, parents=len(lines))
            if len(speakers) == 1:
                tags.append(next(iter(speakers)))
            when = _fact_date(fact.date, latest) or start
            try:
                stored = await self._deposit_fact(
                    ns,
                    f"{fact.entity} {fact.attribute}: {fact.value}",
                    fact.entity or None,
                    fact.attribute or None,
                    parents,
                    when,
                    f"win:{window_id}",
                    kind=fact.kind,
                    persons=list(fact.persons),
                    location=fact.location,
                    topic=fact.topic,
                    extra_tags=tags,
                    require_live=member_ids,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad fact must not lose the rest
                self._counts["facts_deposit_errors"] += 1
                tap.event(
                    "write.extract.rejected", reason="fact_deposit_error", error=type(exc).__name__
                )
                _log.warning("window.fact_deposit_failed", error=str(exc)[:200])
                continue
            if stored is None:
                self._counts["facts_skipped_parent_gone"] += 1
                tap.event("write.extract.rejected", reason="parent_gone", layer="facts")
                job.cancelled = True  # a member vanished: nothing else of this window lands
                return
            self._counts["facts_deposited"] += 1
            tap.event(
                "write.extract.deposit",
                id=getattr(stored, "record_id", None),
                layer="facts",
                parents=len(parents),
            )

    async def _store_episode(
        self,
        ns: str,
        job: _Job,
        records: list[MemoryRecord],
        outcome: EpisodeOutcome,
        window_id: str,
        member_ids: list[str],
    ) -> None:
        assert outcome.episode is not None and self._deposit_episode is not None
        episode = outcome.episode
        text = f"{episode.title}: {episode.narrative}" if episode.title else episode.narrative
        parents = [records[n - 1].record_id for n in outcome.lines]
        try:
            stored = await self._deposit_episode(
                ns, text, parents, records[0].valid_from, window_id, require_live=member_ids
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._counts["episodes_deposit_errors"] += 1
            tap.event(
                "write.extract.rejected", reason="episode_deposit_error", error=type(exc).__name__
            )
            _log.warning("window.episode_deposit_failed", error=str(exc)[:200])
            return
        if stored is None:
            self._counts["episodes_skipped_parent_gone"] += 1
            tap.event("write.extract.rejected", reason="parent_gone", layer="episode")
            job.cancelled = True
            return
        self._counts["episodes_deposited"] += 1
        tap.event(
            "write.extract.deposit", id=getattr(stored, "record_id", None), layer="episode"
        )

    # -- counters (G-W13) ----------------------------------------------------------------------

    def stats(self, *, reset: bool = False) -> dict[str, Any]:
        """The write-quality counters: windows, boundaries, calls per layer, cache hits, parse
        failures, sections dropped, items dropped as ungrounded, deposits, cancellations."""
        c = self._counts
        sessions = sum(len(s.session_windows) for s in self._stores.values()) + c["sessions_closed"]
        out: dict[str, Any] = {
            "mode": "+".join(self.sections),
            "turns": c["turns"],
            "windows": c["windows"],
            "windows_by_reason": dict(self._reasons),
            "windows_processed": c["windows_processed"],
            "windows_per_session": round(c["windows"] / sessions, 3) if sessions else None,
            "turns_per_window": round(c["turns"] / c["windows"], 3) if c["windows"] else None,
            "calls": c["calls"],
            "calls_by_layer": dict(self._calls_by_layer),
            "calls_per_turn": round(c["calls"] / c["turns"], 4) if c["turns"] else None,
            "cache_hits": c["cache_hits"],
            "split_active": self._auto_split or self._split == "always",
            "split_calls": c["split_calls"],
            "parse_failures": c["parse_failures"],
            "call_errors": c["call_errors"],
            "sections_dropped": c["sections_dropped"],
            "sections_dropped_by_layer": dict(self._dropped_by_layer),
            "facts_returned": c["facts_returned"],
            "facts_invalid": c["facts_invalid"],
            "facts_ungrounded": c["facts_ungrounded"],
            "facts_uncited": c["facts_uncited"],
            "facts_deposited": c["facts_deposited"],
            "facts_skipped_parent_gone": c["facts_skipped_parent_gone"],
            "facts_deposit_errors": c["facts_deposit_errors"],
            "edges_returned": c["edges_returned"],
            "edges_invalid": c["edges_invalid"],
            "edges_ungrounded": c["edges_ungrounded"],
            "edges_pending": sum(len(b.edges) for s in self._stores.values() for b in s.pending),
            "episodes_deposited": c["episodes_deposited"],
            "episodes_invalid": c["episodes_invalid"],
            "episodes_uncited": c["episodes_uncited"],
            "episodes_truncated": c["episodes_truncated"],
            "windows_cancelled_forget": c["windows_cancelled_forget"],
            "turns_dropped_forget": c["turns_dropped_forget"],
            "windows_empty": c["windows_empty"],
            "turns_excluded": c["turns_excluded"],
            "jobs_failed": c["jobs_failed"],
            "jobs_abandoned": c["jobs_abandoned"],
            "sessions_closed": c["sessions_closed"],
            "queued": sum(len(s.queue) for s in self._stores.values()),
        }
        if self._seg is not None:  # W2-05: only present with write.segmentation_v2
            out["segmentation_v2"] = True
            out["batches"] = c["batches"]
            out["batched_windows"] = c["batched_windows"]
            out["batch_held"] = c["batch_held"]
            out["batch_cross_window"] = c["batch_cross_window"]
            out["windows_per_call"] = (
                round(c["windows_processed"] / c["calls"], 3) if c["calls"] else None
            )
        if c["windows_deferred_backpressure"]:  # RT-02: only present once backpressure bit
            out["deferred_backpressure"] = c["windows_deferred_backpressure"]
        if c["windows_deferred_budget"]:  # W2-12: only present once a budget paused work
            out["deferred_budget"] = c["windows_deferred_budget"]
        if self._outbox is not None:
            out["queued"] += self._outbox.depth()
            out["outbox"] = True
            out["jobs_persisted"] = c["jobs_persisted"]
        topic = {
            ns: {"pairs": s.segmenter.pairs, "level": s.segmenter.topic_level}
            for ns, s in self._stores.items()
        }
        out["topic_level"] = topic
        if reset:
            self._counts.clear()
            self._reasons.clear()
            self._calls_by_layer.clear()
            self._dropped_by_layer.clear()
        return out


_MISSING: Any = object()
