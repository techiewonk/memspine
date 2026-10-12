# Evaluation, safety, API and operations

This area decides how a v2 change earns its way into the defaults (G-01..G-05, G-25, G-26), how memory is defended
and governed (firewall, trust horizon, lineage, PII, consent, isolation, erasure, retention, audit: G-06..G-15), what
the public surface looks like (G-16..G-18) and how v2 is deployed, configured, pinned and migrated (G-19..G-24).
It has 26 questions. The owner took the recommendation on 25 of them.

There are two notable differences:

- **G-19 chose A, "library-first, inline runner"**, not the recommended C, "three named modes". Option A's own note
  says derived work is lost on a crash. The write and offline areas, however, chose a durable outbox (W-02 D,
  W-10 B+C, W-13 C). This chapter reads A as "embedded library with an inline runner and no named-mode matrix",
  keeps durability through a **durable local outbox inside the library**, and puts one question back to the owner
  (did you mean B?).
- **G-06 C carries the note "A + C"**: today's three firewall signals stay on as the base layer for every channel,
  and C's extra signals are added on top.

v2 is the next architecture of the same `memspine` package, built on the same principles (owner, 2026-10-11). It is
planned in `docs/v2/` and judged by its own gate against a frozen v1 reference (G-16, G-23, G-25). Today's facts that bear on this area are written into the decisions below:

- the P0 contamination fix 11853ed;
- the G-E1 defect seen live (eval_focus printed PROMOTE for both the arm and the reference);
- the 18:17 `uv sync` incident;
- the lock file, which still pins huggingface_hub 1.16.1.

## Design in one page

| # | Component (v2) | As decided |
|---|---|---|
| 1 | `memspine.evals.gate` | One paired gate in code against a named reference with a matching manifest. Every in-scope set must be present, with per-dataset cluster uncertainty and per-slice non-inferiority. The gate refuses to give a verdict when anything is missing. After it passes, the frozen config gets one sealed blind run (G-01 C). |
| 2 | `memspine.evals.suite` | Dev pools: LoCoMo (with cat 5) and OP-Bench. Blind gates: MAB-CR, ConvoMem and PrefEval. The sixth, dataset-free gate is the behavioural suite. LongMemEval stays out (G-02 D, owner note). |
| 3 | `memspine.evals.uncertainty` | Per-dataset paired cluster bootstrap and an exact sign test on flips. The band is widened to the flip rate seen in repeat runs (G-03 C). |
| 4 | `memspine.evals.behavioural` | Invariance (renaming, paraphrase, order swap, filler) plus safety pairs (tenant isolation, unanswerable questions, generic vs personal). It blocks adoption (G-04 C). |
| 5 | `memspine.evals.loop_metrics` | All five loop metrics on scripted, replayed episodes. Forgetting compliance and correction latency are non-inferiority guards, and cost per cycle has a declared tolerance (G-05 D). |
| 6 | `memspine.evals.manifest`, `.env` | A full manifest (environment freeze hash, prompt hashes, model and data hashes, cost and latency fields, engine line v1/v2) with recompute from raw outputs (G-25 D). Each pinned run gets its own venv built from a corrected lock (G-24 C). |
| 7 | `memspine.trust.firewall` | Base layer: instruction, embedding outlier and MINJA bridge on every channel. On top: instruction_extended and semantic_risk. query_anomaly stays opt-in. The assistant-role bridge exemption comes from the chat preset (G-06 A + C). |
| 8 | `memspine.trust.horizon`, `.lineage` | The trust-horizon invariant (THI) switches on when a grant exists (G-07 B). Implicit per-turn parents are recorded on the agent and tool channel whether or not THI is on (G-08 B). |
| 9 | `memspine.compose.trust_render` | An untrusted-data wrapper by default; claims-only in protected and regulated profiles (G-09 D). |
| 10 | `memspine.trust.privacy` | Secrets always redacted; PII and sensitive topics tagged; records above tier `low` withheld from remote providers (G-10 C). |
| 11 | `memspine.trust.consent` | Opt-in purpose tags plus a per-subject do-not-derive switch that K_P and K_C honour (G-11 C). |
| 12 | `memspine.scope` | Namespace is the hard boundary. Every read inside a multi-party namespace must name a viewer, and fails closed without one (G-12 B). |
| 13 | `memspine.trust.erasure` | A persisted reverse-lineage index over every cache and projection, generation checks, and re-derivation of surviving derived items (G-13 C). |
| 14 | `memspine.ops.retention`, `memspine.trust.audit` | Retention and audit are set per profile. Action audit is always on; read audit is on in shared and regulated profiles (G-14 D, G-15 D). |
| 15 | `memspine.api` | A small loop-verb core on a scoped handle, with admin groups on the engine. It is a new package, and v1 stays frozen (G-16 D). |
| 16 | `memspine.protocols` | REST binds only to loopback unless auth is set, and scopes are bound to the principal. MCP stays stdio. Framework adapters are deferred (G-17 C, G-18 A). |
| 17 | `memspine.runtime` | An embedded library with an inline runner that drains a **durable local outbox** in the same store. dbos and taskiq are alternative drainers (G-19 A with a durable outbox; confirmed by the owner, Q2). |
| 18 | `memspine.ops.ledger` | A uniform per-stage ledger and an optional OTel extra. No content in spans and no product telemetry (G-20 C). |
| 19 | `memspine.config` | No model by default. The base profile is generic; settings tied to a data shape live in presets. A flag registry with retirement (G-21 B, G-22 C). |
| 20 | `memspine.migrate` | Upcast the v1 events and rebuild every projection by replay. A v1 compatibility shim runs for one minor release (G-23 C). |
| 21 | Paper | Headlines come only from the frozen config's blind runs. Each novelty claim N1-N7 is stated with its own evidence (G-26 C). |

**How v2 relates to v1 (G-16, G-23, G-25; owner decision 2026-10-11).** v2 enhances `memspine` in place:

- Same package (`src/memspine/`), same repository, same principles: the event log is the truth, derived layers are
  opt-in, no model call by default, every change graduates through the gate.
- The v2 plan lives only in `docs/v2/`; v1 documents point to it.
- v2 components land in `memspine` behind flags and profiles, one phase at a time (`ROADMAP.md`); a component
  becomes default only after its gate passes.
- The v1 reference stays reproducible by commit (`pinned_run.sh` at the commit of clean-jina-full); every run
  manifest records the commit and the flag set.
- API changes ship with `memspine.compat.v1` aliases and deprecation warnings for one minor release (G-23 C).
- Store changes migrate by replaying the event log through upcasters into rebuilt projections; an old store is
  never edited in place.

## Decisions

### G-01 Adoption gate for any behaviour change: C, B plus one frozen blind run

- **Decision:** One gate tool in code. It makes a paired per-item comparison against a named reference with a
  matching manifest (G-25). It needs every in-scope dev set and uses per-dataset cluster uncertainty (G-03) and
  per-slice non-inferiority. It refuses to give a verdict when anything is missing. After a pass, the frozen
  configuration runs once on the sealed blind sets with a pre-registered metric and tolerance. D's null controls run
  as an occasional audit, not per adoption.
