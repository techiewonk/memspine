"""Lexical capability port (D-25): standalone Tantivy BM25 (core default),
OpenSearch/Elasticsearch seam ([opensearch]) for server scale.

The lexical index is a dedicated search index, never bolted onto the
transactional store — so hybrid retrieval works identically on any storage
backend. It is a rebuildable projection (D0.1) like the vector store: a
:class:`~memspine.services.lexical.projector.LexicalProjector` re-indexes record
content from WRITE events and drops it on FORGET — never a second source of
truth. RRF fusion of the vector + lexical legs is implemented once in
:mod:`memspine.services.lexical.base` (``rrf_fuse``).
"""
