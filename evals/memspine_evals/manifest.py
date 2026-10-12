"""P0-03: run identity and the comparability key (ER-35).

A paired test is only meaningful between two runs that differ in the thing under test and in nothing else. The
``identity`` block of a ``RunManifest`` records everything that could differ:

* ``commit``, ``env_hash``, ``venv_hash``: the code and the environment (``pinned_run.sh`` exports them);
* ``flags`` and ``flags_hash``: the effective config of the arm (this is what a paired test varies);
* ``prompts``: ``{template name: content hash}`` for every prompt the run used;
* ``models``: reader, judge, embedder, reranker, worker model ids;
* ``datasets``: dataset id, slice id, slice hash and ``{file: sha256}`` for every data file read;
* ``harness_version``.

``comparability_key`` hashes only the fields two arms must share. The arm's own flags, commit and env are left out
on purpose: they are what is being compared. ``compare_manifests`` names every field that differs, so a gate can say
*why* two runs are incomparable.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import __version__

MODEL_ROLES = ("reader", "judge", "embedder", "reranker", "worker")

#: Dotted paths into a manifest dict (``RunManifest.to_dict()`` or its JSON) that must be equal for a paired test.
#: A path under ``identity.`` is preferred; the others are the fallbacks every manifest already carries.
COMPARABILITY_FIELDS: tuple[str, ...] = (
    "dataset.dataset_id",
    "dataset.revision_id",
    "dataset.content_sha256",
    "identity.datasets.slice_id",
    "identity.datasets.slice_hash",
    "judge.model",
    "judge.prompt_hash",
    "reader.model",
    "harness_version",
    "judge.scale",  # scoring: the scale and rule the correctness bit came from
    "judge.judge_id",
    "protocol.budget_tokens",  # D16 required field; top_k and seed are arm knobs and stay out
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_flags(flags: Mapping[str, Any]) -> str:
    """Order-independent hash of the effective flag dict (16 hex chars, like the env hash)."""
    blob = json.dumps(dict(flags), sort_keys=True, default=str, ensure_ascii=False)
    return sha256_bytes(blob.encode("utf-8"))[:16]


def prompt_identity(name: str, text: str) -> dict[str, str]:
    """``{"name": ..., "sha256": ...}`` of one prompt template's content."""
    return {"name": name, "sha256": sha256_bytes(text.encode("utf-8"))}


def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); ``None`` for no values."""
    xs = sorted(values)
    if not xs:
        return None
    return xs[max(0, min(len(xs) - 1, -(-len(xs) * q // 100) - 1))]


def build_cost(
    *,
    tokens: Mapping[str, Mapping[str, int | None]] | None = None,
    embedding_calls: int | None = None,
    rerank_calls: int | None = None,
    duration_s: float | None = None,
    question_latencies_s: Iterable[float] | None = None,
) -> dict[str, Any]:
    """G-25 informational cost and latency block. NOT part of the comparability key. Everything optional:
    a missing figure is ``None``, never an error. ``tokens`` is ``{role: {"prompt": n, "completion": n}}``."""
    toks: dict[str, dict[str, int | None]] = {}
    for role, d in (tokens or {}).items():
        toks[role] = {"prompt": (d or {}).get("prompt"), "completion": (d or {}).get("completion")}
    lat = list(question_latencies_s) if question_latencies_s is not None else []
    return {
        "tokens": toks,
        "embedding_calls": embedding_calls,
        "rerank_calls": rerank_calls,
        "duration_s": duration_s,
        "latency_p50_s": percentile(lat, 50),
        "latency_p95_s": percentile(lat, 95),
    }


def build_identity(
    *,
    flags: Mapping[str, Any] | None = None,
    prompts: Mapping[str, str] | None = None,
    models: Mapping[str, str | None] | None = None,
    dataset_id: str | None = None,
    slice_id: str | None = None,
    slice_ids: Iterable[str] | None = None,
    data_files: Iterable[str | Path] = (),
    environ: Mapping[str, str] | None = None,
    cost: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``identity`` block. ``cost`` (``build_cost``) is informational and excluded from the comparability key. ``prompts`` maps template name to its text (hashed here). ``slice_ids`` (the question or
    item ids of the slice) gives ``slice_hash``; ``data_files`` are hashed by content. Commit, env and venv hashes come
    from the ``MEMSPINE_*`` variables ``pinned_run.sh`` exports, or ``None`` when the run was not pinned."""
    env = os.environ if environ is None else environ
    flag_dict = dict(flags or {})
    unknown = sorted(set(models or {}) - set(MODEL_ROLES))
    if unknown:
        raise ValueError(f"unknown model roles {unknown}; expected a subset of {MODEL_ROLES}")
    files = {str(p): sha256_file(p) for p in data_files}
    slice_hash = (
        sha256_bytes("\n".join(sorted(set(slice_ids))).encode("utf-8"))[:16] if slice_ids is not None else None
    )
    return {
        "commit": env.get("MEMSPINE_PINNED_COMMIT"),
        "env_hash": env.get("MEMSPINE_ENV_HASH"),
        "venv_hash": env.get("MEMSPINE_VENV_HASH"),
        "flags": flag_dict,
        "flags_hash": hash_flags(flag_dict),
        "prompts": {n: prompt_identity(n, t) for n, t in sorted((prompts or {}).items())},
        "models": {role: (models or {}).get(role) for role in MODEL_ROLES},
        "datasets": {
            "dataset_id": dataset_id,
            "slice_id": slice_id,
            "slice_hash": slice_hash,
            "files": dict(sorted(files.items())),
        },
        "harness_version": __version__,
        "cost": dict(cost) if cost is not None else build_cost(),
    }


def _get(d: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def comparability_fields(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The values of ``COMPARABILITY_FIELDS`` in a manifest dict (``None`` where absent)."""
    return {f: _get(manifest, f) for f in COMPARABILITY_FIELDS}


def comparability_key(manifest: Mapping[str, Any]) -> str:
    """Hash of the fields two arms must share. Equal keys = comparable in a paired test."""
    blob = json.dumps(comparability_fields(manifest), sort_keys=True, default=str, ensure_ascii=False)
    return sha256_bytes(blob.encode("utf-8"))[:16]


def compare_manifests(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """Names (with both values) of the comparability fields on which ``a`` and ``b`` differ; empty = comparable."""
    fa, fb = comparability_fields(a), comparability_fields(b)
    return [f"{k}: {fa[k]!r} != {fb[k]!r}" for k in COMPARABILITY_FIELDS if fa[k] != fb[k]]
