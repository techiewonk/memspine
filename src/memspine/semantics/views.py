"""Read views over stored claims (RM-46): reverse direction without a second fact.

A symmetric or inverse-view predicate is stored once, in the asserted
direction. :func:`edge_views` renders the reverse direction for reading and
traversal; the reverse view carries the *same* claim id and evidence ids,
``asserted=False``, and is never written back, counted as extra evidence or
treated as an independent source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from memspine.semantics.specs import PredicateSpec


class StoredEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    claim_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str
    object: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)


class EdgeView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    claim_id: str
    subject: str
    label: str
    object: str
    direction: str
    asserted: bool
    evidence_ids: tuple[str, ...]


def edge_views(
    predicates: Mapping[str, PredicateSpec], edges: Iterable[StoredEdge], *, focus: str
) -> tuple[EdgeView, ...]:
    """Edges seen from ``focus``: asserted ones, plus reverse views where defined."""
    views: list[EdgeView] = []
    for edge in edges:
        spec = predicates[edge.predicate]
        if edge.subject == focus:
            views.append(
                EdgeView(
                    claim_id=edge.claim_id,
                    subject=edge.subject,
                    label=edge.predicate,
                    object=edge.object,
                    direction="forward",
                    asserted=True,
                    evidence_ids=edge.evidence_ids,
                )
            )
        elif edge.object == focus and (spec.symmetric or spec.inverse_view):
            views.append(
                EdgeView(
                    claim_id=edge.claim_id,
                    subject=edge.object,
                    label=edge.predicate if spec.symmetric else str(spec.inverse_view),
                    object=edge.subject,
                    direction="reverse",
                    asserted=False,
                    evidence_ids=edge.evidence_ids,
                )
            )
    return tuple(views)
