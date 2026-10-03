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

import re
from collections.abc import Mapping
from datetime import UTC, datetime
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

    async def reset(self, item_id: str) -> None:
        await self.close()
        self._origin = {}
        self._sessions = set()
        self._engine = await self._build_engine()

    async def insert(self, turn: Turn) -> DepositResult:
        if self._engine is None:
            self._engine = await self._build_engine()
        # The speaker NAME is content ("Caroline: ..."), not a provenance role:
        # passing it as the role dropped names from the stored text and gave
        # every speaker an unknown-role trust. The session stamp becomes the
        # record's event time (valid_from), so dated questions are answerable.
        self._sessions.add(turn.session_id)
        before = self._calls()
        usage_before = self._usage()
        records = await self._engine.write_messages(
            [{"role": "user", "content": f"{turn.speaker}: {turn.text}"}],
            namespace=self.namespace,
            session_id=turn.session_id,
            group_id=turn.session_id,
            valid_from=parse_turn_time(turn.timestamp),
        )
        ids: list[str] = []
        for record in records:
            record_id = str(record.record_id)
            self._origin[record_id] = turn.turn_id
            ids.append(record_id)
        after = self._calls()
        if before is None or after is None:
            # An engine without ``model_calls()`` cannot report write cost; the
            # flag marks the gap rather than letting the ledger imply a free write.
            return DepositResult(
                n_records=len(ids),
                record_ids=tuple(ids),
                model_calls=0,
                meta={
                    "cost_observable": False,
                    "cost_unknown_reason": "engine does not expose model_calls()",
                },
            )
        engine_llm = self._usage_delta(usage_before, self._usage())
        return DepositResult(
            n_records=len(ids),
            record_ids=tuple(ids),
            model_calls=after - before,
            meta={"engine_llm": engine_llm} if engine_llm else {},
        )

    async def build(self) -> DepositResult:
        """After the history is in: run the sleep cycle when ``build_sleep`` is on.

        Its model calls (one per session per LLM stage) are measured and land in
        the synthesise (K) bucket; queries pinned mid-stream run before it.
        """
        if not self._build_sleep or self._engine is None:
            return DepositResult()
        estimate = self.sleep_calls_per_session * max(len(self._sessions), 1)
        if self.remaining_model_calls is not None and estimate > self.remaining_model_calls:
            from ..runner import ModelCallBudgetExceeded

            raise ModelCallBudgetExceeded(
                f"sleep needs >= {estimate} model calls ({len(self._sessions)} sessions x "
                f"{self.sleep_calls_per_session}); only {self.remaining_model_calls} remain"
            )
        before = self._calls()
        usage_before = self._usage()
        stats = await self._engine.sleep()
        after = self._calls()
        meta: dict[str, Any] = {"sleep": {name: dict(stage) for name, stage in stats.items()}}
        engine_llm = self._usage_delta(usage_before, self._usage())
        if engine_llm:
            meta["engine_llm"] = engine_llm
        if before is None or after is None:
            # R3-10: unknown is not zero; the ledger must not report a free sleep.
            meta["cost_observable"] = False
            meta["cost_unknown_reason"] = "engine does not expose model_calls()"
            return DepositResult(model_calls=0, meta=meta)
        return DepositResult(model_calls=after - before, meta=meta)

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        if self._engine is None:
            raise RuntimeError("query before reset/insert — no engine started")
        before = self._calls()
        usage_before = self._usage()
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
            {"model_calls": after - before}
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
                "n_records": len(evidence),
                "read_mode": self._read_mode or "assemble",
                "ranked": self._read_mode is None,
                # query-side calls (rewrites, relevance filter, planner LLMs)
                **cost,
                **({"engine_llm": engine_llm} if engine_llm else {}),
            },
        )

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.stop()
            self._engine = None
