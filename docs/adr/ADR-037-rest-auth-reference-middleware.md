# ADR-037: REST auth reference middleware (not a production auth plane)

- **Status:** accepted
- **Date:** 2026-10-05
- **Decision id:** master absorb list #51 (TS-12). Amends ADR-017 / ADR-018 (REST had no authentication).

## Context

The REST app trusts the `X-Memspine-Namespace` header verbatim (ADR-017): whoever can reach it can
read and write every namespace, and the `actor` of a write, forget or correction is whatever the
caller claims. ADR-018 kept authentication out of scope and told deployers to override the
`resolve_namespace` seam. In practice every deployment has to write the same glue, and the
engine-global routes (`/sleep`, `/rebuild`) and the new subject-access export (`/export`, #46) need a
role check that a namespace override alone cannot express. The read audit (#49) also needs an
authenticated principal to be worth anything.

## Decision

Ship a small reference middleware in `memspine.protocols.rest.auth`, selected by config:

- `rest.auth.mode`: `none` (default, byte-identical app behaviour), `api_key` or `oidc_jwt`.
- `api_key`: `rest.auth.api_keys` maps each key (read from an env var named by `key_env`, or `key`)
  to a principal, its allowed namespace globs and an admin flag. Only SHA-256 digests are kept and
  compared in constant time; keys are never logged or returned.
- `oidc_jwt`: bearer JWTs verified with PyJWT (`jwks_url` or a key from an env var; issuer, audience
  and algorithms configurable). PyJWT is optional: selecting the mode without it raises a
  `ConfigError` naming the package. Principal, namespaces and roles come from configurable claims.
- Every authenticated request is bound to its principal: the namespace header must match one of its
  globs (else 403), the principal becomes the `actor` of `/correct` and forget and of the
  `memory.read_audit` events (through a context variable, `principal_scope`), and the admin routes
  (`/sleep`, `/rebuild`, `/export`, `/quarantine…`) need the admin flag or role (else 403).
- `rest.rate_limit` adds an in-memory token bucket per principal (per client address without
  auth): 429 when empty.
- `create_app(engine, rest=...)` accepts an explicit `RestConfig`; without one it reads the started
  engine's `rest` block.

## Consequences

- Positive: a deployment gets namespace binding, an admin split and a trustworthy principal for the
  audit trail from config alone; the default app is unchanged.
- Negative / cost: one extra middleware hop per request (a pass-through in `mode: none`).
- Not provided, on purpose: key rotation and revocation lists, per-route scopes beyond the admin
  split, token introspection, mTLS, distributed rate limits, an admin console. This is a reference
  of where authentication plugs in, not a production auth plane; put a real gateway in front for
  that.
- Follow-up: the MCP protocol, when it lands, should reuse `Principal` and `principal_scope`.

## Alternatives rejected

- **Keep auth out of scope (ADR-018 as is)** — every deployer re-implements the same binding, and
  the read audit records unauthenticated claims.
- **Require an external gateway only** — cannot express the admin-route split or bind the principal
  inside the engine for auditing.
- **A hard dependency on PyJWT / an OIDC client** — the core stays slim (D-03); JWT support is
  activated only when the package is installed.
