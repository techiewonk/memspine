"""Per-stage cost ledger, write targets and budget reporting (LIB-27; W2-12, T5-06).

Pure and unwired. The ledger records, for each pipeline stage, how many model calls it made, how
many tokens they cost, how long they took and how they ended, split by cost class (generative,
embedding, rerank, tool; ER-39). It never holds content: every string field is an identifier
(a stage name, a hash, an id), checked by a guard that rejects anything shaped like free text.
Durations come from the caller; this module reads no clock.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.public import CostRecord

__all__ = [
    "MAX_ID_LENGTH",
    "OTEL_ATTRIBUTE_KEYS",
    "Aggregate",
    "ContentGuardError",
    "StageEntry",
    "StageLedger",
    "TargetVerdict",
    "WriteTargets",
    "information_per_token",
    "percentile",
    "scope_hash",
    "to_otel_attributes",
]

MAX_ID_LENGTH = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+#=\-]*")

CostClass = Literal["generative", "embedding", "rerank", "tool"]
Outcome = Literal["ok", "error", "timeout", "skipped", "retried"]
GroupBy = Literal["request", "job", "stage", "class", "model"]


class ContentGuardError(ValueError):
    """A ledger field looked like content rather than an identifier."""


def _guard(name: str, value: str) -> str:
    """Accept only short, whitespace-free identifier strings."""
    if len(value) > MAX_ID_LENGTH:
        raise ContentGuardError(f"{name} is longer than {MAX_ID_LENGTH} characters: not an id")
    if any(ch.isspace() for ch in value):
        raise ContentGuardError(f"{name} contains whitespace: not an id")
    if not _ID_RE.fullmatch(value):
        raise ContentGuardError(
            f"{name} is not an identifier (allowed: letters, digits, . _ : / @ + # = -)"
        )
    return value


def scope_hash(scope: OwnerScope) -> str:
    """Stable 16-hex-digit hash of a scope, so the ledger carries no tenant or owner names."""
    raw = f"{scope.tenant}\x1f{scope.owner}\x1f{scope.owner_kind}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class StageEntry(BaseModel):
    """One stage's cost for one request or job. Ids, counts and durations only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    scope_hash: str
    request_or_job_id: str
    cost_class: CostClass
    model_id: str | None = None
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    calls: int = Field(default=1, ge=0)
    duration_ms: float = Field(default=0.0, ge=0)
    outcome: Outcome = "ok"
    is_job: bool = False

    @field_validator("stage", "scope_hash", "request_or_job_id", "model_id")
    @classmethod
    def _ids_only(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _guard(str(info.field_name), value)

    @field_validator("duration_ms")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("duration_ms must be finite")
        return value

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @classmethod
    def from_cost_record(
        cls, record: CostRecord, *, stage: str, outcome: Outcome = "ok"
    ) -> StageEntry:
        """Build an entry from a public :class:`CostRecord` (request or job id kept)."""
        is_job = record.request_id is None
        ident = record.job_id if is_job else record.request_id
        if ident is None:  # unreachable: CostRecord requires one of the two
            raise ValueError("cost record has neither request nor job id")
        return cls(
            stage=stage,
            scope_hash=scope_hash(record.scope) if record.scope is not None else "none",
            request_or_job_id=ident,
            cost_class=record.cost_class,
            model_id=record.model_id if record.cost_class != "tool" else record.tool_name,
            prompt_tokens=record.tokens_in,
            completion_tokens=record.tokens_out,
            calls=record.calls,
            duration_ms=record.latency_ms or 0.0,
            outcome=outcome,
            is_job=is_job,
        )


def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None when there are no values."""
    if not 0 <= q <= 100:
        raise ValueError("q must be within 0..100")
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


class Aggregate(BaseModel):
    """Totals over a group of entries."""

    model_config = ConfigDict(frozen=True)

    key: str
    entries: int
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms_total: float
    duration_ms_p50: float | None
    duration_ms_p95: float | None
    failures: int


class StageLedger:
    """Append-only, in-memory ledger of :class:`StageEntry` (no content, deterministic order)."""

    def __init__(self) -> None:
        self._entries: list[StageEntry] = []

    def append(self, entry: StageEntry) -> None:
        self._entries.append(entry)

    def record(self, **fields: Any) -> StageEntry:
        """Build a :class:`StageEntry` from keyword fields (guarded) and append it."""
        entry = StageEntry(**fields)
        self._entries.append(entry)
        return entry

    def extend(self, entries: Iterable[StageEntry]) -> None:
        for entry in entries:
            self.append(entry)

    @property
    def entries(self) -> tuple[StageEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def select(
        self,
        *,
        stages: Iterable[str] | None = None,
        cost_class: str | None = None,
        request_or_job_id: str | None = None,
    ) -> list[StageEntry]:
        wanted = None if stages is None else set(stages)
        return [
            e
            for e in self._entries
            if (wanted is None or e.stage in wanted)
            and (cost_class is None or e.cost_class == cost_class)
            and (request_or_job_id is None or e.request_or_job_id == request_or_job_id)
        ]

    @staticmethod
    def _key(entry: StageEntry, by: GroupBy) -> str | None:
        if by == "request":
            return None if entry.is_job else entry.request_or_job_id
        if by == "job":
            return entry.request_or_job_id if entry.is_job else None
        if by == "stage":
            return entry.stage
        if by == "class":
            return entry.cost_class
        return entry.model_id

    def aggregate(
        self, by: GroupBy, entries: Iterable[StageEntry] | None = None
    ) -> dict[str, Aggregate]:
        """Group by request, job, stage, cost class or model; keys sorted.

        Entries lacking the key (a job entry under ``request``, no model id) are skipped.
        """
        groups: dict[str, list[StageEntry]] = {}
        for entry in self._entries if entries is None else entries:
            key = self._key(entry, by)
            if key is not None:
                groups.setdefault(key, []).append(entry)
        out: dict[str, Aggregate] = {}
        for key in sorted(groups):
            items = groups[key]
            durations = [e.duration_ms for e in items]
            prompt = sum(e.prompt_tokens for e in items)
            completion = sum(e.completion_tokens for e in items)
            out[key] = Aggregate(
                key=key,
                entries=len(items),
                calls=sum(e.calls for e in items),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                duration_ms_total=sum(durations),
                duration_ms_p50=percentile(durations, 50),
                duration_ms_p95=percentile(durations, 95),
                failures=sum(1 for e in items if e.outcome in ("error", "timeout")),
            )
        return out

    def calls_per_turn(
        self,
        turns: int,
        *,
        cost_class: str | None = "generative",
        stages: Iterable[str] | None = None,
    ) -> float | None:
        """Model calls (generative by default, ER-39) per turn; None when ``turns`` is zero."""
        if turns <= 0:
            return None
        chosen = self.select(stages=stages, cost_class=cost_class)
        return sum(e.calls for e in chosen) / turns

    def request_durations(self, stages: Iterable[str] | None = None) -> dict[str, float]:
        """Summed stage duration per request id (stages run in sequence), over ``stages``."""
        sums: dict[str, float] = {}
        for entry in self.select(stages=stages):
            if not entry.is_job:
                key = entry.request_or_job_id
                sums[key] = sums.get(key, 0.0) + entry.duration_ms
        return dict(sorted(sums.items()))


# --- targets -----------------------------------------------------------------------------------


class TargetVerdict(BaseModel):
    """Outcome of one target check, with the numbers (observed is at most target to be met)."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: Literal["met", "missed", "not_measured"]
    observed: float | None
    target: float


class WriteTargets(BaseModel):
    """Configurable write-side targets.

    EX-2: write p95 at most 250 ms and at most one generative call per window.
    EX-4: sleep at most 0.2 calls per turn. A target set to None is not checked.

    ``write_stages``, ``window_stages`` and ``sleep_stages`` name the ledger stages that count
    toward each target; an empty ``write_stages`` or ``window_stages`` means every stage.
    ``sleep_calls_per_turn`` is not measured without ``sleep_stages``.
    """

    model_config = ConfigDict(frozen=True)

    write_p95_ms: float | None = 250.0
    generative_calls_per_window: float | None = 1.0
    sleep_calls_per_turn: float | None = 0.2
    write_stages: tuple[str, ...] = ()
    window_stages: tuple[str, ...] = ()
    sleep_stages: tuple[str, ...] = ()

    def check(
        self, ledger: StageLedger, *, turns: int | None = None, windows: int | None = None
    ) -> list[TargetVerdict]:
        verdicts: list[TargetVerdict] = []
        if self.write_p95_ms is not None:
            p95 = percentile(ledger.request_durations(self.write_stages or None).values(), 95)
            verdicts.append(self._verdict("write_p95_ms", p95, self.write_p95_ms))
        if self.generative_calls_per_window is not None:
            per_window = None
            if windows and windows > 0:
                chosen = ledger.select(stages=self.window_stages or None, cost_class="generative")
                per_window = sum(e.calls for e in chosen) / windows
            verdicts.append(
                self._verdict(
                    "generative_calls_per_window", per_window, self.generative_calls_per_window
                )
            )
        if self.sleep_calls_per_turn is not None:
            per_turn = None
            if self.sleep_stages:
                per_turn = ledger.calls_per_turn(
                    turns or 0, cost_class=None, stages=self.sleep_stages
                )
            verdicts.append(
                self._verdict("sleep_calls_per_turn", per_turn, self.sleep_calls_per_turn)
            )
        return verdicts

    @staticmethod
    def _verdict(name: str, observed: float | None, target: float) -> TargetVerdict:
        if observed is None:
            return TargetVerdict(name=name, status="not_measured", observed=None, target=target)
        status: Literal["met", "missed"] = "met" if observed <= target else "missed"
        return TargetVerdict(name=name, status=status, observed=observed, target=target)


# --- information per token (ER-16) -----------------------------------------------------------


def information_per_token(
    evidence_tokens_used: int,
    answer_supported: bool | None,
    *,
    total_tokens: int | None = None,
) -> float | None:
    """Useful grounded information per token of the complete request (ER-16, under-specified).

    ER-16 asks for "useful grounded information per token" over complete requests (schema,
    context, output, retries) but fixes no formula. This helper encodes the minimal reading:
    the evidence tokens the answer actually used count as information only when the answer is
    supported by evidence, divided by the complete request's tokens (``total_tokens``, e.g. an
    :class:`Aggregate` total). ``answer_supported`` None (unjudged) or a missing or zero
    ``total_tokens`` gives None; unsupported gives 0.0. Fidelity and recall at equal budgets are
    measured elsewhere and are not folded in here.
    """
    if evidence_tokens_used < 0:
        raise ValueError("evidence_tokens_used must be non-negative")
    if answer_supported is None or not total_tokens or total_tokens <= 0:
        return None
    if not answer_supported:
        return 0.0
    return min(evidence_tokens_used, total_tokens) / total_tokens


# --- OTel export (T5-06) ----------------------------------------------------------------------

OTEL_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "memspine.stage",
    "memspine.scope_hash",
    "memspine.request_or_job_id",
    "memspine.cost_class",
    "memspine.model_id",
    "memspine.prompt_tokens",
    "memspine.completion_tokens",
    "memspine.calls",
    "memspine.duration_ms",
    "memspine.outcome",
)


def to_otel_attributes(entry: StageEntry) -> Mapping[str, str | int | float]:
    """Flat dict of allowed keys only (no OTel import; no content). None values are omitted."""
    raw: dict[str, str | int | float | None] = {
        "memspine.stage": entry.stage,
        "memspine.scope_hash": entry.scope_hash,
        "memspine.request_or_job_id": entry.request_or_job_id,
        "memspine.cost_class": entry.cost_class,
        "memspine.model_id": entry.model_id,
        "memspine.prompt_tokens": entry.prompt_tokens,
        "memspine.completion_tokens": entry.completion_tokens,
        "memspine.calls": entry.calls,
        "memspine.duration_ms": entry.duration_ms,
        "memspine.outcome": entry.outcome,
    }
    return {k: v for k, v in raw.items() if v is not None}
