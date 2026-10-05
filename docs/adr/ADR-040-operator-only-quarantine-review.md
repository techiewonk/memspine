# ADR-040: Quarantine review is operator-only, and an author cannot release its own write

- **Status:** accepted (Wave 1 trust review fixes, 2026-10-05)
- **Date:** 2026-10-05
- **Decision id:** amends #3 (quarantine review) and ADR-018 §5 (REST deployment contract).

## Context

`POST /quarantine/{id}/approve` and `/reject` were tenant-scoped routes. The namespace header chose
the tenant, and the request body named the actor. A tenant whose write was held by the firewall
could therefore release it itself, which defeats the hold. The engine verb had the same gap: it did
not compare the reviewer with the record's author.

## Decision

- **REST seam.** The REST app gains an operator seam, `resolve_operator`, alongside
  `resolve_namespace`. By default it refuses every request with 403. Deployers override it with
  their auth layer's operator identity. That identity becomes the decision's actor, and for approval
  also its principal. The `actor` field in the body is ignored.
- **Engine check.** `Engine.approve_quarantined` takes `principal`. It raises `ConflictError` when
  the reviewer's principal or actor equals the held record's `source.principal`.
- **Unchanged.** `/sleep`, `/rebuild` and `/audit/taint` keep the network-boundary contract of
  ADR-018. This ADR changes only the review routes.

## Consequences

- **Behaviour change.** The REST review routes return 403 until a deployer fills the seam. SDK
  callers are unaffected unless they pass the author's identity as the reviewer.
- **Limit.** A held record without `source.principal` cannot be checked for self-review. The
  operator seam is the guard in that case.
