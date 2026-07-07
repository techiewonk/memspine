# memspine — Novelty catalog (research-paper backbone)

Running inventory of the contributions a memspine paper can claim, each with the
claim, the evidence base, and where it lives in code. Maintained alongside the
code (CLAUDE.md rule: docs move with the code). Positioning: the survey base is
the 15+ framework corpus analyzed in `DEPENDENCY_ANALYSIS.md` / `PACKAGE_CATALOG.md`
(Mem0, Letta, Zep/graphiti, cognee, MemOS, MemoryBear, LightMem, SimpleMem, A-mem,
ReMe, powermem, EverMemOS, hindsight, honcho, telemem, langmem).

## N1 — Memory Firewall: first-class memory-poisoning defense (E1)

**Claim.** First open-source memory engine with an integrated, write-path
poisoning defense: trust scoring at write (source class × channel, retrieved
content capped), a quarantine tier excluded from consolidation/promotion until
corroborated, write-path anomaly detection (embedding-outlier + MINJA
heuristics), instruction-shaped-content flagging, and blast-radius `audit taint`.

**Evidence gap in the field.** OWASP ASI06 / LLM08 name the attack class; MINJA
(~98% injection success), AgentPoison (<0.1% poisoned entries → >80% attack
success), MemoryGraft (10 seeds → 48%) demonstrate it; **no surveyed framework
ships any defense** (Mem0/Letta/Zep/Cognee/MIRIX audit, plan §E1).

**In code.** Phase-0 DDL already carries `trust`/`quarantined`/`instruction_flag`
(`core/records.py`, migration 0001); contract in `core/policies/trust.py`; full
firewall lands P4. Provenance (`source`, prompt-version) makes taint audits a
log walk (ADR-001).

**Paper artifact.** Attack-replication harness in `evals/` (MINJA/AgentPoison
protocols) with defense on/off ablation → the (attack-success, utility-cost)
frontier.

## N2 — Governable event-sourced memory: retention-mode spectrum (D-45, ADR-011)

**Claim.** An event-sourced memory substrate whose audit/storage trade-off is a
typed, per-profile dial: `full` / `rolling` (prune provably never passes the
slowest projector's high-water mark) / `ephemeral` (projection-only), with
rebuildability degrading *loudly* (`RebuildUnavailableError`, `can_rebuild`)
rather than silently. Combined with zstd payload compression, this gives a
regulated profile (full+compress: audit intact, storage bounded) and a
high-volume profile (rolling) from one engine.

**Evidence gap.** Surveyed frameworks either keep no write-ahead truth at all
(most) or an unbounded one (event-log designs); none expose retention as a
governed, capability-signaled spectrum.

**In code.** `core/events.py` (EventLogMode), `services/storage/sqlite/engine.py`
(prune/min-high-water-mark logic, tested), templates `voice`/`regulated_financial`.

## N3 — Composable typed-memory combination calculus (D-13/D-14)

**Claim.** Nine memory types as independently enable-able units over one
universal record, with a declared dependency graph (hard + any-of edges),
deterministic C1(b) auto-enable closure, and a combination test matrix (15
boots) asserting every profile boots, round-trips, and describes itself.
Combinability is a *tested contract*, not a config accident.

**Evidence gap.** MemOS's MemCube composes 4 backend slots; no surveyed system
declares or validates inter-type dependencies (e.g. reflective⇒episodic,
shared⇒≥1 of semantic|episodic).

**In code.** `core/registry.py` (dependency closure + logged auto-enable),
`config/schema.py` (reserved v0.2 key), golden tests.

## N4 — Bi-temporal universal record with versioned provenance lifecycle (M1+D-42)

**Claim.** One record shape unifying: bi-temporal validity (event time vs record
time), full provenance object (role/channel/doc/message/prompt-version),
versioned lifecycle with in-record history, governance fields (PII tier,
consent tags), firewall fields, and dedup sketches (simhash/MinHash signature)
— present from the first migration so every later mechanism (conflict ladder,
dedup, decay, taint audit) is auditable against the same row.

**Evidence base.** Synthesizes graphiti's bi-temporality with MemOS's
lifecycle/provenance metadata; no surveyed record model carries all of it.

**In code.** `core/records.py`, migration `0001_phase0_substrate.py`.

## N5 — Cache-aware context assembly as an engine policy (E2, P1)

**Claim.** Prefix-cache-aware placement (stability-sorted: persona → skills →
facts → [cache boundary] → volatile) as a *memory-engine read policy* with
measured `cached_tokens`, not an app-level trick. Evidence: relocating volatile
working memory moved provider cache hit 7%→84%, −59% LLM cost (plan §E2).

**In code.** Contract in `core/policies/assembly.py`; logic P1.

## N6 — Deterministic-first hierarchical consolidation (D-40/D-42, P6)

**Claim.** Community→summary-parent consolidation using algorithmic clustering
(graspologic `hierarchical_leiden`) with the LLM only summarizing each
community — cheaper and reproducible vs. the field's LLM-only subclustering
(MemOS reorganizer). Enables a determinism ablation (same log ⇒ same hierarchy).

## N7 — Prompt provenance as a first-class cache/audit key (D-43, P2)

**Claim.** Every internal LLM call resolves a named, versioned prompt whose
version participates in semantic-cache keys (E3) and record provenance — so a
prompt upgrade cleanly invalidates caches and taint audits can name the exact
prompt that produced a memory. No surveyed framework versions prompts into
provenance.

## Evaluation discipline (for all claims)

Every benchmark reports the **(accuracy, tokens, latency)** triplet (plan §7 —
no leaderboard claims without the harness); ablations toggle exactly one of
E1–E9; combination matrix results reported per profile. Eval assertions via
deepeval live in `evals/`, outside the wheel (D-35).

## Status

| # | Contribution | Substrate in code today | Full mechanism |
|---|---|---|---|
| N1 | Memory Firewall | P0–P2 (columns, trust contract, provenance; **R1 trust gate live in the M4 ladder + rejected writes fully recoverable from audit events**; P3: quarantined records frozen out of decay/compression; ingest chunks carry doc-path taint trail) | P4 |
| N2 | Retention-mode spectrum | **P0 complete + tested; P3: prune scheduled in the sleep cycle** | ✔ complete |
| N3 | Combination calculus | P0 (registry + closure + golden tests) | matrix P1–P7 |
| N4 | Universal record | **P0 complete; M4/M5 consumers live in P2; M3/M6 consumers live in P3** (decay tier + cold-tier `content_zstd` on the same row — rebuild-identity holds across compression) | firewall consumer P4 |
| N5 | Cache-aware assembly | **P1 complete + tested** (MMR, θ-abstain, exposed cache boundary) | provider `cached_tokens` metrics P3+ |
| N6 | Deterministic consolidation | **P3: session-boundary detection + extractive summaries are fully deterministic (same log ⇒ same summaries); LLM only rewords, never structures** | hierarchical (community) tier P6 |
| N7 | Prompt provenance | **P2: versioned prompt pack live; P3: prompt version keys the E3 extraction cache** (upgrade ⇒ clean invalidation, tested) | ✔ complete |
