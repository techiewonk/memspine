"""SystemAdapter for memspine itself.

Deliberately thin: it uses the public facade only (``write_messages`` in,
``assemble`` out), because an adapter that reaches into internals would
evaluate a configuration no user can reproduce. Each item gets a fresh
in-memory engine, so nothing leaks between conversations.

Imports are lazy. The harness core must stay runnable in an environment where
memspine's own dependencies are not installed — otherwise the baselines, which
need none of them, could not be run either.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from ..contracts import DepositResult, Evidence, RetrievedContext, Turn
from ..tokens import HeuristicTokenCounter, TokenCounter

_DATE_FORMATS = (
    "%I:%M %p on %d %B, %Y",  # LoCoMo: "1:56 pm on 8 May, 2023"
    "%I:%M %p on %d %b, %Y",
    "%Y/%m/%d (%a) %H:%M",  # LongMemEval haystack dates: "2023/05/20 (Sat) 02:21"
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)
_USAGE_KEYS = ("calls", "prompt", "completion")
#: #33: the counters of one ``Engine.usage()`` entry (per named prompt).
_PROMPT_USAGE_KEYS = ("calls", "input_tokens", "output_tokens", "estimated_calls")


def engine_version() -> str:
    """memspine's version without starting an engine (R3-11): the manifest is built
    before the first item resets the system, so ``describe()`` cannot wait for it."""
    try:
        import memspine

        version = getattr(memspine, "__version__", None)
        if version:
            return str(version)
    except ImportError:
        pass
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as dist_version

        return dist_version("memspine")
    except (ImportError, PackageNotFoundError):
        return "not-installed"


_NAIVE_WARNED: set[str] = set()


def stamp_zone(name: str) -> tzinfo:
    """F5: an IANA zone name -> tzinfo (``UTC`` needs no tz database)."""
    if name.upper() == "UTC":
        return UTC
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _note_naive(zone: tzinfo) -> None:
    """F5: benchmark stamps ("1:56 pm on 8 May, 2023") carry no offset. They are read in
    ``zone`` (default UTC, so a stamp is stored as written); say so once per zone."""
    key = str(zone)
    if key not in _NAIVE_WARNED:
        _NAIVE_WARNED.add(key)
        logging.getLogger(__name__).warning(
            "dataset timestamps are naive wall-clock times; reading them in %s (no conversion). "
            "Pass --stamp-timezone <IANA name> if the dataset states its zone.",
            key,
        )


def parse_turn_time(stamp: str | None, zone: tzinfo = UTC) -> datetime | None:
    """Benchmark session stamps -> aware datetime (None if absent or unknown format).
    A stamp without an offset is read in ``zone`` (F5; default UTC, with a one-time warning)."""
    if not stamp:
        return None
    text = re.sub(r"\s+", " ", stamp.strip())
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        _note_naive(zone)
        return parsed.replace(tzinfo=zone)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo:
        return parsed
    _note_naive(zone)
    return parsed.replace(tzinfo=zone)


def parse_turn_stamp(turn: Turn, zone: tzinfo = UTC) -> datetime | None:
    """F3 [INJ-3]: the turn's event time. A missing stamp is None; a non-empty stamp that
    :func:`parse_turn_time` cannot read is an error, not a silent fall back to "now"."""
    stamp = turn.timestamp
    parsed = parse_turn_time(stamp, zone)
    if parsed is None and stamp and str(stamp).strip():
        raise ValueError(
            f"turn {turn.turn_id!r} (session {turn.session_id!r}) has an unparseable timestamp "
            f"{stamp!r}; add its format to _DATE_FORMATS (it would be stored as 'now')"
        )
    return parsed


#: D7 [INJ-6]: the forensic logs ``MEMSPINE_FORENSICS_DIR`` collects.
FORENSIC_LOGS = ("forensics.jsonl", "ingest.jsonl")


def prepare_forensics_dir(directory: str, environ: Mapping[str, str] | None = None) -> None:
    """D7 [INJ-6]: refuse to append to a forensics directory that already holds logs, unless
    ``MEMSPINE_FORENSICS_OVERWRITE=1`` (then the old logs are removed)."""
    import os

    env = os.environ if environ is None else environ
    existing = [name for name in FORENSIC_LOGS if (Path(directory) / name).exists()]
    if not existing:
        return
    if env.get("MEMSPINE_FORENSICS_OVERWRITE") == "1":
        for name in existing:
            (Path(directory) / name).unlink()
        return
    raise RuntimeError(
        f"MEMSPINE_FORENSICS_DIR={directory!r} already contains {', '.join(existing)}; a second "
        "run would append to them and mix two runs. Use a fresh directory, or set "
        "MEMSPINE_FORENSICS_OVERWRITE=1 to delete the old logs."
    )


#: ``storage.path`` sentinel: one fresh file-backed store per item (see ``_build_engine``).
TEMPDIR_STORAGE = "tempdir"


#: C1: how context lines that are final search hits are marked for the reader.
MARK_HITS_MODES = ("off", "star", "rank")


def hit_rank_map(stages: Mapping[str, Any]) -> dict[str, int]:
    """record_id -> 1-based rank of each final search hit (``stages["final"]``)."""
    return {str(rid): i + 1 for i, (rid, _score) in enumerate(stages.get("final", []))}


def mark_hit_line(line: str, rank: int | None, mode: str) -> str:
    """Prefix a rendered context line when it is search hit ``rank`` (None = neighbour)."""
    if rank is None or mode == "off":
        return line
    if mode == "star":
        return f"* {line}"
    if mode == "rank":
        return f"[hit {rank}] {line}"
    raise ValueError(f"unknown mark_hits mode {mode!r}")


#: R2-4: how the rendered context lines are ordered.
CONTEXT_ORDERS = ("chrono", "hits_first", "hit_blocks")
#: Separator line between the ranked hits and the rest (``hits_first``).
OTHER_LINES_HEADER = "Other related conversation:"


def order_context(
    record_ids: Sequence[str], ranks: Mapping[str, int], order: str
) -> list[int | str]:
    """Order the context lines; entries are indices into ``record_ids`` or a literal line.

    ``chrono`` (and any order without hit ranks) keeps the engine's order. ``hits_first``:
    the hits in rank order, a header line, then the other lines in engine order.
    ``hit_blocks``: per hit in rank order its block (the hit plus the non-hit lines nearest
    to it in the context, ties to the higher rank, engine order inside the block), blocks
    separated by a blank line; every line appears once. Non-hit lines in a context with no
    hit stay in engine order.
    """
    if order not in CONTEXT_ORDERS:
        raise ValueError(f"unknown context_order {order!r}")
    n = len(record_ids)
    hits = sorted(
        (i for i in range(n) if record_ids[i] in ranks), key=lambda i: ranks[record_ids[i]]
    )
    if order == "chrono" or not hits:
        return list(range(n))
    rest = [i for i in range(n) if record_ids[i] not in ranks]
    if order == "hits_first":
        return [*hits, *([OTHER_LINES_HEADER, *rest] if rest else [])]
    blocks: dict[int, list[int]] = {h: [h] for h in hits}
    for i in rest:
        # nearest hit by position; hits is rank-ordered so min() breaks ties by rank
        owner = min(hits, key=lambda h: abs(h - i))
        blocks[owner].append(i)
    out: list[int | str] = []
    for h in hits:
        if out:
            out.append("")
        out.extend(sorted(blocks[h]))
    return out


class MemspineSystem:
    """Drives ``memspine.Engine`` through the two-verb contract."""

    def __init__(
        self,
        template: str = "base",
        namespace: str = "eval",
        config: Mapping[str, Any] | None = None,
        counter: TokenCounter | None = None,
        system_id: str = "memspine",
        record_deposit_calls: bool = True,
        dated: bool = True,
        read_mode: str | None = None,
        build_sleep: bool = False,
        sleep_calls_per_session: int = 1,
        batch_turns: int = 1,
        as_of_question_date: bool = False,
        mark_hits: str = "off",
        context_order: str = "chrono",
        perspective_metadata: bool | None = None,
        stamp_timezone: str = "UTC",
    ) -> None:
        self.system_id = system_id
        #: F5: the zone the dataset's naive wall-clock stamps are read in (IANA name).
        self._zone = stamp_zone(stamp_timezone)
        if context_order not in CONTEXT_ORDERS:
            raise ValueError(
                f"context_order must be one of {CONTEXT_ORDERS}, got {context_order!r}"
            )
        #: R2-4: line order of the rendered context (chrono | hits_first | hit_blocks).
        self._context_order = context_order
        if mark_hits not in MARK_HITS_MODES:
            raise ValueError(f"mark_hits must be one of {MARK_HITS_MODES}, got {mark_hits!r}")
        #: C1: mark the lines that are final search hits (``star`` | ``rank``); the
        #: neighbour-window lines stay unmarked. ``off`` renders as before.
        self._mark_hits = mark_hits
        #: N34: read as of each question's date (LongMemEval ``question_date``), so a
        #: turn recorded after the question was asked is never retrieved.
        self._as_of_question_date = as_of_question_date
        self._question_as_of: Any = None
        # ``template`` names a config template (base, personal, coding, ...);
        # the *profile* is a field those templates set. Conflating them would
        # silently evaluate the default engine while claiming another.
        self.template = template
        self.namespace = namespace
        self.config = dict(config or {})
        #: I5 / I39: send each turn's speaker (a LoCoMo participant name) and chat role
        #: (``user`` / ``assistant``) to ``write_messages`` as message metadata, and the
        #: asker (``persona`` / ``asker`` in a question's meta) to the read. The stored
        #: text is the same either way. None = on exactly when the config enables the
        #: perspective layer (``memories.episodic.policies.perspective``), so default runs
        #: write what they always wrote.
        policies = ((self.config.get("memories") or {}).get("episodic") or {}).get("policies") or {}
        self._perspective_metadata = (
            bool(policies.get("perspective"))
            if perspective_metadata is None
            else bool(perspective_metadata)
        )
        #: I25: left to the config (and so to a data profile's presets) unless set explicitly.
        self._perspective_auto = perspective_metadata is None
        self._shape: dict[str, Any] = {}
        self._asker: str | None = None
        self._read_session: str | None = None
        self._counter = counter or HeuristicTokenCounter()
        self._record_deposit_calls = record_deposit_calls
        self._dated = dated
        #: None = ``assemble`` (the default arm); else an ``Engine.read`` mode
        #: (``replay`` / ``auto`` / ``full``, C7'). Replay orders context
        #: chronologically, so R@k is not comparable with ranked arms.
        self._read_mode = read_mode
        #: run ``Engine.sleep()`` once the history is in (``build``), so write-time
        #: stages (mine_facts, anticipate, reflect_profile, ...) take part in a run.
        self._build_sleep = build_sleep
        #: R3-10: the declared lower bound on sleep-cycle model calls per session. The
        #: runner sets ``remaining_model_calls`` before ``build``; a sleep whose estimate
        #: exceeds it is refused up front instead of being charged after it ran.
        self.sleep_calls_per_session = sleep_calls_per_session
        self.remaining_model_calls: int | None = None
        self._sessions: set[str] = set()
        self._engine: Any = None
        self._version = engine_version()
        # record_id -> turn_id, so retrieved records map back to gold units.
        self._origin: dict[str, str] = {}
        #: G9: up to this many turns of one session go into one ``write_messages``
        #: call (one batched embedding). 1 = one call per turn, the original path.
        self.batch_turns = max(1, int(batch_turns))
        self._buffer: list[Turn] = []
        #: D7: ids the runner hands over; written into every forensic log row
        self._run_id: str | None = None
        self._query_id: str | None = None
        self._forensics_ready = False

    def describe(self) -> Mapping[str, Any]:
        return {
            "system_id": self.system_id,
            "version": self._version,
            "kind": "engine",
            "template": self.template,
            "namespace": self.namespace,
            "config": self.config,
            "record_access": bool((self.config.get("read") or {}).get("record_access", False)),
            "dated_rendering": self._dated,
            "read_mode": self._read_mode or "assemble",
            "build_sleep": self._build_sleep,
            "sleep_calls_per_session": self.sleep_calls_per_session,
            # Listed only when on, so default runs keep their config hash.
            **({"batch_turns": self.batch_turns} if self.batch_turns > 1 else {}),
            **({"as_of_question_date": True} if self._as_of_question_date else {}),
            **(
                {"data_shape": self._shape}
                if self._shape and self.config.get("data_profile")
                else {}
            ),
            **({"mark_hits": self._mark_hits} if self._mark_hits != "off" else {}),
            **({"context_order": self._context_order} if self._context_order != "chrono" else {}),
            "token_counter": dict(self._counter.describe()),
        }

    def declare_shape(self, shape: Any) -> None:
        """I25: the data shape of the item about to be loaded (``DataShape`` or mapping).

        Used only when the config sets ``data_profile``; it becomes the engine's
        ``data_shape``, from which ``data_profile: auto`` picks presets. A ``data_shape``
        already in the config is the deployer's and is never replaced."""
        self._shape = shape.to_dict() if hasattr(shape, "to_dict") else dict(shape or {})

    async def _build_engine(self) -> Any:
        try:
            import memspine
            from memspine.engine import Engine
        except ImportError as exc:  # pragma: no cover - needs the engine installed
            raise RuntimeError(
                "MemspineSystem needs the memspine package — run `uv sync --all-extras` "
                "in the repo root, or evaluate the baselines only"
            ) from exc
        self._version = getattr(memspine, "__version__", "unknown")
        overrides: dict[str, Any] = {"storage": {"path": ":memory:"}, **self.config}
        # ``storage.path: "tempdir"``: a fresh file-backed store per item, removed on
        # close. LanceDB's in-memory tables commit ~18 MB of memory per write that is
        # never released (7.5 GB for one LoCoMo conversation), so parallel paid runs
        # exhaust the machine's commit limit; a file-backed store peaks at ~0.8 GB.
        storage = dict(overrides.get("storage") or {})
        if storage.get("path") == TEMPDIR_STORAGE:
            import tempfile

            self._tempdir = tempfile.mkdtemp(prefix="memspine-eval-")
            storage["path"] = str(Path(self._tempdir) / "memspine.db")
            overrides["storage"] = storage
        # Never load a .env: the harness passes only what a run needs (a repo .env
        # can hold unrelated secrets, and a benchmark must not depend on it).
        overrides.setdefault("dotenv_path", None)
        # Questions must be independent: with access recording on, every search
        # refreshes last_accessed_at, so records retrieved for early questions
        # rank higher (recency) for later ones. Off unless a run asks for it.
        read = dict(overrides.get("read") or {})
        read.setdefault("record_access", False)
        overrides["read"] = read
        profile_on = str(overrides.get("data_profile", "off")).strip().lower() != "off"
        if profile_on and self._shape and "data_shape" not in overrides:
            overrides["data_shape"] = dict(self._shape)
        engine = Engine(template=self.template, **overrides)
        await engine.start()
        if profile_on and self._perspective_auto:
            # a preset may have switched the perspective layer on: send speaker and role too
            policy = engine._memory_policy(engine._config(), "episodic").get("perspective")
            self._perspective_metadata = bool(policy)
        return engine

    def _calls(self) -> int | None:
        """Total model calls the engine has made (None when it cannot say)."""
        counts = getattr(self._engine, "model_calls", None)
        return sum(counts().values()) if callable(counts) else None

    def _rerank_stats(self) -> dict[str, Any] | None:
        """``Engine.rerank_stats()`` (C-5), None for an engine without it."""
        stats = getattr(self._engine, "rerank_stats", None)
        return dict(stats()) if callable(stats) else None

    def _embed_model(self) -> str | None:
        """C-6: the paid (LiteLLM) embedder's model id, None for a local one."""
        embedding = dict(self.config.get("embedding") or {})
        return str(embedding.get("model")) if embedding.get("provider") == "litellm" else None

    def _rerank_model(self) -> str | None:
        """C-6: the paid (LiteLLM) reranker's model id, None for a local one or none."""
        read = dict(self.config.get("read") or {})
        return str(read.get("rerank_model")) if read.get("rerank") == "litellm" else None

    def _embed_services(self, texts: list[str]) -> dict[str, float]:
        """C-6: estimated embedding tokens for ``texts`` (the harness token counter; the
        engine does not report its embedder's usage), keyed ``embed:<model>``."""
        model = self._embed_model()
        if model is None or not texts:
            return {}
        return {f"embed:{model}": float(sum(self._counter.count(t) for t in texts))}

    def _usage(self) -> dict[str, dict[str, Any]]:
        """Per-role engine LLM use so far (``Engine.model_usage``), {} when unavailable."""
        usage = getattr(self._engine, "model_usage", None)
        return dict(usage()) if callable(usage) else {}

    @staticmethod
    def _usage_delta(
        before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Per-role calls and tokens spent between two ``_usage`` snapshots."""
        delta: dict[str, dict[str, Any]] = {}
        for role, now in after.items():
            then = before.get(role, {})
            diff = {k: int(now.get(k, 0)) - int(then.get(k, 0)) for k in _USAGE_KEYS}
            if any(diff.values()):
                delta[role] = {"model": now.get("model", ""), **diff}
        return delta

    def _prompt_usage(self) -> dict[str, dict[str, Any]]:
        """#33: per-prompt engine LLM use so far (``Engine.usage``), {} when unavailable."""
        usage = getattr(self._engine, "usage", None)
        return dict(usage()) if callable(usage) else {}

    @staticmethod
    def _prompt_delta(
        before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """#33: per-prompt calls and tokens spent between two ``_prompt_usage`` snapshots,
        so cost per cycle can be attributed to the loop stage each prompt serves."""
        delta: dict[str, dict[str, Any]] = {}
        for key, now in after.items():
            then = before.get(key, {})
            diff = {k: int(now.get(k, 0)) - int(then.get(k, 0)) for k in _PROMPT_USAGE_KEYS}
            if any(diff.values()):
                delta[key] = {"prompt_id": now.get("prompt_id"), "roles": now.get("roles"), **diff}
        return delta

    def begin_run(self, run_id: str) -> None:
        """D7 [INJ-6]: the runner announces the run before anything is ingested. Fails fast
        when the forensics directory already holds another run's logs."""
        import os

        self._run_id = run_id
        directory = os.environ.get("MEMSPINE_FORENSICS_DIR")
        if directory and not self._forensics_ready:
            prepare_forensics_dir(directory)
            self._forensics_ready = True

    def set_query_id(self, query_id: str) -> None:
        """D7: the id of the question about to be asked (forensics rows carry it)."""
        self._query_id = query_id

    async def reset(self, item_id: str) -> None:
        self._item_id = item_id
        await self.close()
        self._origin = {}
        self._sessions = set()
        self._buffer = []
        self._engine = await self._build_engine()

    async def insert(self, turn: Turn) -> DepositResult:
        if self._engine is None:
            self._engine = await self._build_engine()
        self._sessions.add(turn.session_id)
        if self.batch_turns <= 1:
            return await self._deposit([turn])
        # G9 batching: a session boundary flushes the previous session first, so
        # one write_messages call never mixes session ids, group ids or episodes.
        results: list[DepositResult] = []
        if self._buffer and self._buffer[0].session_id != turn.session_id:
            results.append(await self.flush())
        self._buffer.append(turn)
        if len(self._buffer) >= self.batch_turns:
            results.append(await self.flush())
        if not results:
            return DepositResult(meta={"buffered": len(self._buffer)})
        return _merge_deposits(results)

    def _message(self, turn: Turn, text: str) -> dict[str, Any]:
        """One ``write_messages`` entry. Default: role ``user`` for every turn (the stored
        content carries the speaker name). With perspective metadata: ``speaker`` = the
        turn's speaker, and the chat role when the speaker is ``user`` / ``assistant``."""
        if not self._perspective_metadata:
            return {"role": "user", "content": text}
        who = str(turn.speaker or "").strip()
        role = who.lower() if who.lower() in ("user", "assistant") else "user"
        message: dict[str, Any] = {"role": role, "content": text}
        if who:
            message["speaker"] = who
        return message

    async def flush(self) -> DepositResult:
        """Write the buffered turns (G9). The runner calls it before every query
        and before ``build``; ``query`` and ``build`` also call it themselves, so
        no read ever sees a history that is missing buffered turns."""
        if not self._buffer or self._engine is None:
            return DepositResult()
        turns, self._buffer = self._buffer, []
        return await self._deposit(turns)

    async def _deposit(self, turns: list[Turn]) -> DepositResult:
        """One ``write_messages`` call for turns of ONE session."""
        # The speaker NAME is content ("Caroline: ..."), not a provenance role:
        # passing it as the role dropped names from the stored text and gave
        # every speaker an unknown-role trust. The session stamp becomes the
        # record's event time (valid_from), so dated questions are answerable.
        session_id = turns[0].session_id
        texts = [f"{turn.speaker}: {turn.text}" for turn in turns]
        before = self._calls()
        usage_before = self._usage()
        prompts_before = self._prompt_usage()
        if len(turns) == 1:
            records = await self._engine.write_messages(
                [self._message(turns[0], texts[0])],
                namespace=self.namespace,
                session_id=session_id,
                group_id=session_id,
                valid_from=parse_turn_stamp(turns[0], self._zone),
            )
        else:
            messages: list[dict[str, Any]] = []
            for turn, text in zip(turns, texts, strict=True):
                message: dict[str, Any] = self._message(turn, text)
                stamp = parse_turn_stamp(turn, self._zone)
                if stamp is not None:
                    message["timestamp"] = stamp
                messages.append(message)
            records = await self._engine.write_messages(
                messages,
                namespace=self.namespace,
                session_id=session_id,
                group_id=session_id,
            )
        ids: list[str] = []
        for record, turn_id in _align(records, turns, texts):
            record_id = str(record.record_id)
            self._origin[record_id] = turn_id
            ids.append(record_id)
        self._write_ingest_log(records, turns, texts)
        meta: dict[str, Any] = {}
        n_quarantined = sum(1 for record in records if getattr(record, "quarantined", False))
        if n_quarantined:
            meta["n_quarantined"] = n_quarantined
        if len(turns) > 1:
            meta["batched_turns"] = [turn.turn_id for turn in turns]
        services = self._embed_services(texts)
        if services:
            meta["engine_services"] = services
        after = self._calls()
        if before is None or after is None:
            # An engine without ``model_calls()`` cannot report write cost; the
            # flag marks the gap rather than letting the ledger imply a free write.
            return DepositResult(
                n_records=len(ids),
                record_ids=tuple(ids),
                model_calls=0,
                meta={
                    **meta,
                    "cost_observable": False,
                    "cost_unknown_reason": "engine does not expose model_calls()",
                },
            )
        engine_llm = self._usage_delta(usage_before, self._usage())
        if engine_llm:
            meta["engine_llm"] = engine_llm
        engine_prompts = self._prompt_delta(prompts_before, self._prompt_usage())
        if engine_prompts:
            meta["engine_prompts"] = engine_prompts
        return DepositResult(
            n_records=len(ids),
            record_ids=tuple(ids),
            model_calls=after - before,
            meta=meta,
        )

    async def build(self) -> DepositResult:
        """After the history is in: run the sleep cycle when ``build_sleep`` is on.

        Its model calls (one per session per LLM stage) are measured and land in
        the synthesise (K) bucket; queries pinned mid-stream run before it.
        """
        flushed = await self.flush()
        if not self._build_sleep or self._engine is None:
            return flushed
        estimate = self.sleep_calls_per_session * max(len(self._sessions), 1)
        if hasattr(self._engine, "_llm") and not getattr(self._engine._llm, "roles", None):
            # Plan v3.2 (W5 rule miner): an engine with no LLM bound makes no model
            # calls at sleep, whatever stages are on.
            estimate = 0
        if self.remaining_model_calls is not None and estimate > self.remaining_model_calls:
            from ..runner import ModelCallBudgetExceeded

            raise ModelCallBudgetExceeded(
                f"sleep needs >= {estimate} model calls ({len(self._sessions)} sessions x "
                f"{self.sleep_calls_per_session}); only {self.remaining_model_calls} remain"
            )
        before = self._calls()
        usage_before = self._usage()
        prompts_before = self._prompt_usage()
        stats = await self._engine.sleep()
        after = self._calls()
        meta: dict[str, Any] = {"sleep": {name: dict(stage) for name, stage in stats.items()}}
        engine_llm = self._usage_delta(usage_before, self._usage())
        if engine_llm:
            meta["engine_llm"] = engine_llm
        engine_prompts = self._prompt_delta(prompts_before, self._prompt_usage())
        if engine_prompts:
            meta["engine_prompts"] = engine_prompts
        if before is None or after is None:
            # R3-10: unknown is not zero; the ledger must not report a free sleep.
            meta["cost_observable"] = False
            meta["cost_unknown_reason"] = "engine does not expose model_calls()"
            return DepositResult(model_calls=0, meta=meta)
        return DepositResult(model_calls=after - before + flushed.model_calls, meta=meta)

    def set_query_meta(self, meta: Mapping[str, Any]) -> None:
        """N34: the runner hands each question's metadata over before ``query``."""
        self._question_as_of = (
            parse_question_date(meta.get("question_date"), self._zone)
            if self._as_of_question_date
            else None
        )
        asker = meta.get("asker") or meta.get("persona")
        read_cfg = self.config.get("read") or {}
        wants_asker = (
            self._perspective_metadata
            or read_cfg.get("user_header") == "on"
            or read_cfg.get("owner_check", "off") != "off"
        )
        self._asker = str(asker) if asker and wants_asker else None
        #: I64: the session a re-injection penalty counts in: the question's own session id,
        #: else the persona / asker (one OP-Bench persona = one conversation); LoCoMo has
        #: neither, so the penalty never applies there.
        sid = meta.get("session_id") or asker
        self._read_session = (
            str(sid) if sid and float(read_cfg.get("reinjection_penalty") or 0.0) > 0.0 else None
        )

    def _write_ingest_log(self, records: list[Any], turns: list[Turn], texts: list[str]) -> None:
        """Injection audit (``MEMSPINE_FORENSICS_DIR``): one line per record written.

        Records the source turn next to what the engine stored: content fidelity, the event time
        it was filed under (``valid_from``), grouping and type, so a later miss can be traced back
        to a write-side defect (dropped or merged turn, wrong date, altered text).
        """
        import json
        import os
        from pathlib import Path

        directory = os.environ.get("MEMSPINE_FORENSICS_DIR")
        if not directory:
            return
        by_turn = {turn_id: record for record, turn_id in _align(records, turns, texts)}
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        # I73: the batch's per-step write timings (observability.write_timers), taken as a
        # delta and carried on the batch's first line only; absent when the timers are off.
        taker = getattr(self._engine, "write_timers", None)
        timers = taker(reset=True) if callable(taker) else {}
        with (out / "ingest.jsonl").open("a", encoding="utf-8") as fh:
            for position, (turn, text) in enumerate(zip(turns, texts, strict=True)):
                record = by_turn.get(turn.turn_id)
                stored = getattr(record, "content", None)
                valid_from = getattr(record, "valid_from", None)
                fh.write(
                    json.dumps(
                        {
                            "run_id": self._run_id,
                            "item": getattr(self, "_item_id", None),
                            "turn": turn.turn_id,
                            "session": turn.session_id,
                            "speaker": turn.speaker,
                            "source_timestamp": turn.timestamp,
                            "source_text": text,
                            "written": record is not None,
                            "record_id": str(record.record_id) if record is not None else None,
                            "stored_text": stored,
                            "text_identical": stored == text if stored is not None else None,
                            "valid_from": valid_from.isoformat() if valid_from else None,
                            "memory_type": str(getattr(record, "memory_type", None)),
                            "group_id": getattr(record, "group_id", None),
                            "session_id": getattr(record, "session_id", None),
                            "batch_size": len(turns),
                            # F2 [INJ-2]: what the firewall did with the record
                            "quarantined": getattr(record, "quarantined", None),
                            "trust": getattr(record, "trust", None),
                            "status": _enum_value(getattr(record, "status", None)),
                            **({"write_timers": timers} if timers and position == 0 else {}),
                        }
                    )
                    + "\n"
                )

    def _write_forensics(
        self, directory: str, query: str, stages: dict[str, Any], assembled: Any
    ) -> None:
        """One JSON line per question: every retrieval stage, ranked, with turn ids.

        Scores are the engine's own (RRF-normalised for fusion, raw cross-encoder for the
        reranker). ``turn`` is the dataset turn a record was written from; offline joining with
        the gold evidence (``evals/forensics_report.py``) names the stage that lost it.
        """
        import json
        from pathlib import Path

        def rank(pairs: list[tuple[Any, float]]) -> list[dict[str, Any]]:
            return [
                {
                    "r": i + 1,
                    "turn": self._origin.get(str(rid), str(rid)),
                    "score": round(float(s), 5),
                }
                for i, (rid, s) in enumerate(pairs)
            ]

        row = {
            "run_id": self._run_id,
            "query_id": self._query_id,
            "item": getattr(self, "_item_id", None),
            "namespace": self.namespace,
            "query": query,
            "vector": rank(stages.get("vector", [])),
            "lexical": rank(stages.get("lexical", [])),
            "extra_legs": {name: rank(hits) for name, hits in stages.get("extra_legs", [])},
            "fused": rank(stages.get("fused", [])),
            "pool": rank(stages.get("pool", [])),
            "reranker": stages.get("reranker"),
            # I24: trigger decisions the engine records (absent when the path did not run)
            **{k: stages[k] for k in (
                "bridge_gate",
                "bridge_phrases",
                "decider",
                # I74: the read-path decider decisions and the relevance-gate info
                "decisions",
                "relevance_calibration",
                "relevance_bypass",
            )
            if k in stages},
            "rerank_scores": rank(stages.get("rerank_scores", [])),
            "final": rank(stages.get("final", [])),
            "context_records": [
                {"turn": self._origin.get(str(r.record_id), str(r.record_id)), "text": r.content}
                for r in getattr(assembled, "records", [])
            ],
            "context_tokens": getattr(assembled, "tokens_used", None),
        }
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        with (out / "forensics.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        if self._engine is None:
            raise RuntimeError("query before reset/insert — no engine started")
        # A no-op when the runner already flushed; otherwise its write cost is
        # carried in this query's cost so the run total stays complete.
        flushed = await self.flush()
        before = self._calls()
        usage_before = self._usage()
        prompts_before = self._prompt_usage()
        rerank_before = self._rerank_stats()
        as_of = {"as_of": self._question_as_of} if self._question_as_of is not None else {}
        if self._read_session:
            as_of = {**as_of, "session_id": self._read_session}
        import os

        forensics_dir = os.environ.get("MEMSPINE_FORENSICS_DIR")
        from memspine.core.perspective import asker_scope
        from memspine.engine import search_forensics

        with search_forensics() as stages, asker_scope(self._asker):
            if self._read_mode:
                result = await self._engine.read(
                    text,
                    namespace=self.namespace,
                    mode=self._read_mode,
                    budget_tokens=budget_tokens,
                    top_k=top_k,
                    **as_of,
                )
                assembled = result.context
            else:
                assembled = await self._engine.assemble(
                    text,
                    namespace=self.namespace,
                    budget_tokens=budget_tokens,
                    top_k=top_k,
                    **as_of,
                )
        if forensics_dir:
            self._write_forensics(forensics_dir, text, stages, assembled)
        after = self._calls()
        engine_llm = self._usage_delta(usage_before, self._usage())
        engine_prompts = self._prompt_delta(prompts_before, self._prompt_usage())
        rerank_meta = self._rerank_meta(rerank_before, self._rerank_stats())
        services = self._embed_services([text])
        if rerank_meta.get("rerank_calls") and self._rerank_model() is not None:
            services[f"rerank:{self._rerank_model()}"] = float(rerank_meta["rerank_calls"])
        lines: list[str] = []
        evidence: list[Evidence] = []
        offset = 0
        # D1: the final search hits' ranks always travel with the evidence (meta["hit_rank"],
        # None = neighbour line) so a replay-mode context can be truncated by hit rank.
        all_hit_ranks = hit_rank_map(stages) if "final" in stages else None
        hit_ranks = all_hit_ranks if self._mark_hits != "off" and all_hit_ranks else {}
        records = list(assembled.records)
        # R2-4: line order. Entries are record indices, or literal separator lines that carry
        # no evidence; spans below are offsets in the final text, whatever the order.
        entries = order_context(
            [str(r.record_id) for r in records], all_hit_ranks or {}, self._context_order
        )
        for entry in entries:
            if isinstance(entry, str):
                lines.append(entry)
                offset += len(entry) + 1
                continue
            rank = entry
            record = records[rank]
            # Dated rendering: absolute event dates next to every retrieved line
            # (the single largest temporal-question lever in the literature).
            when = getattr(record, "valid_from", None)
            undated = "ts_defaulted" in (getattr(record, "tags", None) or ())  # I7
            prefix = (
                f"[{when:%Y-%m-%d}] " if self._dated and when is not None and not undated else ""
            )
            record_id = str(record.record_id)
            line = mark_hit_line(
                f"{prefix}{record.content}", hit_ranks.get(record_id), self._mark_hits
            )
            lines.append(line)
            evidence.append(
                Evidence(
                    turn_id=self._origin.get(record_id, record_id),
                    score=1.0 / (rank + 1),
                    meta={
                        "memory_type": getattr(record, "memory_type", None),
                        "unit_id": record_id,
                        **(
                            {"hit_rank": all_hit_ranks.get(record_id)}
                            if all_hit_ranks is not None
                            else {}
                        ),
                        "span": (offset, offset + len(line)),
                    },
                )
            )
            offset += len(line) + 1
        body = "\n".join(lines)
        cost: dict[str, Any] = (
            {"model_calls": after - before + flushed.model_calls}
            if before is not None and after is not None
            else {
                "model_calls": 0,
                "cost_observable": False,
                "cost_unknown_reason": "engine does not expose model_calls()",
            }
        )
        return RetrievedContext(
            text=body,
            tokens=getattr(assembled, "tokens_used", 0) or self._counter.count(body),
            evidence=tuple(evidence),
            truncated=False,
            boundary_index=getattr(assembled, "boundary_index", None),
            meta={
                "abstained": getattr(assembled, "abstained", False),
                **(
                    {"evidence_signal": dataclasses.asdict(signal)}
                    if (signal := getattr(assembled, "evidence", None)) is not None
                    else {}
                ),
                "n_records": len(evidence),
                **({"n_marked_hits": len(hit_ranks)} if self._mark_hits != "off" else {}),
                "read_mode": self._read_mode or "assemble",
                "ranked": self._read_mode is None,
                **({"flushed_records": flushed.n_records} if flushed.n_records else {}),
                # query-side calls (rewrites, relevance filter, planner LLMs)
                **cost,
                **({"engine_llm": engine_llm} if engine_llm else {}),
                **({"engine_prompts": engine_prompts} if engine_prompts else {}),
                **rerank_meta,
                **({"engine_services": services} if services else {}),
            },
        )

    @staticmethod
    def _rerank_meta(
        before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """C-5: this query's rerank audit: the configured mode, whether a rerank returned
        scores (``reranked``), and its attempts and failures. {} without the counters."""
        if before is None or after is None:
            return {}
        calls = int(after.get("calls", 0)) - int(before.get("calls", 0))
        failures = int(after.get("failures", 0)) - int(before.get("failures", 0))
        return {
            "rerank_mode": after.get("mode"),
            "reranked": calls - failures > 0,
            "rerank_calls": calls,
            "rerank_failures": failures,
            **({"rerank_unavailable": True} if after.get("unavailable") else {}),
        }

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.stop()
            self._engine = None
        tempdir = getattr(self, "_tempdir", None)
        if tempdir:
            import shutil

            shutil.rmtree(tempdir, ignore_errors=True)
            self._tempdir = None


def _enum_value(value: Any) -> Any:
    """An enum member's value (``RecordStatus.ACTIVATED`` -> ``"activated"``), else as is."""
    return getattr(value, "value", value)


def _align(records: list[Any], turns: list[Turn], texts: list[str]) -> list[tuple[Any, str]]:
    """Pair each written record with the turn it came from.

    ``write_messages`` returns records in message order, one per turn unless the
    engine skipped a turn (role or recalled-memory filters). Equal counts zip.
    Otherwise each record is matched, in order, to the next turn with identical
    content; a record that matches none (its content was redacted) takes the
    next unmatched turn.
    """
    if len(records) == len(turns):
        return [(record, turn.turn_id) for record, turn in zip(records, turns, strict=True)]
    pairs: list[tuple[Any, str]] = []
    cursor = 0
    for record in records:
        content = getattr(record, "content", None)
        match = next((i for i in range(cursor, len(turns)) if texts[i] == content), None)
        index = cursor if match is None else match
        if index >= len(turns):
            break
        pairs.append((record, turns[index].turn_id))
        cursor = index + 1
    return pairs


def _merge_deposits(results: list[DepositResult]) -> DepositResult:
    """One insert's view of the flushes it triggered (a session boundary and a
    full buffer can both happen on the same turn)."""
    if len(results) == 1:
        return results[0]
    meta: dict[str, Any] = {}
    for result in results:
        for key, value in result.meta.items():
            if key == "batched_turns":
                meta.setdefault(key, []).extend(value)
            elif key == "n_quarantined":
                meta[key] = int(meta.get(key, 0)) + int(value)
            elif key == "engine_services":
                services = meta.setdefault(key, {})
                for name, units in value.items():
                    services[name] = services.get(name, 0.0) + float(units)
            elif key == "engine_llm":
                merged = meta.setdefault(key, {})
                for role, usage in value.items():
                    slot = merged.setdefault(
                        role, {"model": usage.get("model", ""), **dict.fromkeys(_USAGE_KEYS, 0)}
                    )
                    for usage_key in _USAGE_KEYS:
                        slot[usage_key] += int(usage.get(usage_key, 0))
            else:
                meta[key] = value
    return DepositResult(
        n_records=sum(r.n_records for r in results),
        record_ids=tuple(i for r in results for i in r.record_ids),
        model_calls=sum(r.model_calls for r in results),
        meta=meta,
    )


def parse_question_date(value: Any, zone: tzinfo = UTC) -> datetime | None:
    """N34: LongMemEval's ``question_date`` ("2023/05/30 (Tue) 23:40") in ``zone`` (default
    UTC, the zone the turn stamps are read in), else None."""
    import re

    if not value:
        return None
    m = re.match(
        r"\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s*\(\w+\))?(?:[ T]+(\d{1,2}):(\d{2}))?",
        str(value),
    )
    if not m:
        # I8: a question date in an unknown format must not silently drop the anchor.
        logging.getLogger(__name__).warning("unparseable question_date %r: no as-of anchor", value)
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    hh, mm = int(m[4] or 23), int(m[5] or 59)
    return datetime(y, mo, d, hh, mm, tzinfo=zone)
