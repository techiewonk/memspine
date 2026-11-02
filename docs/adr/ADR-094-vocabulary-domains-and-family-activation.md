# ADR-094: Per-family vocabulary domains and fixture-gated activation

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-118

## Context

Built-in packs declare no top domain, so `graph_filter` by domain cannot match built-in data
(RM-107). Proposed families D, E, F, H, I, J, L, M and N are registered but inactive
(RM-62, RM-63).

## Decision

- Each built-in family declares one domain from a fixed generic set: `personal`, `work`,
  `health`, `finance`, `travel`, `learning`, `social`, `tools`, `other`. Packs carry none.
  No benchmark-specific names.
- A proposed family moves into the active pack only when its executable fixtures (positive,
  negative, known-time, replay, erasure) pass. Until then it stays opt-in via
  `memspine.proposed`.
- RM-66 closes as not applicable in the first cycle. The alias audit script stays as an
  optional diagnostic.

## Amendment 2026-11-02 (owner decisions)

- `owns` accepts species objects (pets and animals can be owned). Family J activates once its
  fixtures pass.
- Family M carries `participation_mode` as a claim qualifier through the canonical fact door;
  it activates once the edge compiler fills it.
- The judgement-call domains stand as built: commitments to work, documents to learning,
  instructions to tools, and `colleague_of` and `occupation` in the work-status family.
- Claim role bindings (obligor, beneficiary, participant and the like) compile into qualified
  graph edges, so graph search and `graph_filter` can reach them. Claim qualifiers and
  `statement_kind` pass through to claims and edges.

### Implementation (2026-11-02)

- `owns` lists `species`; families J and M are in `memspine.core` (v5). `memspine.proposed`
  (v4) is empty.
- `transport_mode` has a closed generic vocabulary (`memspine.semantics.modes`). Both modes are
  typed edge qualifiers with `graph_filter` fields (`participation_modes`, `transport_modes`).
- The canonical fact door validates candidate qualifiers and stores them on the claim
  (`SemanticEnvelope.qualifiers`), with the predicate's `statement_kind`. Edges carry
  `statement_kind` (claim, else registry) and `claim_qualifiers`.
- Each entity role binding becomes a role edge `role:<role>` from the claim's assertion node to
  the filler's entity node. The entity index links role fillers at write time. The edge carries
  the claim's qualifiers with `event_role` set to the role name. A filler that is not an
  entity, that the entity policy rejects or that has no entity link gets no edge; it is
  reported with its reason (`CompiledEdges.role_skips`).