- **Verdict:** ok.
- **What will happen:**
  - Today's defect was seen live on 2026-10-11 at 19:37: `eval_focus.py f-chain-i2` printed PROMOTE because it scores
    against the slice's stored labels, and the reference run printed PROMOTE as well, although the paired result was
    net -1. Under C, that verdict cannot be produced, because a run is only ever judged against a named, manifest-
    matched reference.
  - `eval_blind` can no longer confirm with one set missing.
  - Cost: one extra blind run per adoption (estimate: one GPU unit).
  - Quality: no fewer correct adoptions, and the false ones are gone (estimate).
  - Covers all memory types and operators R, C, G, D, K_C and K_P.
- **Where:**
  - `evals/eval_focus.py:98` (`--ref` "informational") and `:107` (slice/ref mismatch only prints a note)
  - `evals/eval_blind.py:15` (confirm rule) and `:59` (global band)
  - `evals/eval_screen.py`
  - These become `memspine.evals.gate.AdoptionGate`, with the sketch
    `gate(candidate: RunSet, reference: RunSet, policy: GatePolicy) -> Verdict | Refusal`.
- **Depends on / affects:** G-02, G-03, G-04 and G-05 supply its inputs; G-25 supplies the manifest match; G-21 and
  G-22 graduation reads its verdicts; G-26 uses its blind results.
- **Migration:** v1's verdicts on the focus slice are taken from the paired comparison by hand until the gate lands
  (register, 19:37). The frozen v1 reference for v2 is clean-jina-full: 81.4% on 1,540 LoCoMo cat 1-4, at commit
  888b888.
- **Test or gate:** Replaying f-chain-i2 against f-jref2-i2 must give "not adopted". The reference compared with
  itself must give "no change". A run set with a blind set removed must give a refusal.

### G-02 Role of each benchmark in v2: D, B plus dataset-free behavioural suite as a gate

- **Decision:**
  - Dev pools: LoCoMo cat 1-5 (dev/held-out split by conversation) and OP-Bench (dev 331 / held-out 528).
  - Sealed blind gates: MAB-CR, ConvoMem and PrefEval, each sealed by a hash split and run once per frozen candidate.
  - Sixth gate: the G-04 behavioural suite.
  - LongMemEval is excluded (standing owner decision).
- **Verdict:** ok.
- **What will happen:**
  - Preference following becomes a gate for the first time, so personalisation regressions block.
  - Wins tied to one benchmark's shape (date wording, speaker names) must also pass the dataset-free suite.
  - Cost per adoption rises by three blind sets (estimate).
  - Memory types: episodic, semantic and reflective. Operators: R and K_P.
- **Where:**
  - `evals/run_blind_validation.sh`, `evals/freeze_blind_slices.py`, `evals/make_split.py`
  - Blind runs are wired but have never run, and PrefEval is downloaded but not wired.
  - These become `memspine.evals.suite` (a dataset-role registry read by the gate).
- **Depends on / affects:** G-01 and G-04; G-21 B uses "passed every in-scope set" as the test for the base profile.
- **Migration:** Keep v1's splits and hashes. Add the PrefEval split. Blind sets stay unseen until the first frozen
  v2 candidate.
- **Test or gate:** The gate refuses when any of the five sets or the suite is missing. A blind split hash is
  checked before each run.

### G-03 Uncertainty and noise band method: C, B calibrated by repeat runs

- **Decision:** A paired cluster bootstrap per dataset (resampling conversations or personas), giving a 95% interval
  of the paired difference, plus an exact sign test on flips. Each dataset's band is checked against two repeat runs
  of the same configuration, and the wider of the two is used.
- **Verdict:** ok.
- **What will happen:**
  - The single coefficient `0.328*sqrt(n)` (about ±7 questions on the 437-question focus slice) gives way to
    per-dataset intervals.
  - Measured on 2026-10-11 at 20:00: f-jref2-i2 compared with f-jref-i2 gave +0/-0 on all 437 questions. The
    deterministic local reader therefore contributes no flips, and the repeat-run check matters mainly where a judge
    or a hosted reader is nondeterministic.
  - CPU only, apart from one repeat run per dataset.
  - Operators: R and G.
- **Where:**
  - `evals/eval_blind.py:59` and `evals/eval_screen.py` (global band)
  - `evals/noise_floor.py` (waits for repeats, I16)
  - `accuracy_ci` in `evals/memspine_evals/metrics.py`
  - These become `memspine.evals.uncertainty`.
- **Depends on / affects:** G-01 consumes it; G-05 guards use the same intervals.
- **Migration:** Reuse the v1 `accuracy_ci` code as the base for B.
- **Test or gate:** On a seed-swap or identical rerun, the gate must report "no change" on at least 95% of its
  datasets. That is D's audit, run occasionally.

### G-04 Metamorphic and behavioural controls: C, invariance plus safety pairs, blocking

- **Decision:**
  - Invariance checks: renaming, paraphrase, speaker-order swap and filler, run on synthetic conversations with
    invented names (Ilse, Bram).
  - Safety pairs: tenant isolation, unanswerable probes, and generic vs personal requests.
  - Blocking rule: a flip under pure renaming, or any leak across a tenant pair, rejects the change.
  - D's store fuzzing grows later with G-08 and G-13.
- **Verdict:** ok.
- **What will happen:**
  - This is the guard against overfitting to a benchmark, with no dataset needed.
  - Retrieval-only variants run on CPU; reader variants need a small model budget (estimate: minutes per run).
  - It is also the screen that G-06 C and G-21 B need before they can be adopted.
  - Operators: R, C and G; all memory types.
- **Where:**
  - Only the I19 leakage probe exists today (12 configurations, no leak); rule 8 of `docs/GAP_TO_SOLUTION_PLAN.md`
    is prose.
  - New component: `memspine.evals.behavioural.BehaviouralSuite`.
- **Depends on / affects:** G-01 and G-02 (the sixth gate); G-06 (false-positive screen); G-12 (isolation pairs);
  G-21 (base settings).
- **Migration:** Port the I19 probe in as the first tenant pair.
- **Test or gate:** The suite runs in CI on retrieval-only variants. Any flip under renaming or any tenant leak fails
  the build.

### G-05 Loop-level metrics in the v2 harness: D, all five metrics as adoption guards

- **Decision:**
  - All five metrics (loop gain, drift fidelity, correction latency, forgetting compliance, cost per cycle) run on a
    protocol of scripted, replayed sessions. Example episodes: Ilse changes job; Bram asks the system to forget an
    address.
  - Forgetting compliance and correction latency are non-inferiority guards in G-01.
  - Cost per cycle carries a declared tolerance (plan rule 9).
