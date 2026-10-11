# MemSpine v2 roadmap

Build order for v2, in phases. Each phase has an entry condition and an exit gate; no phase starts building a
decision marked needs-measurement until its measurement has passed (see `VERIFY.md` section 3). Durations are not
estimated here; they are set when Phase 0 closes.

## Phase 0: settle and prepare (v1 branch and this plan; no v2 code)

- **Owner answers Q1-Q9**: done (2026-10-12); chapters, decisions and architecture updated.
- **Fix the environment**: done 2026-10-11 (`1d15194`: huggingface-hub 1.33.0, click override for deepeval);
  next, per-run environments (G-24).
- **Harden the gate first** (G-01, G-E1 in v1; paired gate built 2026-10-12, manifests and sealed blind run still open): paired comparison against a named reference, all sets required,
  per-conversation uncertainty. Every later measurement depends on it.
- **Finish the queued v1 screens**: GC screens (contaminated for absolute claims), f-win on the fixed prompts,
  contract-jina-full against the 81.4% reference, then MAB-CR and ConvoMem for A03.
- **Exit gate**: owner answers recorded; the gate replays the 2026-10-11 case correctly (refuses PROMOTE for an
  identical arm); a fresh environment reproduces clean-jina-full.

## Phase 1: foundations (inside `memspine`, behind flags)

- Event log and upcasters; the record shape with `layer`, envelope, tri-temporal times, scope tuple, lineage table,
  identity key with kind and occurrence (M-01, M-02, M-05, M-06, M-09, M-10, M-17, M-26).
- Durable local outbox and inline runner (Q2; W-10, W-13, G-19); run manifest and ledger (G-25, P-22).
- Storage profiles: embedded first; server later in the phase with Postgres for records and a real Neo4j graph adapter fed through the outbox, with erasure, isolation and rebuild tests on both systems (M-04, M-18, Q4).
- Migration: replay a v1 event log into a fresh v2 store (G-23).
- **Exit gate**: v1 data migrated and replayed byte-stably; fault-injection tests pass (crash after inference, after a
  partial deposit, before acknowledgement, during forget); a raw-only v2 read is non-inferior to the v1 raw read on
  the focus slice.

## Phase 2: write path

- Intake envelope, stamping, role policy, three firewall gates (W-01..W-09).
- Segmentation with time-gap cut and token batching (W-11; measurement first).
- Online extraction with prompt v2, constrained decoding, repair re-ask, grounding, code-side resolution, conflict
  ladder with the cardinality registry (W-12..W-22; measurements W-17, W-18, W-19, P-04, P-23 first).
- **Exit gate**: the window arm on v2 is non-inferior to the v1 reference with grounding at least 95% on the labelled
  set, at most one call per window, write p95 at most 250 ms.

## Phase 3: read path

- Depth tiers, typed query contract (after its full run and blind sets), legs, family fusion, protected pool, Jina
  rerank (Q5, Q6), lifting, relevance gate, packer fix, typed render (R-01..R-21).
- Deep tier with the sufficiency check only after `f-suff` passes (R-22).
- **Exit gate**: v2 standard tier beats or matches clean-jina-full (81.4) beyond the band on the full set with no
  category loss, then holds on MAB-CR, ConvoMem and PrefEval; zero-model read p95 at most 300 ms.

## Phase 4: offline memory

- Scheduler, triggers, namespace jobs with leases and watermarks (O-01..O-06).
- Call 2 at session close, stitched episodes, summaries (opt-in layers per Q1) (O-07..O-09).
- Reconcile backlog, staged entity resolution and contradictions, near-duplicate merge, value histories, pattern observations behind a screen (Q3) (O-10..O-15;
  calibrations O-13, W-19 first).
- Profile card operations, reflection, communities and entity summaries opt-in, prospective and procedural memory as
  decided (O-16..O-26).
- **Exit gate**: OP-Bench and PrefEval up with no LoCoMo loss beyond band; contradiction recall on the labelled set;
  sleep at most 0.2 calls per turn; restart and locking fault tests pass.

## Phase 5: trust, API and operations

- Firewall layering, trust horizon on grants, privacy tiers, consent switch, erasure index, audit (G-06..G-15).
- Loop-verb API on scoped handles, REST and MCP, config presets with flag retirement (G-16..G-22).
- **Exit gate**: behavioural suite passes; the N1 attack and utility runs hold with enforcement on; isolation and
  erasure probes pass on both storage profiles.

## Phase 6: release and paper

- Freeze the v2 configuration; one sealed blind run per blind set; loop metrics on scripted episodes (G-05).
- Paper claims only from the frozen configuration's blind runs; each N1-N7 claim with its own evidence (G-26).
- Release `memspine` 2.0 with `memspine.compat.v1` for one minor release.

## Change log
- 2026-10-11: first version.
- 2026-10-11 (later): v2 lands inside `memspine` (Q9); lock fix done.
- 2026-10-12: Q1-Q9 answered; pattern observations (Phase 4) and the Neo4j server graph (Phase 1) added.
