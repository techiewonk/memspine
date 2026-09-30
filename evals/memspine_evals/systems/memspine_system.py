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
        self._engine: Any = None
        self._version = "unknown"
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

    async def reset(self, item_id: str) -> None:
        await self.close()
        self._origin = {}
        self._engine = await self._build_engine()

    async def insert(self, turn: Turn) -> DepositResult:
        if self._engine is None:
            self._engine = await self._build_engine()
        # The speaker NAME is content ("Caroline: ..."), not a provenance role:
        # passing it as the role dropped names from the stored text and gave
        # every speaker an unknown-role trust. The session stamp becomes the
        # record's event time (valid_from), so dated questions are answerable.
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
        return DepositResult(
            n_records=len(ids),
            record_ids=tuple(ids),
            # The public facade does not report per-write model calls, so this
            # is 0 by observation, not by measurement. Profiles that enable LLM
            # extraction or entity NER on the write path DO call models; a run
            # that needs deposit-stage cost must read it from the engine's own
            # structlog events, and the flag below marks the gap rather than
            # letting the ledger imply a free write.
            model_calls=0,
            meta={
                "cost_observable": False,
                "cost_unknown_reason": (
                    "memspine's public facade does not report per-write model calls; "
                    "profiles with LLM extraction or NER on the write path do call models"
                ),
            },
        )

    async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
        if self._engine is None:
            raise RuntimeError("query before reset/insert — no engine started")
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
        lines: list[str] = []
        evidence: list[Evidence] = []
        for rank, record in enumerate(assembled.records):
            # Dated rendering: absolute event dates next to every retrieved line
            # (the single largest temporal-question lever in the literature).
            when = getattr(record, "valid_from", None)
            prefix = f"[{when:%Y-%m-%d}] " if self._dated and when is not None else ""
            lines.append(f"{prefix}{record.content}")
            record_id = str(record.record_id)
            evidence.append(
                Evidence(
                    turn_id=self._origin.get(record_id, record_id),
                    score=1.0 / (rank + 1),
                    meta={"memory_type": getattr(record, "memory_type", None)},
                )
            )
        body = "\n".join(lines)
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
            },
        )

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.stop()
            self._engine = None
