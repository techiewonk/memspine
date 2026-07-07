"""Associative memory (M13.6/D-40): a rebuildable link graph over records.

Links are LINK events through the write door (ADR-015); the graph store is a
projection. PPR recall, bounded A-MEM evolution, and optional Leiden
communities (``[community]``) live here.
"""

from memspine.memories.associative.projector import GraphProjector
from memspine.memories.associative.store import AssociativeMemory

__all__ = ["AssociativeMemory", "GraphProjector"]
