# ADR-014 — P5 lifecycle amendments (procedural, prompts, reflections)

**Status:** accepted · **Date:** 2026-07-07 · **Register:** D-48

## Context

P5 mapped three lifecycles onto existing machinery, and the 6-agent review pass
found each mapping silently reinterpreted a design-doc clause — the exact
pattern ADR-013 was written to stop. One of the three also hid a real
vulnerability (trust laundering through reflections). This ADR records the
decisions and the code they required.

## Decisions

### 1. Staged plans are held via `RESOLVING`, not the quarantine tier

E6's text says plans enter at `staged` "(reuses E1 quarantine)". Literal reuse
would violate ADR-013 §3: quarantine's only sanctioned writers are
`FirewallVerdict.apply` and corroboration. Instead, all pre-active ladder
stages (draft/staged/verified) ride `RecordStatus.RESOLVING` — the same
"held out of every retrieval surface" effect, without a second writer into the
quarantine mechanism. Exit is explicit promotion through the ladder
(`verified` + dry-run gate), never corroborative.

Corollary (review CRITICAL): when corroboration lifts an *actual* quarantine
on a procedural record, it restores the status its `skill_stage` implies
(`stage_status()` — RESOLVING pre-active), never blanket `ACTIVATED`; and
`same_fact` corroboration matching is scoped by `memory_type`, so a semantic
fact can never corroborate a same-named skill.

### 2. Prompt versions enter the ladder at `active` in v0.1

D-43 §4 says prompts "ride the ladder". A resolved prompt *is* the active
version by construction (overrides bump the version), and no promotion actor
exists for prompts yet — so `prompt_registry.py` records versions directly at
`active`. The full draft→…→active prompt lifecycle is deferred until a prompt
review/promotion workflow exists (candidate: P7+). `sync_prompt_versions` runs
under the per-namespace write lock like every other write verb; its firewall
exemption (deterministic, system-generated reference strings — never free
text) is the stated one, shared with `set_persona`.

### 3. Reflections carry caller trust and inherit least-member trust

Reflections are caller-authored free text, so (a) they default to
`role="assistant"` — never the privileged `"system"`, which short-circuited
every firewall check (review CRITICAL: instruction-shaped reflection content
was unquarantinable); (b) their trust is capped at `min(parent.trust)` after
the gate, mirroring consolidation summaries (D-47 §5); (c) parents must live
in the reflection's namespace — reflections never span tenants, and neither
does the taint graph they create. `Engine.reflect(actor=..., source=...)`
threads real authorship into provenance.

## Consequences

- Plan Part B E6 and Part E §4 texts amended to match.
- `promote`/`deprecate`/`reflect` verify record↔namespace ownership; the
  "not found" error is identical for missing and foreign records (no
  cross-namespace existence oracle).
- `SkillStage` moved to `core/records.py` so `MemoryRecord.skill_stage` is
  enum-typed; `reflection_depth` is bounded `le=REFLECTION_DEPTH_CAP` at the
  field. Illegal states fail at construction, not deep in a transition.
- `dry_run_passed` remains a caller attestation in v0.1 (no dry-run harness
  exists yet); the docstrings say so. A receipt-token upgrade is reserved for
  the phase that ships skill execution.

## Alternatives rejected

- Literal quarantine reuse for staged plans — breaks ADR-013 §3.
- Full prompt ladder in v0.1 — no promotion actor for prompts exists.
- Keeping `role="system"` reflections with a special-cased firewall — the
  privileged exemption is the point of the role; content authorship is not
  privileged.
