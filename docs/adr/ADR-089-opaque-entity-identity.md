# ADR-089: Opaque entity identity with evidence-based linking

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-113

## Context

Canonical entity nodes are keyed by name (`ent:NS:alex`), so two different people with the
same name merge on asserted edges (DS-10). Mention surface nodes reuse the `ent:` prefix
(RM-127), so any scan keyed on the prefix can misread them as entities.

## Decision

- Canonical entities get opaque ids `ent:NS:<uuid7>` with a maintained name/alias index
  carrying type and status (`confirmed`, `provisional`, `contested`, `pending_merge`).
- Surface nodes move to `surface:NS:LABEL:SURFACE`. Mention occurrences keep `mention:`.
- **Auto-link rule.** A new mention links to an existing entity automatically only when,
  in the same namespace, exactly one entity matches by exact name or alias with a compatible
  type (`basis=unique_name`). With two or more candidates, or a type clash, the mention
  starts or joins a provisional entity, with the candidates recorded. Retrieval similarity
  only proposes (D-100).
- Merges need cited evidence and are undone on revocation or erasure of that evidence
  (RM-55). Grantee-scoped merges stay invisible to the grantor (F13).
- Fresh schema, no alias for the old name-keyed ids.

## Consequences

Same-name people stay distinct unless evidence joins them. Entity seeding and lookup go
through the name index (RM-110 cache) instead of parsing node ids.

## Amendment 2026-11-01 (direction review)

- The entity id is minted once, inside the committed effect payload, and recorded in the
  event log. Projectors and replay never mint ids. A replay-equality test rebuilds the
  graph and the index and compares entity ids.
- A `unique_name` link has status `provisional` until cited identity evidence (an alias,
  coreference or an explicit merge) confirms it. This partially supersedes ADR-078 E1 and
  contract C03 for exact-name links within one namespace.
- Uniqueness is checked against the complete name index at the source's known sequence.
  A capped, truncated or deferred lookup never auto-links.
- The name index is a rebuildable projection in `storage.records`, built from entity,
  alias, link and merge events. It extends the RM-110 entity cache and is never a second
  source of truth.
- A compatible type means the same entity type, or either side untyped. Anything else is a
  clash, which gives a new provisional entity.
- Auto-link never crosses namespaces. Grantee merges (RM-55, F13) keep their visibility
  rules.