- **Verdict:** ok.
- **What will happen:**
  - The paper's loop claims get measurements.
  - A recall gain that slows corrections or leaks forgotten content is blocked.
  - Under the inline runner (G-19), correction latency depends on whether derivations are drained. The protocol
    therefore reports two variants: with the read barrier (`wait_for_pending`, W-12 D) and without it.
  - Loop gain needs a utility judge (small reader budget, estimate).
  - Memory types: episodic, semantic and reflective. Operators: D, K_C and K_P.
- **Where:**
  - Only `cost_per_cycle` exists today (`evals/memspine_evals/metrics.py:128`).
  - New component: `memspine.evals.loop_metrics.EpisodeRunner`.
- **Depends on / affects:** G-01; G-13 (forgetting compliance is the erasure test); G-20 and P-22 (the cost ledger);
  W-12 (barrier).
- **Migration:** None. This is new code that runs against both engine lines, so v1 gets a baseline too.
- **Test or gate:**
  - Forgetting compliance: zero residual hits after a hard forget, across a restart.
  - Correction latency: no worse than the v1 baseline on the scripted episodes.

### G-06 Memory firewall default posture: C, with note "A + C"

- **Decision (combined):**
  - **Base layer (A):** instruction regex, embedding outlier and the 96-character MINJA prefix bridge stay on for
    every channel and every role, exactly as today. Nothing is removed.
  - **Added layer (C):** `instruction_extended` and `semantic_risk` (self-claimed authority, binding a future answer)
    are on by default. `query_anomaly` stays opt-in.
  - **Chat false positives:** C includes B's assistant-role exemption from the bridge. That conflicts with "keep A as
    the base". The closest compatible reading is to keep A's global default (no exemption) and ship
    `minja_bridge_exempt_roles: [assistant]` in the **chat data-shape preset** (G-21 B), which deployments with chat
    data select.
  - **Order of evaluation:** base signals first, then the added signals. Any signal quarantines or flags as it does
    today.
  - **Fallback (from the recommendation):** if the false-positive screen fails, `semantic_risk` falls back to opt-in.
- **Verdict:** ok-combined. Two things before adoption: a false-positive screen from the G-04 suite before C's
  signals become default, and owner confirmation that the exemption belongs in the preset rather than in the global
  default.
- **What will happen:**
  - Stronger N1 defence, and it stays deterministic: no model in the defence.
  - Without the chat preset, templated assistant openers are still quarantined (measured 39/40 on a synthetic chat,
    I21), which removes context from chat recall. With the preset, that problem goes away.
  - Latency: under 1 ms more per write (regex, estimate).
  - Memory types: episodic, semantic and shared. Operator: D.
- **Where:**
  - `src/memspine/config/schema.py:1376` (`FirewallSignalsConfig`: three on, extended/semantic/query off,
    `minja_bridge_exempt_roles` empty) and `:1407` (`FirewallConfig`)
  - `src/memspine/core/firewall.py:128` (`extended_instruction_shaped`), `:134` (`semantic_risk`),
    `:229` (`instruction_shaped`), `:275` (`Firewall.assess`)
  - These become `memspine.trust.firewall.SignalStack`, with the sketch
    `assess(record, channel, role) -> Verdict` running `base_signals` then `added_signals`.
- **Depends on / affects:** G-04 (screen); G-21 (the chat preset carries the exemption); G-09 (instruction-flagged
  records are rendered as inert data).
- **Migration:** v1 stores are not re-screened. Records written by v1 keep their verdicts, and records rebuilt by
  replay (G-23) are re-assessed under the v2 stack. They are only flagged, never dropped, so evidence stays.
- **Test or gate:**
  - False-positive quarantine rate on generic chat in the behavioural suite: no more than 1% of benign turns with the
    chat preset (proposed tolerance).
  - Attack success on the N1 attack set no higher than with A alone.

### G-07 Cross-agent reach bound by default: B, invariant on whenever a grant exists

- **Decision:** THI arms itself the moment any cross-namespace grant is created. A store with a single namespace is
  unchanged. C's live re-evaluation can graduate later through G-22 once its latency is measured.
- **Verdict:** ok. There is a dependency to resolve with G-08; see that decision.
- **What will happen:**
  - The N1 reach bound holds by construction in every shared store; today it only holds with the multi_agent
    template (kappa 0.5).
  - Single-user deployments pay nothing.
  - Shared stores get trust-aware ranking instead of the flat cap of 0.3, so foreign records rank lower (quality
    effect in shared stores unmeasured, estimate small).
  - Memory type: shared. Operators: R and D.
- **Where:**
  - `src/memspine/config/schema.py:1458` (`IntegrityConfig`, default off)
  - `src/memspine/core/integrity.py:19` (`IntegrityPolicy`)
  - `src/memspine/config/constants.py:138` (`TRUST_RETRIEVED_CAP`)
  - `config/templates/multi_agent.yaml`
  - These become `memspine.trust.horizon`, armed by the grant registry.
- **Depends on / affects:** G-08 (lineage), G-12 (grants); G-26 (N1 claim needs the enforcement-on reruns).
- **Migration:** Replay re-applies grants, so THI is armed on any migrated store that has grants.
- **Test or gate:** Creating a grant must flip THI on. A two-hop laundering attack must stay within kappa^hops.

### G-08 Lineage enforcement for derived records: B, implicit parents per turn

- **Decision:** The engine records what a session read and attaches it as parents to that turn's agent- and
  tool-channel writes (`implicit_parents=turn`). User-direct writes are not affected.
- **Verdict:** ok, with a required decoupling. In v1, the read ledger only runs when `integrity.enabled` is true
  (`engine.py:1921`, `:1944`). Under G-07 B, a single-namespace store keeps THI off, so G-08 would do nothing there.
  In v2, **lineage recording is unconditional**: parents are always recorded and the min-parent trust always applies.
  Grant attenuation (kappa) applies only when THI is armed.
- **What will happen:**
  - Closes read-then-write trust laundering.
  - Gives G-13 erasure complete parent sets.
  - May over-attach parents to agent writes and lower their trust slightly; with trust-blind ranking in
    single-namespace stores, the expected quality effect is 0 (estimate).
  - The ledger is in memory and turn-scoped, so losing it on restart loses at most one turn's parents.
  - Memory types: semantic, reflective and shared. Operators: D and K_C.
- **Where:**
  - `src/memspine/config/schema.py:1507` (`implicit_parents: "off"`)
  - `src/memspine/engine.py:1916` (`_record_reads`) and `:1936` (`_consume_reads`)
  - These become `memspine.trust.lineage.ReadLedger`.
- **Depends on / affects:** G-07 (decoupling), G-11 (consent inherits along lineage), G-13 (cascade), W-08 (provenance
  class).
- **Migration:** v1 records keep their declared parents. Implicit parents start at v2 cut-over; they are not
  reconstructed.
- **Test or gate:** In a store with THI off, read then write without `derived_from`: the write must carry the read
  ids as parents.

