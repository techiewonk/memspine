"""Result rows, and the rule that a row cannot exist without its manifest.

``ResultWriter`` takes a ``RunManifest`` in its constructor and writes it as
the first line of the results file. There is no other entry point, so a bare
number cannot be produced by this harness even by accident — which is the one
structural defence against the failure mode documented across the field's top
twenty scores.
"""

from __future__ import annotations

import csv
import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .judge import JudgeScale, to_unit_interval
from .metrics import Ledger, summarise_floats
from .provenance import RunManifest


class RowStatus(str, Enum):
    """Per-question completion status (`EVALUATION_PLAN_2026-09.md` §5).

    ``UNATTEMPTED`` is the one that matters and the one harnesses usually lack.
    The native run recorded in ``paper_spine/evaluation/NATIVE_MODEL_RESULTS.md``
    had 60 scheduled slots, 29 completed responses, 17 truncations and **14
    unattempted** — a denominator that only stays honest if the unattempted
    slots are written down. A run that stops early must still account for every
    question it scheduled.
    """

    COMPLETED = "completed"
    TRUNCATED = "truncated"
    ERROR = "error"
    UNATTEMPTED = "unattempted"


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One query, one system, one protocol.

    Carries its own identity fields (``protocol_id``, ``dataset_revision``,
    ``seed``) rather than relying on the manifest alone: the evaluation plan
    requires each record to be self-describing, and rows get copied out of
    their file.
    """

    run_id: str
    item_id: str
    query_id: str
    question: str
    gold: str | None
    answer: str
    score: float
    scale: str
    status: str = RowStatus.COMPLETED.value
    protocol_id: str = ""
    dataset_revision: str = ""
    system_id: str = ""
    seed: int = 0
    type_label: str | None = None
    context_tokens: int = 0
    context_truncated: bool = False
    answer_truncated: bool = False
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0  # C9': cache-served part of prompt_tokens
    completion_tokens: int = 0
    latency_retrieve_ms: float = 0.0
    latency_answer_ms: float = 0.0
    latency_judge_ms: float = 0.0
    model_calls: int = 0
    retrieved_ids: tuple[str, ...] = ()
    recall: Mapping[str, float | None] = field(default_factory=dict)
    error: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def scored(self) -> bool:
        """Only completed rows enter the mean. Errors and unattempted slots stay
        in the file, and in the denominator, without being silently read as 0."""
        return self.status in (RowStatus.COMPLETED.value, RowStatus.TRUNCATED.value)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["retrieved_ids"] = list(self.retrieved_ids)
        payload["recall"] = dict(self.recall)
        return payload


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Aggregate of one run. Every field is reported together, on purpose."""

    run_id: str
    n_queries: int
    n_errors: int
    n_scheduled: int
    n_truncated: int
    n_unattempted: int
    score_mean: float
    score_mean_measured: float
    score_ci95: tuple[float, float]
    score_scale: str
    failure_score: float
    cost_accounting_complete: bool
    by_type: Mapping[str, float]
    recall: Mapping[str, float]
    context_tokens: Mapping[str, float]
    latency_retrieve_ms: Mapping[str, float]
    latency_answer_ms: Mapping[str, float]
    model_calls: int
    cost_usd: float
    cost_per_cycle_usd: float
    stages: Mapping[str, Any]
    admissible_d16: bool
    missing_protocol_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["missing_protocol_fields"] = list(self.missing_protocol_fields)
        payload["score_ci95"] = list(self.score_ci95)
        return payload

    def headline(self) -> str:
        admissible = "admissible" if self.admissible_d16 else "INADMISSIBLE (D16)"
        lo, hi = self.score_ci95
        missing = self.n_scheduled - self.n_queries
        gap = f" | {missing} not scored" if missing else ""
        cost = f"${self.cost_per_cycle_usd:.6f}" if self.cost_accounting_complete else "incomplete"
        return (
            f"{self.run_id}: {self.score_mean:.4f} [{lo:.3f}, {hi:.3f}] {self.score_scale} "
            f"over n={self.n_queries}/{self.n_scheduled}{gap} "
            f"| ctx {self.context_tokens.get('mean', 0):.0f} tok "
            f"| {self.latency_answer_ms.get('p50', 0):.0f} ms p50 "
            f"| CPC {cost} | {admissible}"
        )


