"""The run manifest: a score that can never lose its protocol (A4-2).

The finding that motivates this module is in
``paper_remembering_you/research/2026-09-16/SOTA_TOP20.md``: of the twenty
highest reported scores in the field, **twenty-one rows were inadmissible and
zero were fully admissible** under D16 — not one published number carries a
complete protocol. Eight of twenty were computed by someone other than the
system's authors. Four judge scales share one column header.

The harness's answer is structural rather than procedural. A result file is
written by ``ResultWriter``, which cannot be constructed without a
``RunManifest``; the manifest cannot be built without a dataset revision, a
judge scale, a reader identity and a budget. Producing an unprovenanced number
is not discouraged here, it is unreachable.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import HARNESS_ID, __version__
from .contracts import DatasetInfo, sha256_mapping
from .judge import JudgeSpec

# Fields D16 requires before a score may enter ``_shared/score_matrix.csv``.
D16_REQUIRED = (
    "dataset.dataset_id",
    "dataset.revision_id",
    "dataset.subset",
    "judge.scale",
    "reader.model",
    "protocol.budget_tokens",
    "system.system_id",
)


@dataclass(frozen=True, slots=True)
class ReaderSpec:
    """The fixed backbone ($\\mathcal{G}$). One per run.

    Mastra's own numbers are the reason this is not optional: 84.23 on
    ``gpt-4o`` against 94.87 on ``gpt-5-mini`` — one harness, one judge, one
    dataset, a 10.64-point swing from the backbone alone.
    """

    reader_id: str
    model: str
    makes_model_calls: bool
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SystemSpec:
    system_id: str
    version: str = "unknown"
    config: Mapping[str, Any] = field(default_factory=dict)

    @property
    def config_hash(self) -> str:
        return sha256_mapping(dict(self.config))


@dataclass(frozen=True, slots=True)
class RunProtocol:
    """The comparability layer, copied deliberately from LongMemEval-V2:
    sequential insert, a declared context budget, one fixed reader, fixed seed."""

    protocol_id: str
    budget_tokens: int
    top_k: int
    seed: int
    recall_ks: tuple[int, ...] = (1, 5, 10)
    notes: str = ""

    def __post_init__(self) -> None:
        if self.budget_tokens <= 0:
            raise ValueError("a run without a declared token budget is not comparable to any other")
        if self.top_k <= 0:
            raise ValueError("top_k must be positive")


def git_sha(repo: Path) -> str:
    """Short SHA of ``repo``, or ``"unknown"``. Never raises: provenance capture
    must not be able to fail a run, only to record that it could not."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    if out.returncode != 0 or not sha:
        return "unknown"
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return f"{sha}-dirty" if dirty.stdout.strip() else sha


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Everything needed to say what a number means, and to run it again."""

    run_id: str
    dataset: DatasetInfo
    system: SystemSpec
    reader: ReaderSpec
    judge: JudgeSpec
    protocol: RunProtocol
    token_counter: Mapping[str, Any]
    code: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    harness: str = HARNESS_ID
    harness_version: str = __version__
    labels: Mapping[str, Any] = field(default_factory=dict)
    #: R3-11: the caps a run was started under (items, queries per item, model calls,
    #: offline flag, call-budget scope). A capped pilot is not a full run.
    limits: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def build(
        run_id: str,
        dataset: DatasetInfo,
        system: SystemSpec,
        reader: ReaderSpec,
        judge: JudgeSpec,
        protocol: RunProtocol,
        token_counter: Mapping[str, Any],
        repo: Path | None = None,
        labels: Mapping[str, Any] | None = None,
        limits: Mapping[str, Any] | None = None,
    ) -> RunManifest:
        repo = repo or Path(__file__).resolve().parents[2]
        return RunManifest(
            run_id=run_id,
            dataset=dataset,
            system=system,
            reader=reader,
            judge=judge,
            protocol=protocol,
            token_counter=dict(token_counter),
            code={"memspine_git": git_sha(repo), "evals_version": __version__},
            env={
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            labels=dict(labels or {}),
            limits=dict(limits or {}),
        )

    # -- D16 admissibility ---------------------------------------------------

    def missing_protocol_fields(self) -> tuple[str, ...]:
        """Which of ``D16_REQUIRED`` this run cannot supply.

        Empty means the run's number may enter the score matrix as admissible.
        Non-empty is not a failure — it is the honest label that twenty-one of
        the field's top twenty published scores should have carried.
        """
        present: dict[str, Any] = {
            "dataset.dataset_id": self.dataset.dataset_id,
            "dataset.revision_id": self.dataset.revision_id,
            "dataset.subset": self.dataset.subset,
            "judge.scale": self.judge.scale.value if self.judge.scale else None,
            # A run with no generation ("none") has no backbone, so it cannot
            # carry an answer metric — the retrieval-only case is marked
            # inadmissible here rather than quietly passing as a QA number.
            "reader.model": None if self.reader.model in ("", "none") else self.reader.model,
            "protocol.budget_tokens": self.protocol.budget_tokens,
            "system.system_id": self.system.system_id,
        }
        missing = [k for k in D16_REQUIRED if not present.get(k)]
        # An LLM judge additionally owes its model and prompt hash; JudgeSpec
        # enforces that at construction, so reaching here means it is present.
        if self.judge.makes_model_calls and not self.judge.prompt_hash:
            missing.append("judge.prompt_hash")
        return tuple(missing)

    @property
    def admissible_d16(self) -> bool:
        return not self.missing_protocol_fields()

    @property
    def makes_model_calls(self) -> bool:
        return self.reader.makes_model_calls or self.judge.makes_model_calls

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["dataset"] = asdict(self.dataset)
        payload["judge"] = {**asdict(self.judge), "scale": self.judge.scale.value}
        payload["system"] = {**asdict(self.system), "config_hash": self.system.config_hash}
        payload["admissible_d16"] = self.admissible_d16
        payload["missing_protocol_fields"] = list(self.missing_protocol_fields())
        payload["makes_model_calls"] = self.makes_model_calls
        return payload

    def score_matrix_row(self, score: float, n: int) -> dict[str, Any]:
        """One row in ``_shared/score_matrix.csv``'s schema (A3-2).

        Columns match the file the survey already carries, so a harness run and
        a literature-harvested row are directly comparable — including in the
        ``admissible_d16`` column, where the harness's own runs must meet the
        same bar they judge everyone else by.
        """
        return {
            "system": self.system.system_id,
            "dataset": self.dataset.dataset_id,
            "score": round(score, 4),
            "n": n,
            "judge_model": self.judge.model or "none",
            "judge_scale": self.judge.scale.value,
            "backbone": self.reader.model,
            "subset": self.dataset.subset,
            "data_revision": self.dataset.revision_id,
            "tokens_per_query": self.protocol.budget_tokens,
            "evidence_class": "harness-run",
            "admissible_d16": "yes" if self.admissible_d16 else "no",
            "run_id": self.run_id,
            "note": "; ".join(self.missing_protocol_fields()),
        }


def format_missing(fields: Sequence[str]) -> str:
    return "none" if not fields else ", ".join(fields)
