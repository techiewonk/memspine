"""Tantivy lexical adapter stub — the standalone BM25 index for non-Lance
configs (D-25), behind the ``[tantivy]`` extra.

STUBBED in v0.1 (chosen over a real impl): tantivy-py is synchronous with its
own index-directory, writer-lock and commit lifecycle, none of which is
exercised in the gate (the ``[tantivy]`` extra is not a dev dependency, so a
real adapter would be untestable here) — and the zero-dep SQLite FTS5 store
already satisfies the D-25 core-default lexical leg. When a non-Lance
deployment needs standalone Tantivy, the real adapter replaces this stub and
the extra gains its requirement; the config surface is reserved now. Mirrors
the graph stubs (:class:`~memspine.services.graph.ladybug.LadybugGraphStore`).
"""

from __future__ import annotations

from memspine.exceptions import MissingServiceError

__all__ = ["TantivyLexical"]


class TantivyLexical:
    def __init__(self) -> None:
        raise MissingServiceError("lexical:tantivy", extra="tantivy")
