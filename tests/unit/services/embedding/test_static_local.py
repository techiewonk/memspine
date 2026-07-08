"""E4 model2vec StaticEmbedder (ADR-020): a missing [static] extra fails fast
with the D-10 fix-hint; the real path (extra installed) embeds deterministically.
"""

from __future__ import annotations

import importlib.util

import pytest

from memspine.exceptions import MissingServiceError
from memspine.services.embedding.static_local import StaticEmbedder

_HAS_MODEL2VEC = importlib.util.find_spec("model2vec") is not None


@pytest.mark.skipif(_HAS_MODEL2VEC, reason="[static] installed — the miss path cannot be exercised")
def test_missing_extra_raises_missing_service_error() -> None:
    with pytest.raises(MissingServiceError) as excinfo:
        StaticEmbedder()
    assert excinfo.value.extra == "static"  # names the pip install to run


@pytest.mark.skipif(not _HAS_MODEL2VEC, reason="[static] not installed")
async def test_static_embed_is_normalized_and_stable() -> None:
    embedder = StaticEmbedder()
    [a1] = await embedder.embed(["the sky is blue"])
    [a2] = await embedder.embed(["the sky is blue"])
    assert a1 == a2  # deterministic lookup table
    assert abs(sum(x * x for x in a1) - 1.0) < 1e-5  # L2-normalized
    assert embedder.embedder_id.startswith("static:")
    assert embedder.dim > 0