def cluster_bootstrap_ci(
    rows: Sequence[ResultRow],
    scale: JudgeScale,
    seed: int = 0,
    resamples: int = 2000,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """95% CI resampled by **item**, not by question.

    Ten LoCoMo conversations carry perhaps 1,986 questions, and treating those
    questions as independent would report a precision the design does not have.
    The evaluation plan is explicit: resample by independent history or
    conversation. Turns within one conversation are correlated; the cluster is
    the unit of independence.
    """
    by_item: dict[str, list[float]] = {}
    for row in rows:
        by_item.setdefault(row.item_id, []).append(to_unit_interval(row.score, scale))
    clusters = list(by_item.values())
    if len(clusters) < 2:
        flat = [v for values in clusters for v in values]
        mean = sum(flat) / len(flat) if flat else 0.0
        return (round(mean, 4), round(mean, 4))
    rng = random.Random(seed)
    n = len(clusters)
    means: list[float] = []
    for _ in range(resamples):
        drawn = [clusters[rng.randrange(n)] for _ in range(n)]
        flat = [value for cluster in drawn for value in cluster]
        if flat:
            means.append(sum(flat) / len(flat))
    means.sort()
    lo = means[int((alpha / 2) * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return (round(lo, 4), round(hi, 4))


def aggregate(
    manifest: RunManifest,
    rows: Sequence[ResultRow],
    ledger: Ledger,
    failure_score: float = 0.0,
) -> RunSummary:
    """Mean over *one* scale. Rows of mixed scales are a programming error, not
    something to average: the harness raises rather than pooling them.

    Two means, because `LOOP_METRIC_CONTRACT.md` draws a line the field usually
    does not: *"A known failed or truncated answer receives the declared failure
    score, not deletion from the denominator. Missing instrumentation is
    unknown, not zero."*

    * ``score_mean`` — failed answers scored at ``failure_score`` and kept in
      the denominator. This is the headline.
    * ``score_mean_measured`` — only questions that produced a gradeable answer.
      Higher, and meaningless on its own.

    Unattempted questions are in neither: they are unknown, counted in
    ``n_unattempted`` and never imputed.
    """
    scored = [r for r in rows if r.scored and r.error is None]
    failed = [r for r in rows if r.status == RowStatus.ERROR.value]
    scales = {r.scale for r in scored}
    if len(scales) > 1:
        raise ValueError(
            f"refusing to aggregate mixed judge scales {sorted(scales)} — "
            "this is exactly the error the field makes in its comparison tables"
        )
    scale_value = scales.pop() if scales else manifest.judge.scale.value
    scale = JudgeScale(scale_value)
    units = [to_unit_interval(r.score, scale) for r in scored]

    by_type: dict[str, float] = {}
    labels = {r.type_label for r in scored if r.type_label}
    for label in sorted(labels):
        subset = [to_unit_interval(r.score, scale) for r in scored if r.type_label == label]
        by_type[label] = round(sum(subset) / len(subset), 4) if subset else 0.0

    recall: dict[str, float] = {}
    for k in manifest.protocol.recall_ks:
        key = f"R@{k}"
        values = [r.recall.get(key) for r in scored]
        present = [v for v in values if v is not None]
        if present:
            recall[key] = round(sum(present) / len(present), 4)

    return RunSummary(
        run_id=manifest.run_id,
        n_queries=len(scored),
        n_errors=sum(1 for r in rows if r.status == RowStatus.ERROR.value),
        n_scheduled=len(rows),
        n_truncated=sum(1 for r in rows if r.status == RowStatus.TRUNCATED.value),
        n_unattempted=sum(1 for r in rows if r.status == RowStatus.UNATTEMPTED.value),
        score_mean=(
            round((sum(units) + failure_score * len(failed)) / (len(units) + len(failed)), 4)
            if (units or failed)
            else 0.0
        ),
        score_mean_measured=round(sum(units) / len(units), 4) if units else 0.0,
        score_ci95=cluster_bootstrap_ci(scored, scale, seed=manifest.protocol.seed),
        score_scale=scale_value,
        failure_score=failure_score,
        cost_accounting_complete=ledger.complete,
        by_type=by_type,
        recall=recall,
        context_tokens=summarise_floats([float(r.context_tokens) for r in scored]),
        latency_retrieve_ms=summarise_floats([r.latency_retrieve_ms for r in scored]),
        latency_answer_ms=summarise_floats([r.latency_answer_ms for r in scored]),
        model_calls=sum(r.model_calls for r in rows),
        cost_usd=round(ledger.total_cost_usd, 6),
        # Attempted cycles: completed, truncated and failed. Failed attempts
        # cost real money and stay in the denominator (contract §5); cycles that
        # never ran do not.
        cost_per_cycle_usd=round(ledger.cost_per_cycle(len(scored) + len(failed)), 8),
        stages=ledger.to_dict(),
        admissible_d16=manifest.admissible_d16,
        missing_protocol_fields=manifest.missing_protocol_fields(),
    )


class ResultWriter:
    """JSONL results whose first line is the manifest. No other constructor."""

    def __init__(self, path: Path, manifest: RunManifest) -> None:
        if not isinstance(manifest, RunManifest):
            raise TypeError(
                "results require a RunManifest — a number without its protocol is noise"
            )
        self.path = Path(path)
        self.manifest = manifest
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._fh.write(
            json.dumps({"kind": "manifest", **manifest.to_dict()}, ensure_ascii=False, default=str)
            + "\n"
        )
        self._rows = 0

    def write(self, row: ResultRow) -> None:
        self._fh.write(
            json.dumps({"kind": "result", **row.to_dict()}, ensure_ascii=False, default=str) + "\n"
        )
        self._rows += 1

    def write_summary(self, summary: RunSummary) -> None:
        self._fh.write(
            json.dumps({"kind": "summary", **summary.to_dict()}, ensure_ascii=False, default=str)
            + "\n"
        )

    @property
    def rows(self) -> int:
        return self._rows

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> ResultWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """Read back a results file as ``(manifest, rows, summary)``."""
    manifest: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            payload = json.loads(line)
            kind = payload.get("kind")
            if kind == "manifest":
                manifest = payload
            elif kind == "result":
                rows.append(payload)
            elif kind == "summary":
                summary = payload
    if manifest is None:
        raise ValueError(f"{path} has no manifest line — it is not a harness result file")
    return manifest, rows, summary


SCORE_MATRIX_COLUMNS = (
    "system",
    "dataset",
    "score",
    "n",
    "judge_model",
    "judge_scale",
    "backbone",
    "subset",
    "data_revision",
    "tokens_per_query",
    "evidence_class",
    "admissible_d16",
    "run_id",
    "note",
)


def append_score_matrix_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    """Append harness rows to a score-matrix CSV, creating the header if new."""
    path = Path(path)
    exists = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(SCORE_MATRIX_COLUMNS), extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
            written += 1
    return written
