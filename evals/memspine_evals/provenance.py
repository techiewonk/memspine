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

import os
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


_RUNTIME_PACKAGES = (
    "memspine",
    "torch",
    "transformers",
    "sentence-transformers",
    "httpx",
    "lancedb",
)
_SECRET_NAME = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as dist_version

        return dist_version(name)
    except (ImportError, PackageNotFoundError):
        return None


def _git_describe(path: Path) -> str:
    """``git describe --always --dirty`` of the repo containing ``path``, or ``unknown``."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "describe", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else "unknown"


def _probe_ollama(base_url: str, timeout: float = 2.0) -> dict[str, Any]:
    """Best effort ``/api/version`` and ``/api/ps`` of a local Ollama; never raises. Only a
    loopback host is asked, so a hosted endpoint never receives a stray request."""
    from urllib.parse import urlparse
    from urllib.request import urlopen

    parsed = urlparse(base_url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return {"skipped": f"not a loopback host: {parsed.hostname}"}
    root = f"{parsed.scheme}://{parsed.netloc}"
    out: dict[str, Any] = {}
    for key, path in (("version", "/api/version"), ("ps", "/api/ps")):
        try:
            import json

            with urlopen(root + path, timeout=timeout) as response:
                out[key] = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # unreachable, not Ollama, bad JSON: record, do not fail
            out[key] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return out


def capture_runtime(
    base_url: str | None = None,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """D4 [HAR-5]: the runtime block of a manifest. Never raises: a field it cannot read is
    recorded as ``None`` / ``"unknown"``. ``base_url`` set = also probe a loopback Ollama."""
    env = os.environ if environ is None else environ
    engine_file: str | None = None
    try:
        import memspine

        engine_file = getattr(memspine, "__file__", None)
    except Exception:  # the engine is not importable: record that
        engine_file = None
    runtime: dict[str, Any] = {
        "memspine_file": engine_file,
        "versions": {name: _package_version(name) for name in _RUNTIME_PACKAGES},
        "engine_git_describe": (
            _git_describe(Path(engine_file).resolve().parent) if engine_file else "unknown"
        ),
        "argv": list(sys.argv if argv is None else argv),
        "env": {
            k: ("<redacted>" if any(part in k.upper() for part in _SECRET_NAME) else v)
            for k, v in sorted(env.items())
            if k.startswith(("MEMSPINE_", "OLLAMA_"))
        },
    }
    if base_url:
        runtime["ollama"] = _probe_ollama(base_url)
    return runtime


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
    #: D4 [HAR-5]: what the process actually ran (``capture_runtime``): engine path and
    #: versions, argv, MEMSPINE_*/OLLAMA_* environment, the local Ollama's state. {} when
    #: not captured.
    runtime: Mapping[str, Any] = field(default_factory=dict)

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
        runtime: Mapping[str, Any] | None = None,
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
            runtime=dict(runtime or {}),
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