### G-09 Low-trust content in the context window: D, wrapper by default, claims-only in protected profiles

- **Decision:**
  - Every profile wraps records below the external-channel trust cap in a labelled "data, not instructions" block.
  - The protected and regulated templates render only mined claims, marked unverified, below the cap.
- **Verdict:** ok.
- **What will happen:**
  - No change on dev sets, where every record is stated by the user (expected 0 wrapped blocks; to be counted).
  - About 20 tokens per wrapped block (estimate).
  - Containment relies on the reader obeying the label, except in protected profiles.
  - Memory types: working, episodic and semantic. Operator: C.
- **Where:**
  - `src/memspine/config/schema.py:1510` (`untrusted_wrap_below` 0.0) and `:1517` (`claims_only_below` 0.0)
  - These become `memspine.compose.trust_render`.
- **Depends on / affects:** G-06, G-21 (profiles), R-area compose budget.
- **Migration:** Configuration only.
- **Test or gate:** A record injected through a tool, with instruction framing, appears only inside the wrapper. The
  dev sets show 0 wrapped blocks.

### G-10 PII and secret handling default: C, B plus a gate at the remote-model tier

- **Decision:**
  - Secrets (keys, tokens, private keys) are redacted at write.
  - PII is tagged (`pii:<kind>`, with `pii_tier` raised) and its text kept. Sensitive topics are tagged.
  - Records above tier `low` are withheld from any prompt to a non-local provider (`remote_llm_max_tier=low`).
    Local hosts are not affected.
- **Verdict:** ok. Still to define: how a provider is classed as remote (a localhost Ollama or vLLM counts as local),
  and how `assemble()` learns where its output is going. The engine controls its own model roles only, and the
  caller's reader is outside it.
- **What will happen:**
  - The default stack is local (D-03), so nothing changes there.
  - A user who binds a hosted model loses recall on tagged personal details until they opt in (estimate, small).
  - The pattern checks cost under 1 ms per write.
  - Memory types: episodic, semantic and resource. Operators: D and C.
- **Where:**
  - `src/memspine/config/schema.py:1425` (`redact_secrets` False), `:1430` (`pii` "off") and `:1638`
    (`remote_llm_max_tier` None)
  - These become `memspine.trust.privacy` (Redactor, EgressGate).
- **Depends on / affects:** G-21 (profiles); P-area model-role binding; eval harness runs with a hosted reader
  (`evals/bedrock_smoke.py`) must declare the tier.
- **Migration:** On replay, secrets in the v1 payloads are redacted in the v2 projections. The v1 log itself is left
  untouched, which is a known residue until G-13 D crypto-shredding.
- **Test or gate:** A planted key never appears in any projection. A PII-tagged record is absent from a prompt to a
  remote role and present in a prompt to a local role.

### G-11 Consent and purpose scopes: C, purposes plus do-not-derive

- **Decision:**
  - Purpose tags stay, with enforcement opt-in (`enforce` false, `untagged` allow).
  - A new per-subject do-not-derive switch is honoured by K_P and K_C whenever it is set. Raw evidence is still
    stored.
- **Verdict:** ok.
- **What will happen:**
  - The user gets a control over O3 systemic profiling ("don't profile Bram").
  - No effect on reads.
  - Memory types: semantic and reflective. Operators: R and K_P.
- **Where:**
  - `src/memspine/config/schema.py:1620` (`ConsentConfig`)
  - `src/memspine/core/privacy.py:128` (`inherited_consent`)
  - These become `memspine.trust.consent`.
- **Depends on / affects:** G-08 (inheritance along lineage); W-05 (`sub(m)` subject tags must exist); O-area K_P and
  K_C stages must check the switch.
- **Migration:** v1 consent tags carry over by replay. No subject starts opted out.
- **Test or gate:** With do-not-derive set for Bram, a sleep cycle yields no profile item or fact whose subject is
  Bram, and his raw turns remain searchable.

### G-12 Isolation boundary and multi-party visibility: B, namespace plus mandatory viewer

- **Decision:**
  - The namespace is the single hard enforcement point.
  - Inside a multi-party namespace (one with participant or visibility tags naming more than one party), every read
    must name a viewer.
  - A read without a viewer sees only `owner_shared` and unmarked records.
  - D (a physical store per tenant) was proposed as a server deployment setting under G-19. With G-19 A it drops to
    an operator choice: one data directory or database per tenant, with no engine support.
- **Verdict:** ok.
- **What will happen:**
  - Group chats and household assistants fail closed.
  - No cost.
  - Memory types: shared and episodic. Operator: R.
- **Where:**
  - `src/memspine/core/namespace.py:25` (`validate_namespace`) and `:56` (`grant_allows`)
  - `src/memspine/core/visibility.py:81` (`visible_to`) and `:96` (`inherit_tags`)
  - These become `memspine.scope` (Scope, ViewerPolicy). The viewer is fixed on the handle (G-16 D).
- **Depends on / affects:** G-16, G-17 (credentials bind scope), G-04 (tenant pairs).
- **Migration:** Visibility tags carry over by replay.
- **Test or gate:** The isolation tests from RV21 over write, search, update, delete and cache hits pass, and a
  multi-party read with no viewer returns no private records.

### G-13 Right to erasure across derived state: C, reverse lineage plus re-derivation

- **Decision:**
  - Every cache entry and derived projection (vectors, lexical index, graph edges, window replies, profile) records
    its source ids and generation, in a persisted reverse-lineage index.
  - Erasure tombstones them, and a generation check stops an in-flight derivation from being published.
  - A summary or profile that had the erased record among several parents is re-derived from the remaining parents.
- **Verdict:** ok. When no model role is bound (D-03 default), re-derivation falls back to deleting the derived item.
  Under G-19, re-derivation jobs travel through the durable outbox.
- **What will happen:**
  - Closes the RV07 gap: cache membership that outlives a restart.
  - Erasure becomes slower and costs model calls when a role is bound (estimate: one call per affected summary).
  - Useful summaries survive.
  - All memory types. Operators: K_C and D.
- **Where:**
  - `src/memspine/engine.py:9657` (`forget`), `:9699` (`erase_subject`), `:9731` (`erase_namespace`) and `:10230`
    (`verify_forget`)
  - `src/memspine/core/erasure.py:120` (`redact_record`) and `:167` (`retained_fields`)
  - These become `memspine.trust.erasure.ReverseLineageIndex`.
- **Depends on / affects:** G-08 (complete parents), G-05 (forgetting compliance), G-15 (erasure audit carries ids
  only), P-25 (same re-derivation queue), O-06 (watermarks).
- **Migration:** The index is built during replay (G-23). v1 caches are not migrated; they are rebuilt.
- **Test or gate:** After a hard forget and a process restart, verify-forget finds zero retained payloads in any
  projection or cache, including during a derivation that was in flight.

### G-14 Retention policy and event log lifetime: D, retention set per profile

