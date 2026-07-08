"""Lexical capability port (D-25): SQLite FTS5/BM25 core default, Tantivy [tantivy].

The lexical index is a rebuildable projection (D0.1) like the vector store: a
:class:`~memspine.services.lexical.projector.LexicalProjector` re-indexes record
content from WRITE events and drops it on FORGET — never a second source of
truth. RRF fusion of the vector + lexical legs is implemented once in
:mod:`memspine.services.lexical.base` (``rrf_fuse``).
"""
