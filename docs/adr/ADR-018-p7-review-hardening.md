# ADR-018 — P7 review hardening (security + robustness)

**Status:** accepted · **Date:** 2026-07-07 · **Register:** D-52

## Context

A 5-agent review of the P7 surface (python · security · silent-failure ·
test-coverage · blueprint) found that P7 shipped more tenant-isolation exposure
than the *documented* one (the no-authn REST stance of ADR-017 §1). Several
findings were **engine-level** holes that stand regardless of the REST layer —
a grantee who obtains a foreign `record_id` (legitimately, via `shared_search`)
could pivot it into a cross-tenant mutation or trace. This ADR records the four
security decisions plus the correctness/robustness closures, and sharpens the
REST deployment contract. No new DDL, no new `EventKind`.

## Decisions

### 1. The `shared` memory type is reserved from the public write path (SEC-H1)

`memory_type="shared"` is engine-internal bookkeeping: grant and subscription
records carry authorization state, and `parse_grant` validates only *shape*. A
public `write(namespace=grantor, memory_type="shared", entity=grantee,
attribute="grant", content='{"grant":…}')` would therefore forge a **live
grant**, bypassing `SharedMemory.grant()`'s scope validation, self-grant guard,
and idempotent supersession. `Engine.write` (and the REST `/write` route, which
delegates to it) now rejects `memory_type=="shared"` with
`ConflictError("memory_type 'shared' is engine-internal — use
grant()/subscribe()")`. The only writers of `shared` records are
`SharedMemory.grant`/`subscribe`, which build the record and go through
`_write_locked` directly — never the public door.

### 2. `forget` / `verify_forget` / `audit_taint` are namespace-scoped (SEC-C2/C3)

All three took a **global** `record_id` and reached across namespaces. They are
now scoped to the caller's namespace, using the ADR-014 **anti-oracle** error
shape (one message for missing and foreign — a leaked id must not become a
cross-namespace existence oracle), mirroring
`ProspectiveMemory._require_watch`:

- **`forget`** — a record that EXISTS in another namespace always raises and is
  **not touched** (this closes the IDOR: a read-only grantee cannot hard-delete
  the grantor's data). A **soft** forget of an absent id also raises. A **hard**
  forget of an absent id is left to proceed as an idempotent no-op — see the
  durability note below.
- **`verify_forget`** — gains a `namespace` param; a record still present in a
  foreign namespace raises. (Post-hard-delete the row is absent, which is the
  normal verify path and proceeds.)
- **`audit_taint`** — gains a `namespace` param (default `"default"`). The seed
  must belong to it, determined from the read model when present, else from the
  seed's **home namespace in the log** (`TaintReport.origin_namespace`, the
  namespace of the first event that mentions the seed) — so a since-merged or
  hard-deleted seed can still be audited by its owner but not by a stranger. The
  taint **walk** is unchanged (derivation edges are namespace-local post-P5/P6,
  so scoping the seed is sufficient — the walk is not weakened).

**Durability deviation (deliberate):** a **hard** `forget` of an *absent* id
proceeds rather than raising. A hard delete removes the read-model row during
projection, but its LOG redaction may fail mid-operation (e.g. disk hiccup);
the erasure MUST be completable by re-running `forget` after the row is already
gone, or the secret persists in the append-only log forever. The IDOR is still
fully closed because it always targets an *existing* foreign record, which
raises. Soft forget has no such requirement and raises on a missing id.

### 3. REST writes are untrusted external input (SEC-C1)

The REST `/write` route mapped a caller-supplied `source.role` straight into
trust (`role:"operator"` → 0.9 + firewall immunity) with no validation. At the
REST boundary **only** (SDK trust semantics unchanged), the route now forces the
write onto a new **`rest` channel** added to `TrustPolicy._EXTERNAL_CHANNELS`.
`trust_at_write` caps every external channel at `TRUST_RETRIEVED_CAP` regardless
of role, so a caller cannot claim `operator` to escalate trust or dodge the
firewall; instruction-shaped content over REST is still quarantined. The role is
preserved for provenance only. REST writes are treated as untrusted external
input — the same posture as an ingested PDF or a retrieved web page.

