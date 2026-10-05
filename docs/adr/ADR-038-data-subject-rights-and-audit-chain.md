# ADR-038: Data-subject verbs, retention classes, purpose gate and a chained audit trail

- **Status:** accepted
- **Date:** 2026-10-05
- **Decision id:** master absorb list #46-#50 (TS-5, TS-13, TS-6, TS-11, TS-7). Builds on M7 erasure (ADR-014, #43).

## Context

Erasure (M7) was the only data-subject right the engine served. Access and portability (GDPR
Art. 15/20), rectification (Art. 16), storage limitation (Art. 5(1)(e)), purpose limitation and
the EU AI Act's logging duty (Art. 12) each need an engine verb or a log record, not deployer glue.
One concrete defect motivated #47: with `contest_lower_trust` on (the `base` template), a REST
correction of a fact arrives on the low-trust `rest` channel and is CONTESTed instead of applied.

## Decision

All opt-in; `MemspineConfig()` and every template behave as before.

- **Export** (`Engine.export`, CLI `memspine export`, REST `GET /export`): deterministic JSONL of a
  namespace's live, archived and held records (soft-forgotten left out), optionally narrowed to a
  subject (as `erase_subject` defines it, plus derived records) and optionally with its log events as
  stored; the content of soft-forgotten records is scrubbed from exported events.
- **Correct** (`Engine.correct`, REST `POST /correct`): user-direct supersession by record id or fact
  key. It archives the target with `evolve_to` and writes the new value without running the conflict
  ladder, so a correction is never contested or rejected for lower trust. The firewall still screens
  the value; the WRITE events carry `correction: {supersedes, actor, reason}`.
- **Retention classes** (`retention.classes`): `(namespace glob, memory_type) -> ttl_days`, first
  match wins. A `retention_expire` stage runs first in the sleep cycle, only when classes exist, and
  hard-forgets expired records through `Engine.forget` (cascade included). The per-type `retention`
  policy's `may_delete` (legal hold, regulated PII) is checked for the record and its descendants.
- **Audit** (`audit.reads`, `audit.actions`): new event kinds `memory.read_audit` (principal,
  namespace, returned ids, purpose, time; once per outermost read verb) and `memory.audit` (forget,
  correct, expiry, export with actor and reason). Both carry a SHA-256 hash chain in the payload
  (`chain.prev`, `chain.hash`), serialized by an engine lock and resumed from the log on restart;
  `verify_audit_chain()` / `audit_chain_ok()` validate it. No projector reads either kind, so
  projections and replay are unchanged. The FORGET event's own `actor` is left as is (the erasure
  path is owned elsewhere); the chained audit event carries the real actor and reason.
- **Purpose gate** (`consent.enforce`): a record's `consent_tags` are its purposes (`write(...,
  purposes=)`, `*` = any); reads take `purpose=`. Enforced in the search gates and on every returned
  list or assembled context. **Remote-LLM gate** (`consent.remote_llm_max_tier`): roles bound to a
  remote provider are wrapped so the text of records above the tier is replaced before each call.

## Consequences

- Positive: each right is one verb or one config key; the audit trail is tamper-evident without an
  external anchor for edits, inserts and removals inside the chain.
- Negative / cost: read audit adds one event (and one log append) per read; the remote-LLM gate
  scans the high-tier records on each remote call. Both are off by default.
- Limits: the chain is per database; two engines writing one file concurrently fork it (verify
  reports the break). Truncating the newest audit events is only detectable against an externally
  stored head (`report.head`). The remote-LLM gate is textual: a paraphrase of high-tier content is
  not caught, so derived records should carry the tier. A synthetic read header (timeline, lead) is
  untagged and passes the purpose gate as untagged content.

## Alternatives rejected

- **Purpose as a new record field** — `consent_tags` already exists, is unioned on dedup merge and
  needs no schema change.
- **Route corrections through the ladder with a privileged channel** — would let a REST caller claim
  privilege; an explicit verb with an actor and reason is auditable instead.
- **Chain every log event (as `verify_integrity` does offline)** — hard erasure rewrites payloads
  by design and would break a persisted chain; audit events never carry record content, so they are
  never redacted.
- **Per-call-site LLM gating** — every prompt builder would need to know about tiers; one wrapper at
  the provider boundary covers them all.
