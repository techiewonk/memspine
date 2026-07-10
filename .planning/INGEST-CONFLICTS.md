## Conflict Detection Report

Mode: new (fresh bootstrap, no existing .planning/ artifacts to reconcile).
Precedence: ADR > SPEC > PRD > DOC. 36 classifications consumed
(25 ADR / 11 DOC / 0 PRD / 0 SPEC). All 25 ADRs are LOCKED (accepted).

### BLOCKERS (0)

No blockers. No unreconciled LOCKED-vs-LOCKED contradiction was found: every
apparent contradiction between locked ADRs is a **documented supersession/
amendment** carried inline in the source ADRs themselves (see INFO). No
UNKNOWN/low-confidence docs. No unresolvable reference cycle (see INFO on cycle
detection). No existing locked CONTEXT.md to violate (mode = new).

### WARNINGS (0)

No warnings. Zero PRDs means there are no competing acceptance variants to
resolve. Requirements were derived from authoritative DOCs (see INFO); the
downstream roadmapper should be aware they are DOC-precedence, not PRD-authoritative.

### INFO (6)

[INFO] No PRD or SPEC documents in the ingest set
  Found: 25 ADR + 11 DOC classifications; 0 PRD, 0 SPEC.
  Note: requirements.md and constraints.md are DOC/ADR-derived. Requirements come
  from the structure-plan phase plan (P0–P7) + enhancement program (E1–E9) +
  FEATURES.md, at DOC precedence. Constraints' binding invariants come from LOCKED
  ADRs; scope guards from the structure-plan non-goals. The roadmapper should treat
  phase requirements as capability requirements, not formal PRD acceptance contracts.

[INFO] Cycle detection ran — 3 mutual-reference clusters, all benign
  Found: the ADR cross-ref graph contains three reciprocal "see-also / amends /
  hardening" clusters: {ADR-001 ↔ ADR-011}, {ADR-020 ↔ ADR-021}, and the strongly
  connected component {ADR-017, ADR-018, ADR-019}.
  Note: these are documentation relationship links (amends / hardens / see-also),
  not content-resolution dependencies. Each ADR contains a self-contained decision
  and is independently synthesizable, so no synthesis loop occurs. Traversal stayed
  well under the depth-50 cap. Not treated as blockers; all clusters synthesized.

[INFO] Auto-resolved: vector store evolution (ADR-003 → ADR-020/ADR-021)
  Found: ADR-003 originally allowed a SQLite brute-force vector fallback + `[lance]`
  extra alongside LanceDB. ADR-021 removes the SQLite brute-force store and makes
  LanceDB the sole core vector backend; ADR-020 makes E4 rescore LanceDB-native.
  Note: reconciled inline — ADR-003 §Decision + §Amendment already record
  "LanceDB in core as sole vector store (ADR-021, amends D-09)". Current ruling:
  LanceDB-only, LanceDB-native rescore. No contradiction remains.

[INFO] Auto-resolved: graph default evolution (ADR-003 → ADR-015)
  Found: ADR-003 named LadybugDB the default embedded graph (D-26); ADR-015 §5 sets
  the default graph provider to `sqlite_adjacency` (D-26 amended; ladybug/kuzu
  opt-in).
  Note: reconciled inline in ADR-015 §5 (with the 2026-07-09 update keeping the
  config default at sqlite_adjacency pending a future ADR). Later locked ADR wins by
  the project's documented amendment chain — not a filename/timestamp tiebreak.
  Current ruling: sqlite_adjacency default; ladybug `[graph]` + kuzu `[kuzu]` opt-in.

[INFO] Auto-resolved: LLM gateway evolution (ADR-012 / openai_compat → ADR-024)
  Found: ADR-012 added httpx to core for a hand-rolled OpenAI-compatible LLM path;
  ADR-024 adopts litellm as the unified core (lazy-import) LLM/embedding/rerank
  gateway and removes the openai_compat path.
  Note: httpx stays core (ADR-012 not contradicted on that point); the openai_compat
  *adapter* is superseded by litellm. Also supersedes the structure-plan §7 non-goal
  "litellm as a core dependency (D-33 — adapter only)". Current ruling: litellm core.

[INFO] ADR-000 is a template, not a decision
  Found: docs/adr/ADR-000-template.md lives on the ADR path but is empty boilerplate
  (placeholder title + status).
  Note: classified DOC, excluded from decision extraction. No action needed.
