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
from memspine.derive.pipeline.stages.context import SessionClosed, WindowEdgeBatch
from memspine.derive.pipeline.stages.mining import _fact_date
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
from memspine.runtime.runner import InlineRunner, StageHandler
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


# --- the manager ----------------------------------------------------------------------------


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


@dataclass
class _Store:
    segmenter: WindowSegmenter
    queue: deque[_Job] = field(default_factory=deque)
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker: asyncio.Task[None] | None = None
    idle: asyncio.TimerHandle | None = None
    touch: int = 0
    session: str | None = None
    pending: list[WindowEdgeBatch] = field(default_factory=list)
    inflight: _Job | None = None
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
        outbox: InlineRunner | None = None,
    ) -> None:
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
        self._persist_lock = asyncio.Lock()
        self._recovered = outbox is None
        if outbox is not None:
            outbox.register(
                WINDOW_STAGE,
                StageHandler(prepare=self._outbox_prepare, commit=self._outbox_commit),
            )
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
                    },
                )
            )
        else:
            job = _Job(ns, window=window, members=frozenset(window.record_ids))
            store.queue.append(job)
        self._counts["windows"] += 1
        self._reasons[window.reason] += 1
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

    async def _persist_staged(self) -> None:
        """Outbox mode: write the items ``_enqueue`` staged (in order), then wake the workers.
        Staging keeps the segmenter code synchronous and in its old order; the database write
        happens here, off the event loop (``InlineRunner.enqueue``)."""
        if self._outbox is None or not self._staged:
            return
        async with self._persist_lock:
            staged, self._staged = self._staged, []
            for ns, payload in staged:
                await self._outbox.enqueue(self._scope(ns), WINDOW_STAGE, payload)
        if self._async and not self._stopped:
            for ns in dict.fromkeys(n for n, _ in staged):
                self._ensure_worker(ns, self._store(ns))

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
        store.worker = asyncio.get_running_loop().create_task(self._drain(ns, store))

    async def _drain(self, ns: str, store: _Store) -> None:
        if self._outbox is not None:
            await self._outbox.drain(self._scope(ns))
            return
        async with store.run_lock:
            while store.queue and not self._stopped:
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
                await self._persist_staged()
            if store.worker is not None and not store.worker.done():
                await asyncio.shield(store.worker)
            await self._drain(ns, store)
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
        await self._persist_staged()
        if store.worker is not None and not store.worker.done():
            await asyncio.shield(store.worker)
        await self._drain(namespace, store)
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
        for store in self._stores.values():
            if store.idle is not None:
                store.idle.cancel()
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
        window = Window(
            turns=[WindowTurn(record_id=r, text="", tokens=1) for r in payload["record_ids"]],
            reason=str(payload.get("reason", "")),
            session=payload.get("session"),
        )
        job = _Job(ns, window=window, members=frozenset(window.record_ids))
        store.inflight = job  # a forget during the call cancels it
        try:
            prepared = await self._prepare_job(ns, job, strict=True)
        except BaseException:
            store.inflight = None
            raise
        return ("window", prepared)

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
            elif body is not None:
                await self._commit_job(ns, store, body)
        finally:
            store.inflight = None

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
            for batch in list(store.pending):
                batch.edges = [(ids, e) for ids, e in batch.edges if record_id not in ids]
                if record_id in batch.covered:
                    batch.covered.remove(record_id)
                if not batch.covered and not batch.edges:
                    store.pending.remove(batch)
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
        merged: dict[str, Any] = {}
        groups = self._groups()
        failed = False
        for group in groups:
            try:
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
        if job.cancelled:
            self._counts["windows_cancelled_forget"] += 1
            await self._purge_window_cache(member_ids)
            return None
        if strict and failed and not merged:
            self._note_outcome(True, len(groups))
            raise WindowCallFailed(f"all {len(groups)} window call(s) failed")
        return _Prepared(job, records, merged, line_texts, window_id, len(groups), failed)

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
