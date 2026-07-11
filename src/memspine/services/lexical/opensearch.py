"""OpenSearch / Elasticsearch lexical store — server-scale BM25 seam (D-25).

The embedded default lexical leg is the core, standalone :class:`~memspine.
services.lexical.tantivy.TantivyLexical`. For large multi-node deployments the
BM25 index belongs in a dedicated search cluster (OpenSearch or Elasticsearch),
not on a single-box embedded index. That adapter — plus its ``clients/`` client
and index mappings — is out of scope for v0.2; this stub keeps the swap-in slot
explicit so ``read.lexical_provider: opensearch`` fails **actionably** (naming
the ``[opensearch]`` extra, D-10) instead of half-working.

Both OpenSearch and Elasticsearch expose the same BM25 query surface, so one
adapter (behind ``opensearch-py``, which also talks to ES) will satisfy the
:class:`~memspine.services.lexical.base.LexicalStore` port when built.
"""

from __future__ import annotations

from typing import Any

from memspine.exceptions import MissingServiceError

__all__ = ["OpenSearchLexical"]


class OpenSearchLexical:
    def __init__(self, _read_config: Any = None) -> None:
        raise MissingServiceError("lexical:opensearch", extra="opensearch")