### 4. Ancillary hardening

- **SEC-M1** — taskiq log lines strip URL userinfo (`redis://user:pass@host` →
  `redis://***@host`) via `_redact` on the degraded + ack + recovery paths.
- **SEC-M3** — a REST body-size middleware rejects requests over
  `REST_MAX_BODY_BYTES` (default 1 MiB) with 413.
- **COR-1** — `due()` rejects a naive `now` (was a 500 deep in the trigger).
- **COR-2** — `shared_search` truncates the merged own+foreign set to `top_k`
  (the per-grantor loop appended up to `top_k` each).
- **COR-3** — reranker *construction* failures (OSError/RuntimeError/…) — not
  just ImportError — self-disable the E8 stage; search degrades, never crashes.
- **SF-1** — an invalidation (target) watch created, or swept, under
  `event_log.mode=ephemeral` warns once (it can never fire — ADR-016).
- **SF-2** — a degenerate rerank score collapse (>1 candidate, one value) logs
  once; behavior (neutral 0.5) unchanged.
- **SF-4** — the E5 llmlingua block-compress guards each record; one that raises
  is logged and left uncompressed instead of 500-ing `/assemble`.
- **SF-5** — the taskiq degraded latch resets after a healthy enqueue
  (once-per-outage, not once-ever); the enqueue except is narrowed
  (TypeError/AttributeError re-raise as real bugs); and **XAUTOCLAIM
  claim-recovery is wired into `run()`** (previously dead code) — the next run
  for a label reclaims + ACKs a crashed run's pending marker.
- **SF-6** — REST typed-error handlers (409/501/400) emit an M11 log line (never
  the body) before returning.
- **SF-7** — the `shared_search` per-grantor loop is wrapped: one grantor's
  broken vector index logs `shared_search.grantor_failed` and is skipped; the
  reader's own results and healthy grantors' results survive.
- **CMP-1** — `acknowledge_watch` refuses a **quarantined** watch (a held watch
  can never fire; matching `ProceduralMemory.promote`'s E1 stance).
- **CMP-2** — the fastembed reranker's `MissingServiceError` names the remedy
  (upgrade fastembed / use flashrank), since fastembed is a core dep.
- **CMP-3** — REST gains `POST /subscriptions`, `GET /subscriptions`, and
  `GET /grants` (grants_from) to mirror the full P7 verb set.

### 5. REST deployment contract (blast radius)

Recorded in ADR-017 §6 and the module docstring: `/sleep`, `/rebuild`, and
`/audit/taint` are engine-global or cross-cutting and **MUST sit behind an
internal-only network boundary**. REST ships with **NO authn**; the deployer
owns the caller→namespace binding (the `resolve_namespace` override) AND accepts
that role-based trust is neutralized at the boundary (§3). A body cap guards the
unbounded-body DoS path (§4/SEC-M3).

## Consequences

- No new DDL / EventKind; `TaintReport` gains one field (`origin_namespace`).
- `Engine.audit_taint`/`verify_forget` gain a `namespace` param (default
  `"default"`); the CLI `audit taint` / `forget --verify` thread it.
- `Engine.grants_from` is added (backs `GET /grants`).
- The `rest` channel joins `_EXTERNAL_CHANNELS`; existing SDK trust unchanged.

## Register row (D-52)

See the structure-plan register (D-52 row).

## Alternatives rejected

- Raising on a missing id for **hard** forget too — breaks the erasure-retry
  durability guarantee (a partial hard delete could never be completed, leaving
  the secret in the log). The IDOR is closed by the foreign-exists check.
- Adding REST authn to fix SEC-C1 — half an auth model is worse than a loud,
  documented seam (ADR-017); the external-channel cap neutralizes role
  escalation without one.
- Scoping the `audit_taint` **walk** by namespace — unnecessary and would break
  legitimate cross-derivation audits; the derivation edges are already
  namespace-local, so scoping the seed suffices.
- Weakening the anti-oracle to distinguish missing from foreign — that IS the
  oracle; the shared error shape is the point (ADR-014).
