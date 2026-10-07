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
import re
from collections.abc import Mapping
from datetime import UTC, datetime
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


def parse_turn_time(stamp: str | None) -> datetime | None:
    """Benchmark session stamps -> aware datetime (None if absent or unknown format)."""
    if not stamp:
        return None
    text = re.sub(r"\s+", " ", stamp.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


#: ``storage.path`` sentinel: one fresh file-backed store per item (see ``_build_engine``).
TEMPDIR_STORAGE = "tempdir"


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
    ) -> None:
        self.system_id = system_id
        # ``template`` names a config template (base, personal, coding, ...);
        # the *profile* is a field those templates set. Conflating them would
        # silently evaluate the default engine while claiming another.
        self.template = template
        self.namespace = namespace
        self.config = dict(config or {})
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
            "token_counter": dict(self._counter.describe()),
        }

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
        engine = Engine(template=self.template, **overrides)
        await engine.start()
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

    async def reset(self, item_id: str) -> None:
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
                [{"role": "user", "content": texts[0]}],
                namespace=self.namespace,
                session_id=session_id,
                group_id=session_id,
                valid_from=parse_turn_time(turns[0].timestamp),
            )
        else:
            messages: list[dict[str, Any]] = []
            for turn, text in zip(turns, texts, strict=True):
                message: dict[str, Any] = {"role": "user", "content": text}
                stamp = parse_turn_time(turn.timestamp)
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
        meta: dict[str, Any] = {}
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
        if self._read_mode:
            result = await self._engine.read(
                text,
                namespace=self.namespace,
                mode=self._read_mode,
                budget_tokens=budget_tokens,
                top_k=top_k,
            )
            assembled = result.context
        else:
            assembled = await self._engine.assemble(
                text, namespace=self.namespace, budget_tokens=budget_tokens, top_k=top_k
            )
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
        for rank, record in enumerate(assembled.records):
            # Dated rendering: absolute event dates next to every retrieved line
            # (the single largest temporal-question lever in the literature).
            when = getattr(record, "valid_from", None)
            prefix = f"[{when:%Y-%m-%d}] " if self._dated and when is not None else ""
            line = f"{prefix}{record.content}"
            lines.append(line)
            record_id = str(record.record_id)
            evidence.append(
                Evidence(
                    turn_id=self._origin.get(record_id, record_id),
                    score=1.0 / (rank + 1),
                    meta={
                        "memory_type": getattr(record, "memory_type", None),
                        "unit_id": record_id,
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