- **Decision:** There is no global default. The retention class is set per template:
  - assistant: full log, and working and session records expire after a TTL;
  - regulated: explicit TTLs and a full log;
  - coding: rolling log.
  - The ephemeral log mode stays available as a template.
- **Verdict:** ok. With G-19 A, "ephemeral" is a template and a tested configuration, not a named deployment mode.
- **What will happen:**
  - Scratch growth in the assistant profile is bounded.
  - Personal long-term memory never expires automatically.
  - Memory types: working, episodic and semantic. Operator: K_C.
- **Where:**
  - `src/memspine/core/events.py:33` (`EventLogMode`)
  - `src/memspine/config/schema.py:43` (`EventLogConfig`) and `:1575` (`RetentionConfig`, empty)
  - These become `memspine.ops.retention`.
- **Depends on / affects:** G-21 (profiles), G-23 (replay needs a full log), G-15 (audit taint).
- **Migration:** v1 stores are full-log, so migration by replay is always possible.
- **Test or gate:** In the assistant profile, a session record expires after the TTL. A long-term record never does.

### G-15 Audit log scope and default: D, actions always; reads in shared and regulated profiles

- **Decision:**
  - forget, correct, erase, export, grant and retention expiry always append a chained `memory.audit` event that
    carries ids and reasons only.
  - The multi_agent and regulated profiles also audit reads: the record ids returned and the purpose.
- **Verdict:** ok.
- **What will happen:**
  - Every destructive act is attributable at low volume.
  - In shared profiles, read audit adds one event per search or assemble (estimate: under 1 ms, and the log grows
    with reads).
  - All memory types. Operators: R and D.
- **Where:**
  - `src/memspine/config/schema.py:1587` (`AuditConfig`, both off)
  - `src/memspine/engine.py:10511` (`audit_chain_ok`)
  - `src/memspine/core/audit.py:71` (`trace_taint`)
  - These become `memspine.trust.audit`.
- **Depends on / affects:** G-13 (an erasure event must not retain the erased content), G-21.
- **Migration:** The audit chain starts fresh in v2, and its first event records the v1 log high-water mark.
- **Test or gate:** The chain verifies after a run of erasures. No audit event contains erased text.

### G-16 Shape of the public Python API: D, loop-verb core on scoped handles

- **Decision:**
  - The loop verbs (remember, recall, compose, correct, forget, sleep) live on a scoped handle. Namespace, principal,
    purpose and viewer are fixed when the handle is created.
  - Admin groups stay on the engine: admin, privacy, audit, shared and diagnostics.
  - Same `memspine` package (owner, 2026-10-11): the scoped handle is added beside v1's `Engine` (about 114 public
    methods), whose verbs become `memspine.compat.v1` aliases with deprecation warnings.
- **Verdict:** ok (package question settled by the owner).
- **What will happen:**
  - The class of scope mistakes RV21 warns about goes away.
  - The 15,179-line facade is split.
  - No runtime cost.
  - All memory types. Operators: R, C, D, K_C and K_P.
