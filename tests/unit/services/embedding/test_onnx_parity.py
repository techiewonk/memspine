"""Replay fastembed 0.9.0 references; entirely offline, skip uncached exports.

The committed reference vectors, file hashes and pinned revisions are reviewed
numerical fixtures. Tests never regenerate expected vectors or import the
reference implementation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from huggingface_hub import try_to_load_from_cache

from memspine.clients.weights.onnx import OnnxModel
from memspine.clients.weights.onnx_catalogue import CATALOGUE

DATA = Path(__file__).parent / "data"


@pytest.mark.parametrize("name", list(CATALOGUE))
def test_pinned_export_matches_fastembed_reference(name: str) -> None:
    spec = CATALOGUE[name]
    reference = json.loads((DATA / (name.replace("/", "__") + ".json")).read_text(encoding="utf-8"))
    assert reference["revision"] == spec.revision
    assert spec.certified
    for filename, digest in reference["files_sha256"].items():
        cached = try_to_load_from_cache(spec.repo, filename, revision=spec.revision)
        if not isinstance(cached, str):
            pytest.skip(f"Pinned export not cached: {spec.repo}/{filename}")
        assert hashlib.sha256(Path(cached).read_bytes()).hexdigest() == digest
    model = OnnxModel(spec, threads=1, local_files_only=True)
    if spec.pooling == "score":
        actual = np.array(list(model.rerank(reference["query"], reference["documents"])))
        expected = np.array(reference["scores"])
        assert np.max(np.abs(actual - expected)) <= 1e-4
        assert np.array_equal(np.argsort(actual), np.argsort(expected))
    else:
        actual = np.array(list(model.embed(reference["texts"])))
        expected = np.array(reference["vectors"])
        assert np.max(np.abs(actual - expected)) <= 1e-5
        cosine = (actual * expected).sum(axis=1) / (
            np.linalg.norm(actual, axis=1) * np.linalg.norm(expected, axis=1)
        )
        assert np.all(cosine >= 0.99999)