- **Where:**
  - `src/memspine/engine.py:1672` (`write`), `:5035` (`search`) and `:5843` (`assemble`)
  - `src/memspine/protocols/tools.py:223` (`ToolContext`, today's scoped context)
  - These become `memspine.api`. Interface sketch: `h = engine.scope(ns, principal=, purpose=, viewer=)`, then
    `await h.remember(source) / h.recall(q) / h.compose(q, budget) / h.correct(id, value) / h.forget(id, hard=)`.
- **Depends on / affects:** G-12, G-17, G-18, G-23 (shim maps v1 names), W-01 (source envelope is the argument of
  `remember`).
- **Migration:** `memspine.compat.v1` maps the v1 verbs onto handles, with deprecation warnings, for one minor
  release (G-23 C).
- **Test or gate:** The public surface is snapshot-tested. A handle cannot read outside its namespace or viewer.

### G-17 Remote protocols and default authentication: C, loopback only unless auth is set; principal-bound scopes

- **Decision:**
  - With auth mode `none`, the REST server refuses any bind other than 127.0.0.1.
  - Each credential's principal and namespace globs are the only scopes it can name; a request field can never widen
    them.
  - MCP stays stdio. Remote MCP (D) is deferred.
- **Verdict:** ok.
- **G-19 effect:** C assumed a "server mode" with Postgres and a separate worker process. Under G-19 A, the REST app
  is a host for the library. It runs the same inline runner and durable outbox inside its process. Several processes
  can share one store safely because of outbox leases (see G-19). The Postgres storage profile (M-04 C) is the path
  for more than one instance.
- **What will happen:**
  - The server is no longer open by default.
  - Mem0's defect, an API key that is not bound to its principal, cannot happen here.
  - Memory type: shared. Operators: R and D.
- **Where:**
  - `src/memspine/protocols/rest/app.py:86` (header-trusting binding seam) and `:212` ("NO AUTHN in v0.1")
  - `src/memspine/protocols/rest/auth.py`
  - `src/memspine/config/schema.py:1683` (`RestAuthConfig.mode = "none"`)
  - `src/memspine/protocols/mcp.py:130` (`serve_stdio`)
  - These become `memspine.protocols.rest` and `memspine.protocols.mcp`, both built on scoped handles.
- **Depends on / affects:** G-16, G-12, G-19.
- **Migration:** v1 API keys carry over. Their globs become handle scopes.
- **Test or gate:**
  - Binding to 0.0.0.0 with mode `none` exits with an error.
  - A key for `ilse/*` that requests `bram/*` gets a 403.

### G-18 Framework adapters (I70): A, stay on hold; MCP is the adapter

- **Decision:** No adapters in v2 core. If they are revived, they take B's shape: thin lazy-import extras on the tool
  dispatcher.
- **Verdict:** ok.
- **What will happen:** No change. Agents use the Python handle, REST or the five MCP tools. Memory types: shared and
  working. Operators: R and D.
- **Where:**
  - `src/memspine/protocols/tools.py:223`
  - `src/memspine/protocols/mcp.py:47`
  - v2: none (later, a `memspine-adapters` extra).
- **Depends on / affects:** G-16, G-17.
- **Migration:** None.
- **Test or gate:** None. If revived, an adapter must pass the dispatcher guard tests.

### G-19 Deployment modes and background runners: A, library-first with the inline runner (recommended C)

- **Decision as answered:** The embeddable library with the inline runner is the default. Servers, dbos and taskiq
  are extras. There is no matrix of three named, CI-tested modes.
- **What follows from choosing A over C:**
  1. There are no first-class "server" or "ephemeral" modes with their own templates and CI lanes. The REST and MCP
     hosts and the EPHEMERAL log stay as configurations.
  2. Option A as written leaves RV01 open ("derived work is lost on crash"). That contradicts several decisions taken
     elsewhere. **Closest compatible reading: A plus a durable local outbox inside the library.** Nothing about
     deployment changes: no extra service, the same embedded default. In substance this is option B without C's mode
     matrix.
- **How durability works in the inline runner (proposed design):**
  - **Outbox in the same store.** A `derivation_outbox` table in the event-log database (SQLite by default, Postgres
    in the server storage profile). Its row is written **in the same transaction** as the WRITE event: this is W-02
    D's derivation intent.
  - **Row fields:** id, namespace, kind (window / session_close / sleep_stage / heavy_projection / rederive /
    pending_merge), source ids with versions, derivation fingerprint (P-12), generation (G-13), status (pending /
    leased / done / failed / dead), attempts, lease_owner, lease_until, not_before, last_error.
  - **The inline runner is an in-process drainer.** After `remember` returns, it schedules a drain task in the host's
    event loop. It claims rows with a lease (a conditional update on status and expired lease) and runs the stage.
    It deposits idempotently, keyed by source version and fingerprint, and marks the row done **in the same
    transaction as the deposit**, so the acknowledgement follows the commit (W-13 C, O-10 B). Failures back off
    through `not_before`; after N attempts a row goes dead-letter (D-18 severity).
  - **Recovery.** `Engine.open()` reclaims expired leases and resumes. `close()` drains with a timeout, and anything
    left stays pending rather than abandoned. That replaces `jobs_abandoned` at `workers/windows.py:568` and the
    oldest-batch drop at `:808`.
  - **No daemon.** A process that lives only briefly (CLI, serverless, a notebook) finishes its work at the next open,
    on an explicit `engine.drain()`, or through a `memspine drain` command that a host cron can call.
  - **Sleep triggers (O-02 C)** are evaluated after writes and at open, inside the host process only. A namespace
    whose host never runs again keeps pending rows, and the read side sees them through watermarks (O-06 B).
  - **Locks.** The per-namespace job lock (O-03 B) is a lease row in the same store, not an in-process asyncio lock.
    Two processes on one SQLite file, or a REST host plus a CLI, therefore never overlap.
  - **dbos and taskiq** become alternative drainers of the same outbox for many instances. They do not define the
    jobs a second time.
  - **The ephemeral configuration** keeps the outbox in memory and is non-durable by declaration.
- **Decisions that assumed a durable runner or server mode, and their status under this reading:**
  - **W-02 D** (derivation intent in the receipt): holds, because the intent is the outbox row.
  - **W-10 B+C** (heavy projections through the outbox): holds; the inline runner drains them.
  - **W-13 C** (leased at-least-once outbox): holds only with the outbox. It is **contradicted by a literal A**.
  - **W-25 B** (underived markers): holds, as an outbox state.
  - **O-01 B** (three tiers): holds; all tiers run in the host process.
  - **O-02 C** (triggers): holds, but fires only while a host process runs. Document that.
  - **O-03 B** (per-namespace job and lock): holds if the lock is a store lease.
  - **O-10 B** (persisted backlog): holds.
  - **P-21 C** (time-to-searchable SLO): measured in-process. Queue delay includes the time no process was running.
  - **P-25 C** (selective re-derivation): uses the outbox.
  - **M-04 C** (server storage profile): holds as a storage profile. It is no longer tied to a tested "server mode".
  - **G-12 D option** (store per tenant as a server setting): dropped to an operator choice.
  - **G-14 D** (ephemeral template): holds as a template.
  - **G-17 C** (REST auth): holds; the REST host embeds the library.
  - None of these must be re-decided if the owner accepts the outbox reading. All of them must be revisited if
    derived work is meant to stay non-durable.
- **Verdict:** needs-owner. The question: "Option A says derived work is lost on a crash, but W-02 D, W-10 B+C and
  W-13 C require a durable outbox. Do you accept a durable local outbox inside the library (option B in substance),
  or did you mean derived work to stay non-durable by default?"
- **What will happen (outbox reading):**
  - One extra row insert per write in the same transaction (estimate: under 1 ms on SQLite, against today's
    55-210 ms hot path).
  - No lost derivations across restarts.
  - No extra service.
  - One CI lane instead of three, so the server and ephemeral configurations get smoke tests only (risk: they rot).
  - Memory types: working and episodic. Operators: D and K_C.
- **Where:**
  - `src/memspine/config/schema.py:265` and `:278` (`WorkersConfig.runner = "inline"`)
  - `src/memspine/workers/inline.py` (`InlineRunner.run`, no persistence)
  - `src/memspine/workers/windows.py:326` (in-memory deque), `:568` (abandoned on stop) and `:808` (pending edges
    dropped above `WINDOW_PENDING_EDGES_MAX`)
  - `src/memspine/workers/scheduler.py:38`
  - These become `memspine.runtime`: `DerivationOutbox`, `InlineRunner(drainer)`, and `DbosDrainer` /
    `TaskiqDrainer` as extras. Sketch: `outbox.enqueue(tx, job)`, `outbox.claim(n, lease_s) -> [Job]`,
    `outbox.ack(tx, job_id)`.
- **Depends on / affects:** G-14, G-17, and the W-, O- and P- decisions listed above.
- **Migration:** v1 has no persisted queue. At cut-over, replay enqueues derivation for every source that has no
  derived record yet.
- **Test or gate:** Fault injection (the RV01 gate). Kill the process after inference and before the deposit, and
  again after a partial deposit. On reopen, every derivation completes exactly once in effect, with no duplicates.

### G-20 Observability, stats and telemetry: C, per-layer ledger plus optional OTel export

- **Decision:**
  - Every loop stage and derived layer reports calls, tokens (prompt, completion, cached), retries, failures, dropped
    coverage and p50/p95 through one stats object.
  - An optional OTel extra emits spans and metrics carrying ids and counts only.
  - No product telemetry.
- **Verdict:** ok.
- **What will happen:**
  - Failures in the sleep layers become visible (G-W13).
  - The ledger feeds cost per cycle (G-05) and the manifest's cost fields (G-25).
  - Overhead is negligible (estimate).
  - All memory types and all operators.
- **Where:**
  - `src/memspine/engine.py:13279` (`usage`), `:13292` (`structured_stats`), `:13375` (`describe`) and `:1629`
    (`write_timers`)
  - `src/memspine/config/schema.py:1606` (`ObservabilityConfig`)
  - These become `memspine.ops.ledger.StageLedger` and the `memspine[otel]` extra.
- **Depends on / affects:** G-05, G-25, P-22 C (persisted ledger rows), G-19 (outbox queue delay and backlog age are
  ledger fields).
- **Migration:** None.
- **Test or gate:** Every stage that is registered reports non-null counters in an end-to-end test. The OTel span
  payloads contain no record text.

### G-21 Defaults policy and configuration profiles: B, no model; a generic base with presets for data shapes

- **Decision:**
  - D-03 stays: no model role is bound by default.
  - The base profile keeps only settings that passed every in-scope set and the behavioural suite.
  - Settings tied to a data shape (dated turns, named speakers, the chat assistant exemption from G-06) move to
    presets selected by data shape.
- **Verdict:** ok.
- **What will happen:**
  - The combo-A read settings measured on paid LoCoMo ablations (ADR-033) become a dated-chat preset until they pass
    the blind sets.
  - LoCoMo runs must name that preset, or the default score may drop (estimate).
  - Generic deployments stop inheriting settings tuned to one benchmark's shape.
  - All memory types. Operators: R, C and D.
- **Where:**
  - `src/memspine/config/schema.py:232` (`LLMConfig`, roles empty)
  - `src/memspine/config/constants.py:548` (`DEFAULT_TEMPLATE = "assistant"`)
  - `src/memspine/config/templates/base.yaml`
  - These become `memspine.config.profiles`.
- **Depends on / affects:** G-01, G-02, G-04, G-06, G-09, G-14, G-15, G-22.
- **Migration:** Every v1 template maps to a v2 profile plus a preset. The manifest records both (G-25).
- **Test or gate:** Every setting in the base profile carries a passing G-01 verdict id in the flag registry (G-22).

### G-22 Flag graduation and flag retirement: C, a registry plus a retirement budget

- **Decision:**
  - Every flag carries a status (experimental / screened / candidate / profile-default / default / deprecated), an
    evidence id and its profile list.
  - A flag that is measured negative, or left unscreened for two releases, is deprecated and then removed.
  - v2 starts by cutting the measured-negative flags.
- **Verdict:** ok.
- **What will happen:**
  - About 428 schema fields shrink. The word-vector leg (B15) and other measured negatives are not carried into v2.
  - Plan rule 10 becomes enforceable in CI.
  - All memory types. Operators: R, C and D.
- **Where:**
  - `src/memspine/config/schema.py:288` (`ReadConfig`, 242 fields)
  - `evals/analysis/GAP_REGISTER.md` (graduation tracked by hand)
  - These become `memspine.config.flags.FlagRegistry`, read by `describe()` and CI.
- **Depends on / affects:** G-01, G-21, G-23 (the migration warns on removed flags).
- **Migration:** v1 config files are loaded through a translator that maps or warns per field.
- **Test or gate:** A release cannot ship a default whose registry entry lacks evidence.

### G-23 Backwards compatibility and migration from v1: C, replay rebuild with a period of dual reads

- **Decision:**
  - The v1 event log stays as truth. Old event versions are upcast on read.
  - Every v2 projection is rebuilt by replay into a **fresh v2 store**, and old derived records are regenerated,
    never mixed with new ones.
  - The v1 store stays readable through a compatibility shim, with deprecation warnings on v1 API names, for one
    minor release.
- **Verdict:** ok.
- **What will happen:**
  - No stale v1 derived records sit beside v2 ones.
  - Re-extraction costs model calls when a role is bound. With no role bound, only deterministic projections are
    rebuilt.
  - The v1 store is never written, so v1 results stay reproducible.
  - All memory types. Operators: D and K_C.
- **Where:**
  - `src/memspine/core/events.py:122` (`schema_version = 1`)
  - `src/memspine/services/storage/sqlite/alembic/versions/` (0001-0005)
  - `src/memspine/engine.py:13357` (`rebuild`)
  - These become `memspine.migrate` (V1Upcaster, ReplayBuilder) and `memspine.compat.v1`.
- **Depends on / affects:** G-14 (needs a full log), G-16, RV05 (replay of an old-key store creates no duplicates),
  M-02 (layer field set during replay), G-19 (replay enqueues derivations).
- **Migration:** This decision is the migration.
- **Test or gate:** Replay a v1 store twice: the two results are identical and contain no duplicate edges or keys.
  The harness `engine: v1` arms still reproduce clean-jina-full at 81.4%.

### G-24 Environment pinning for runs and installs: C, a separate environment for each run

- **Decision:** Each pinned run builds its own venv from the lock in its worktree (uv cache), records the freeze hash
  in the manifest, and never touches the shared `.venv`. B (lock checked at run start) is the minimum fallback.
- **Verdict:** ok, with a **prerequisite**.
  - At 18:17 on 2026-10-11, a `uv sync` in the shared `.venv` re-installed the stale lock. About 50 packages changed,
    and huggingface_hub fell to 1.16.1, below `datasets`' >=1.31. The f-contract, clean-jina-full, f-gc2/3/9 and
    f-win runs failed at their two-question check.
  - The lock **still pins huggingface_hub 1.16.1** (`uv.lock:1542`, wheel at `:1555`), and it was deliberately not
    regenerated (I69).
  - A per-run venv built from today's lock would therefore reproduce the failure in every run. The lock must be
    regenerated and committed first, with huggingface-hub >=1.31; the working environment had 1.33.0 restored at
    19:41.
- **What will happen:**
  - Concurrent syncs can no longer change a run.
  - A few seconds per run (uv cache) and more disk (estimate).
  - The 20:00 rerun (f-jref2-i2 compared with f-jref-i2: +0/-0 on 437 questions) shows that the 18:17 change did not
    move scores. The danger was silent breakage or drift, not this particular score.
  - Evaluation infrastructure only. Operators: R and G.
- **Where:**
  - `evals/pinned_run.sh:2` (pins code in a detached worktree) and `:23` (`MEMSPINE_PINNED_COMMIT`); the shared venv
    is still used today.
  - `uv.lock:1542`
  - These become `memspine.evals.env.RunEnv`.
- **Depends on / affects:** G-25 (freeze hash in the manifest), G-22 (I69 stale extras).
- **Migration:** v1 runs adopt the same per-run venv, so the frozen v1 reference reruns under identical pinning.
- **Test or gate:** A run refuses to start if the freeze hash differs from the lock's. A sync of the shared venv
    during a run does not change it.

### G-25 Run manifest and comparability key: D, full manifest, recompute, cost and latency fields

- **Decision:** The manifest records:
  - the environment freeze hash (G-24);
  - the rendered prompt hash for each role;
  - model and tokenizer ids with file hashes;
  - dataset and split checksums;
  - reader and judge settings;
  - hardware and concurrency;
  - prefix-cache state, queue delay, retries, tokens and p50/p95 per stage;
  - the **engine line (v1 / v2) and package version**.
  
  Every headline number can be recomputed from the stored per-item outputs with one command. The gate refuses a
  comparison whose manifests differ outside the declared changed field.
- **Verdict:** ok.
- **What will happen:**
  - Comparisons become checkable.
  - Contamination becomes visible through the prompt hashes. The P0 fix 11853ed (single-word LoCoMo gold answers
    removed from prompt examples in window_extract, extract@session3/4, extract_edges and others) changed prompt
    hashes, so any run on the pre-fix prompts is identifiable: the GC screens at 88294e0 are contaminated for
    absolute claims. clean-jina-full and f-contract are unaffected.
  - Per-item files stay local, keeping licence hygiene (OP-Bench scores only).
  - Evaluation infrastructure. Operators: R, C, G and D.
- **Where:**
  - `evals/README.md` (today's digests: config, protocol, build and environment)
  - `evals/pinned_run.sh:23`
  - These become `memspine.evals.manifest.RunManifest`.
- **Depends on / affects:** G-01, G-24, G-26, P-07 (gold-lint now screens single words).
- **Migration:** v1 runs are re-recorded with the full manifest when they are rerun as the frozen reference.
- **Test or gate:** The recompute hash matches. A pairing of a v1 run with a v2 run is accepted only when "engine
  line" is the declared changed field.

### G-26 Claims policy for the MemSpine paper: C, blind headlines plus mechanism claims tied to loop metrics

- **Decision:**
  - Headline claims come only from the frozen configuration's single blind run, with the manifest hash. Numbers from
    the dev pools are labelled "development".
  - Each novelty (N1-N7) is stated with its own evidence:
    - N1 as an attack-success vs utility frontier with enforcement on;
    - forgetting compliance and correction latency for the loop;
    - cost per cycle with manifests.
  - Claims without a measurement are stated as design, not as results.
- **Verdict:** ok.
- **What will happen:**
  - Lower but defensible headlines.
  - The contaminated GC screens and pre-fix extraction arms cannot back absolute claims.
  - The 81.4% clean re-baseline is a development-pool number.
  - N1 waits for the reruns with enforcement on.
- **Where:**
  - `docs/RESEARCH_NOVELTY.md`
  - `evals/analysis/GAP_REGISTER.md:765` (contamination record)
  - v2 component: a claims register in `docs/v2` (no code).
- **Depends on / affects:** G-01, G-05, G-07, G-25.
- **Migration:** v1 numbers are cited as the v1 reference, never as v2 results.
- **Test or gate:** Every number in the paper resolves to a manifest hash and a blind or development label.

## Conflicts and open points

| id | Issue | Proposal | Needs |
|---|---|---|---|
| G-19 | A says derived work is lost on a crash; W-02 D, W-10 B+C, W-13 C, W-25 B, O-03 B and O-10 B need a durable outbox | A plus a durable local outbox inside the library (B in substance, without C's mode matrix); inline runner as the drainer; dbos and taskiq as alternative drainers | owner: confirm outbox, or say whether B was meant |
| G-19 / G-17 / M-04 | C's "server mode with worker process" is gone | The REST host embeds the library and drains the outbox; Postgres storage profile for many instances; leases make shared stores safe | none (design) |
| G-19 / O-02 | With no daemon, sleep triggers fire only while a host runs | Evaluate triggers at open and after writes; `memspine drain` for a cron; watermarks expose the backlog | none (document) |
| G-19 / G-12 | Store per tenant was to be a server setting | An operator choice (one data dir or database per tenant), with no engine feature | none |
| G-06 | "A + C" keeps the bridge on the assistant role, while C would exempt it | Global default A + C signals; the assistant exemption ships in the chat preset (G-21) | owner: confirm the exemption belongs in the preset; FP screen |
| G-08 / G-07 | v1 implicit parents need `integrity.enabled`; G-07 B keeps THI off in single-namespace stores | Lineage and min-parent trust always on; kappa only when a grant exists | none (design) |
| G-10 | The engine cannot see where the caller's reader sends `assemble()` output | `assemble(..., destination="local"|"remote")` and a provider classification table | research: provider classification |
| G-13 | Re-derivation needs a model role; D-03 binds none | Fall back to deleting the derived item when no role is bound | none |
| G-16 / G-23 / G-25 | Package path | Settled: same `memspine` package, v2 lands behind flags; v1 reproducible by commit; `memspine.compat.v1` aliases | none |
| G-24 | A per-run venv from today's lock reproduces the 18:17 failure | Regenerate `uv.lock` with huggingface-hub >=1.31 before C | none (prerequisite) |
| G-21 | Moving combo-A into a preset may lower the LoCoMo score under the default | The harness names the dated-chat preset; the manifest records it | none |

## Measurements to run before building

| What | Screen or dataset | Gate |
|---|---|---|
| False-positive rate of the G-06 A + C stack | G-04 behavioural suite, generic synthetic chat (Ilse, Bram), with and without the chat preset | No more than 1% of benign turns quarantined with the preset; N1 attack success not above A alone |
| Repeat-run flip rate per dataset | Two runs of the same configuration on LoCoMo, OP-Bench, MAB-CR, ConvoMem and PrefEval (judge on) | The band of each dataset is the wider of bootstrap and flip rate (G-03) |
| Gate replay of G-E1 | f-chain-i2 against f-jref2-i2; the reference against itself; one blind set removed | "not adopted", "no change", refusal (G-01) |
| Wrapped-block count | LoCoMo and OP-Bench dev with G-09 D | 0 wrapped blocks, or the token cost reported |
| Outbox overhead and durability | Write micro-benchmark (55-210 ms hot path) plus RV01 fault injection | p95 write rise under 5%; exactly-once effect after a kill (G-19) |
| THI auto-arm cost | Two-namespace grant store, read p95 | Rise reported; reach bound holds (G-07) |
| Effect of moving combo-A to a preset | LoCoMo dev, base profile compared with the dated-chat preset | Difference reported; the base keeps only settings that pass G-01 and G-04 (G-21) |
| Environment pin | A per-run venv from the regenerated lock; a concurrent sync of the shared venv | Freeze hash unchanged; f-jref2-i2 reproduces +0/-0 (G-24) |
| N1 with enforcement on | N1 attack set with THI armed, compared with utility | Attack/utility frontier for the paper (G-26) |


## Owner follow-up (2026-10-12)

- **Q2 Durable outbox** (as proposed): Library-first inline runner draining a durable outbox in the same store; leftover work resumes on the next open; dbos and taskiq are optional drainers.
- **Q4 Neo4j is the server graph** (changed): Embedded profile: auto Ladybug else SQL adjacency (M-18 A). Server profile: Postgres + pgvector + Postgres full text for records, Neo4j for the graph, fed only through the outbox. G-13: the reverse-lineage erasure index must reach Neo4j; isolation and erasure probes run on the server profile as an exit gate.
- **Q8 Assistant exemption in the chat preset only** (as proposed): Base firewall signals on every channel, extended and semantic on top; the assistant-role exemption only in the chat data-shape preset.

## Change log

- 2026-10-11: Chapter written. 26 questions verified: 24 ok, 1 ok-combined (G-06 A + C), 1 needs-owner (G-19 A
  read as a durable local outbox inside the library). Records the G-E1 PROMOTE defect, the P0 contamination fix
  11853ed, the 18:17 `uv sync` incident and the lock pin of huggingface_hub 1.16.1, and proposes the separation of
  v2 and v1 (`memspine`, `engine: v1|v2`).
- 2026-10-11 (later): owner decided v2 stays inside `memspine` (same package and principles, plan in `docs/v2/` only); separation passages rewritten.
- 2026-10-12: owner follow-up answers recorded (Q2, Q4, Q8).
- 2026-10-12 (later): stale owner-confirmation markers resolved from the Q1-Q8 answers.
