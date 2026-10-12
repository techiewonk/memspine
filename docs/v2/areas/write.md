# Write path and ingest

This area decides what happens from the moment a caller hands MemSpine v2 some content until the
derived items from it are stored: the intake contract, the receipt, idempotency, time and role
stamping, the firewall gates, routing, the hand-off from the event log to projections, segmentation,
online extraction, durable derivation, the item schema, grounding, the prompt, entity resolution,
assertion identity, the integration operator, attachments, re-ingest, backpressure and cost. All of it
is the deposit operator $\mathcal{D}$, with a few edges into $\mathcal{K}_C$ and $\mathcal{R}$.

There are **26 questions (W-01 to W-26)**. The owner took the recommendation on **all 26**. Seven carry a
note that combines options (W-04, W-05, W-08, W-10, W-11, W-15, W-23); each is defined below and all
are compatible. Four answers from other areas change the reading of this one:

- **M-02** chose D, "two layers only (raw + facts)", with the note "B + D". This clashes with W-12, which
  deposits graph edges online. The proposed reading is below and needs the owner's confirmation.
- **G-19** chose A, a library-first inline runner, instead of three named modes. The durable queue (W-13)
  and the outbox (W-10) still work, as SQL tables drained by in-process workers.
- **M-18** chose A (Ladybug, else SQL adjacency), and **M-04** adds the note "SQL graph to neo4j". A graph
  backend outside the SQL transaction has to be fed through the outbox.
- **M-12** chose B, so unknown attributes default to coexist. This clashes with W-22's contest default.

Verdicts: 13 `ok`, 6 `ok-combined`, 4 `needs-measurement`, 3 `conflict`.

## Design in one page

| Step | What happens in v2 | Decisions |
|---|---|---|
| 1. Intake | One `SourceEnvelope` (conversation, document, tool log, asset) with ordered segments. Each segment becomes one raw L0 record. `write_messages`, `ingest` and `write_episode` become thin adapters. | W-01 |
| 2. Identity | Idempotency key: the caller's message id when given, otherwise a fingerprint of (namespace, source, segment index, content). The same id with new content becomes a new source version. | W-03, W-24 |
| 3. Stamping | `source_at` (nullable, never "now"), `source_tz` (declared or `unknown`), `recorded_at`. Speaker, addressee and the participant list go in typed columns, and the participant tag is still written. Provenance class comes from the role. | W-04, W-05, W-08 |
| 4. Role policy | Raw keeps every role for audit. Derivation treats assistant text as proposed, tool output as evidence and system text as excluded. The echo guard is on. | W-06 |
| 5. Firewall gate 1 | Screen the raw write. | W-07 |
| 6. Routing | Unkeyed content lands as episodic or resource raw. Typed records come only from derivation (with parents) or from explicit keyed writes. | W-09 |
| 7. Commit and receipt | One SQL transaction holds the event, SQL-resident projections, a derivation intent and outbox rows for every non-SQL index. The receipt returns the intent id and readiness per capability. | W-02, W-10 |
| 8. Segmentation | Adaptive window plus a time-gap cut. Closed windows are batched by tokens or age into one call, each window kept as its own numbered block. Per-turn mode stays a setting. | W-11 |
| 9. Durable queue | A leased outbox records membership, source versions, attempts, lease and state. Work is acknowledged only after the deposit commits. Under load, windows are coalesced and marked underived; nothing is dropped. | W-13, W-25 |
| 10. Online extraction | Runs asynchronously after the receipt. Readers may pass `wait_for_pending`, a bounded barrier. LLM merges stay at sleep. | W-12 |
| 11. Prompt | v2, example-free (about 353 tokens): NEW lines, CONTEXT lines, KNOWN names, the same-language and coarse-date partials, a soft length warning, and a prompt hash on every item. Firewall gate 2 keeps quarantined turns out of the prompt. | W-16, W-18, W-07 |
| 12. Validation | Constrained decoding (P-04), then per-item validation. One repair re-ask for invalid items. Failures are classified and kept in a dead-letter list, and truncation reschedules the remaining lines. | W-14 |
| 13. Item to envelope | The model emits ItemV1 (the B list). Code compiles it into the stored AssertionEnvelope with spans, asserter and subject, times, source class and an optional graph projection. | W-15 |
| 14. Grounding | Spans per assertion and per endpoint, plus rule checks on numbers, polarity and names. Only ambiguous bindings go to the adjudicator. The assent check promotes an item to user_confirmed. | W-17, W-08 |
| 15. Firewall gate 3 | Screen every derived item. Trust is always capped: $\tau_\Delta \le \min$ over its parents. | W-07 |
| 16. Resolution and key | Entity ladder online in code, with uncertain names marked `pending_merge`. Versioned assertion key with kind and occurrence. | W-19, W-20 |
| 17. Integration | The conflict ladder is seeded by kind and transition hints and still decides. The cardinality registry sets the default. Unresolved cases are deferred to $\mathcal{K}_C$. | W-21, W-22 |
| 18. Attachments | One AssetDescriber port, with an ingest trigger and an on-read trigger, both off by default. | W-23 |
| 19. Ledger | Per-call manifest, per-layer counters and an optional budget that pauses derivation (marks it underived) without dropping anything. | W-26 |

Interface sketches (one per major component, names only):

```text
SourceEnvelope{source_id, kind: conversation|document|tool_log|asset, version?, tz?, participants[],
               segments[{seg_index, caller_id?, role, speaker?, addressee?, source_at?, content, assets[]}]}
Receipt{event_ids[], intent_id, readiness (illustrative; fields not frozen, ER-06)}   status(intent_id) -> {layer: pending|done|failed}
OutboxRow{id, kind: projection|derivation, ns, source_id, source_version, members[], attempts, lease_until, state, failure_class?}
ItemV1{s?, r?, o?, text, kind: state|event|fact, mode, neg, when?, until?, lines[], span, source?}
  -> AssertionEnvelope{v:1, text, value, evidence[], asserter, subject[], spk, mode, polarity,
                       source_at, event:[start,end,precision], kind, occurrence?, src, tau, parents[], prompt_hash, graph?}
```

## Decisions

### W-01 Unit of a write call: D, typed source envelope, per-message raw
- **Decision:** one intake contract. A source carries ordered segments, and each segment is stored as its own L0 record. The existing doors become adapters.
- **Verdict:** ok. It is the base for W-02, W-03, W-04, W-09, W-23 and W-24, and it fits M-01 E (the log is the truth).
- **What will happen:** stamping, idempotency and versioning are done once, in one place. Line-level citations stay intact. No change to the number of model calls or to latency. Touches $\mathcal{D}$, episodic and resource.
- **Where:** `engine.py:1672` write, `engine.py:4270` write_messages, `engine.py:4368` _write_turns, `engine.py:4578` write_episode, `engine.py:11778` ingest, `memories/resource/store.py:54` -> `intake.SourceEnvelope` and `intake.IntakeGateway`.
- **Depends on / affects:** W-02, W-03, W-04, W-05, W-09, W-23, W-24; G-16 (API shape).
- **Migration:** the v1 verbs stay as adapters through the G-23 dual-read release, and v1 records replay as single-segment sources.
- **Test or gate:** every door produces identical stamps for the same input (a parity test), and the full v1 suite passes through the adapters.

### W-02 What a write receipt guarantees: D, raw searchable plus derivation intent
- **Superseded by ER-06 A:** durable acceptance plus per-capability readiness; raw search readiness is reported, not guaranteed at return.
- **Decision:** keep raw-searchable-at-return and persist a derivation intent in the same transaction. The receipt carries the intent id, and a status call reports pending, done or failed per layer.
- **Verdict:** ok. G-19 A does not affect it, because the intent is a SQL row.
- **What will happen:** a lost derivation stops being silent. Latency is about the same (one extra row; the measured hot path is 55-210 ms, G-W49). Touches $\mathcal{D}$, episodic.
- **Where:** `engine.py:13479` _append_and_project, `workers/windows.py:326` queue -> `intake.Receipt` and `derive.DerivationIntent`.
- **Depends on / affects:** W-01, W-10, W-13; P-21 (time-to-searchable SLO), O-06 (watermarks).
- **Migration:** v1 receipts gain the field. No data migration.
- **Test or gate:** kill the process after the receipt; on restart the intent is pending, and it ends as done exactly once.

### W-03 Idempotency key for retried writes: D, caller id first, fingerprint fallback, versioned
- **Decision:** use the caller's message id when present, otherwise the fingerprint of (namespace, source, segment index, content). The same id with different content becomes a new source version.
- **Verdict:** ok.
- **What will happen:** retries after a crash or a partial batch no longer duplicate turns, nor the facts derived from them. Edits become versions. Risk goes down sharply (RV01). Touches $\mathcal{D}$, episodic and resource.
- **Where:** `engine.py:1672`, `engine.py:4302` (up-front embedding), `engine.py:4977` (plain WRITE event) -> `intake.IdempotencyKey` and `intake.SourceVersioner`.
- **Depends on / affects:** W-01, W-24, W-13 (outbox keyed on source version).
- **Migration:** fingerprints are back-filled for v1 episodic records on replay. Existing duplicates are kept but linked as same-key.
- **Test or gate:** replaying the same batch twice gives zero new records. A same-id edit gives one new version and one lineage link.

### W-04 Times stamped on every raw input: C, three clocks plus declared zone (note "B + C")
- **Decision:** C contains B, so the combined design is B's three clocks plus C's declared zone. At intake:
  1. `source_at` comes from the segment, otherwise it stays null (never "now").
  2. `source_tz` comes from the segment, otherwise from the envelope, otherwise it is `unknown`.
  3. A naive time with an `unknown` zone is stored as wall-clock time flagged `tz_unknown`. It is no longer shifted by `read.naive_timezone`.
  4. `recorded_at` is always the engine clock.

  The event interval and its precision are filled at derivation. This matches M-10 "B + C" (tri-temporal on records, plus the four bi-temporal stamps on graph edges).
- **Verdict:** ok-combined.
- **What will happen:** backfilled histories keep their own dates, and relative phrases anchor to the cited utterance's zone. Readers must handle a null source time. Touches $\mathcal{D}$, episodic.
- **Where:** `engine.py:4478` (fallback chain), `engine.py:4506` _localize, `engine.py:4521` _event_time, `engine.py:4534` _sequenced, `config/schema.py:481` naive_timezone -> `intake.ClockStamper`.
- **Depends on / affects:** W-01, W-11 (time-gap cut), W-15; M-10, R-07, R-24.
- **Migration:** v1 `valid_from` maps to `source_at` only where v1 had a real message time. Records stamped by fallback get a null `source_at`, recoverable from the v1 MARKER or warning log where present; otherwise they keep `recorded_at` only.
- **Test or gate:** an undated backfill stays undated. A message from Ilse in UTC+9 saying "yesterday" resolves to the correct calendar day.

### W-05 Speaker, addressee and subject tagging: C, fields plus extracted asserter and subject (note "also keep participant tag too")
- **Decision:** raw records carry `spk` and `addr` as typed columns. Each extracted item carries the asserter and the subject, checked against the cited line's speaker. As the owner asked, **participants are kept as well**: a typed `participants[]` column on every raw record, always on (filled from the envelope, otherwise from the session's speakers so far). The v1 `participant:<name>` tag is still written during the dual-read period. Per-observer fan-out (D) stays with M-24 C.
- **Verdict:** ok-combined. Compatible with M-08 B (typed columns, tags kept for free labels).
- **What will happen:** separates "Bram said Ilse moved" from "Ilse said she moved", and allows "who was present" filters and visibility checks. Cost is a few output tokens per item. Touches $\mathcal{D}$, episodic and shared.
- **Where:** `engine.py:4434` (perspective tags), `config/schema.py:1740` write.participants, `workers/windows.py:199` -> `intake.RoleStamper` and `derive.AsserterSubjectCheck`.
- **Depends on / affects:** W-06, W-08, W-15; M-08, M-24, R-08 (field consumers, RV03).
- **Migration:** v1 `spk:`/`addr:`/participant tags are parsed into columns on replay.
- **Test or gate:** the asserter differs from the speaker only when a cited line supports it, and every new field has a named read consumer (RV03 check).

### W-06 Which roles and echoes enter memory: B, store all raw, role-aware derivation
- **Decision:** raw keeps every role. Derivation treats assistant text as proposed and tool output as evidence with its own trust, and excludes system text. The echo guard is on by default.
- **Verdict:** ok. It fits G-06 "A + C" (the chat exemptions stay available).
- **What will happen:** the loop where recalled memory is deposited again as new evidence ($\mathcal{G}\to\mathcal{D}$) closes, which protects N1. Fewer tokens reach extraction. Touches $\mathcal{D}$, episodic and working.
- **Where:** `config/schema.py:1444` skip_message_roles, `:1447` skip_injected_recall, `:1449` tag_assistant_claims, `engine.py:4417` MARKER -> `intake.RolePolicy`.
- **Depends on / affects:** W-05, W-07, W-08.
- **Migration:** a default change only. v1 stores keep their raw records.
- **Test or gate:** an injected recall block produces no new derived items (behavioural suite, G-04).

### W-07 Firewall placement on the derivation path: C, three gates, cap always on
- **Decision:** three gates. Gate 1 screens the raw write. Gate 2 filters the extraction input: quarantined turns are excluded and every input block is marked as data. Gate 3 screens every derived item, and $\tau_\Delta \le \min$ parent trust is always applied.
- **Verdict:** ok. The signal set comes from G-06 "A + C".
- **What will happen:** closes the path by which an extraction call launders a poisoned turn into a clean-looking fact. A false hold now also hides that turn's facts, a small quality risk. Touches $\mathcal{D}$, episodic, semantic and shared.
- **Where:** `engine.py:4670` _screen_write, `engine.py:4655` _screen_derived, `engine.py:4717` trust cap, `core/firewall.py:292` -> `firewall.WriteGates`.
- **Depends on / affects:** W-06, W-12, W-16; G-06, G-07, G-08.
- **Migration:** turning the cap on re-scores trust on replay. Report how many records change trust.
- **Test or gate:** the N1 reach-bound tests pass with online extraction on, and a poisoned turn yields no derived item above its own trust.

### W-08 Provenance class and confirmation at write: C, checked assent (note "combine B + C")
- **Decision:** two stages.
  - Stage 1 (B): the item's source label (user_stated, assistant_proposed, user_confirmed or tool) maps to $\mathrm{src}(m)$, and assistant_proposed is trust-capped.
  - Stage 2 (C): a code assent check promotes an item to user_confirmed only when a cited user line shows assent, not mere acknowledgement, and binds the proposal. Otherwise the item is downgraded.

  If the check cannot run, the B label stands, except that user_confirmed is downgraded. M-07 C's support count is added per record.
- **Verdict:** ok-combined.
- **What will happen:** assistant suggestions stop becoming user facts, and what the agent did or recommended is kept as proposals. Touches $\mathcal{D}$, semantic and reflective.
- **Where:** `core/firewall.py:292` role trust, `config/schema.py:1746` write.inferred, `:1272` inferred_gate -> `provenance.SourceClassifier` and `provenance.AssentChecker`.
- **Depends on / affects:** W-05, W-15, W-17; M-07, O-18 (profile ops read src).
- **Migration:** v1 derived records get the class `unknown`, with trust unchanged until re-derived.
- **Test or gate:** "ok, thanks" never promotes to user_confirmed, and a real assent does (labelled assent set, several languages).

### W-09 Routing writes to memory types: D, raw first, typed by derivation
- **Decision:** unkeyed content always lands as episodic or resource raw. Typed records come from derivation (with parents) or from explicit keyed writes (entity/attribute, skill, plan).
- **Verdict:** ok. It fits G-08 B (implicit parents) and M-02 (raw and facts are the core layers).
- **What will happen:** every typed record has lineage. Callers who relied on free-text `write()` defaulting to semantic see a behaviour change. Touches $\mathcal{D}$, all typed layers.
- **Where:** `engine.py:1672` (semantic default), `engine.py:4965` (semantic pipeline branch) -> `intake.TypeRouter`.
- **Depends on / affects:** W-01; G-08, G-13 (erasure cascade), G-23.
- **Migration:** in the shim, a v1 unkeyed semantic write becomes raw plus a derived fact, with a deprecation warning.
- **Test or gate:** no typed record without parents, unless it was written with an explicit key (lineage completeness check).

### W-10 Event log to projection hand-off: C, hot inline, heavy via outbox (note "Combine B + C")
- **Decision:** the default is C. SQL, lexical, and vector (when the embedding is cached) apply inline in the append transaction. Graph and derived projections go through a transactional outbox with idempotent apply and tombstone checks. B is kept as a mode, `projection.mode = outbox_all`, where every projection goes through the outbox, for high-throughput or server deployments. An external graph backend (Neo4j, per the M-04 note) is always outbox-driven, because it cannot join the SQL transaction. Under G-19 A the outbox is drained by in-process workers; there is no broker.
- **Verdict:** ok-combined.
- **What will happen:** in the default mode, raw is durable at return and its index readiness is reported (ER-06), and an online graph projector does not slow writes. In `outbox_all` mode writes are faster but raw becomes searchable only after the worker runs. Touches $\mathcal{D}$, episodic and associative.
- **Where:** `engine.py:13479` _append_and_project, `engine.py:13576` _projection_batch, `core/replay.py:34` catch_up -> `log.ProjectionRouter` and `log.Outbox`.
- **Depends on / affects:** W-02, W-12, W-13; M-04, M-18. M-18 A dropped the capability contract, but the outbox apply still needs a minimal per-backend contract (idempotent upsert by assertion id, tombstone check). See the open points.
- **Migration:** the projector offsets of v1 become outbox rows, and `catch_up` replays any gap.
- **Test or gate:** crash between append and graph apply; after restart the graph matches a full rebuild. Both modes pass the same projection-equivalence test.

### W-11 Segmentation of turns for extraction: E, adaptive window plus time gap (note "B + E")
- **Decision:** E sets the boundaries: topic shift, 6 turns, 1,500 tokens, 120 s idle, session end, plus a new cut on source-time gaps (learned per store) or on a calendar-day change. B is a dispatch stage on top: closed windows accumulate until a token cap or a maximum age, then one call carries them as separately numbered blocks. Windows never merge into one segment, so citations stay window-precise. Per-turn mode stays a setting.
- **Verdict:** needs-measurement. The batching stage must show fewer calls without a loss in grounding.
- **What will happen:** fewer calls than one per window of about 186 tokens (estimate). Time-to-searchable rises by up to the batch's maximum age. The time-gap cut fixes long silences inside one session id. Touches $\mathcal{D}$, episodic.
- **Where:** `core/window.py:128` WindowSegmenter, `core/window.py:197` segment, `config/schema.py:1758-1767` window_* -> `derive.Segmenter` and `derive.CallBatcher`.
- **Depends on / affects:** W-04 (source time), W-12, W-25; M-14 (episodes), P-14, P-23 (the pre-pass skips empty windows).
- **Migration:** settings only. The batching cap starts off until the screen passes.
- **Test or gate:** see the measurements table (calls per turn down; grounding and recall inside the noise band).

### W-12 Online extraction timing: D, async deposit plus read barrier
- **Decision:** facts and graph items deposit right after the write, through the shared emit. A read may pass `wait_for_pending` (with a bounded timeout). LLM merges stay at sleep.
- **Verdict:** conflict with **M-02**. M-02 D says "drop … graph", but M-04, M-16, M-18 and W-19/W-20 keep a graph. Proposed reading of the M-02 note "B + D": the layer field (B) exists, and the always-built layers are L0 raw and L1 facts (D). The graph is a **code-compiled projection of L1 facts** (W-15's optional graph projection), not a separately LLM-derived layer, so online edges deposit together with their facts. Needs owner confirmation.
- **What will happen:** edges become fresh within seconds instead of after sleep, with no slower write. A barrier read pays up to its timeout (a 9B call measured about 2.7 s, G-W49). Touches $\mathcal{D}$ and $\mathcal{R}$, semantic and associative.
- **Where:** `config/schema.py:1774` window_async, `workers/windows.py:608` take_edges, `workers/windows.py:808` pending buffer, `workers/pipelines.py:1983` extract_graph, `engine.py:13757` flush_windows -> `derive.OnlineExtractor` and `read.PendingBarrier`.
- **Depends on / affects:** W-02, W-11, W-13, W-19, W-20; M-02, O-06, O-10, P-21.
- **Migration:** v1 edges still pending at sleep are drained before the cut-over.
- **Test or gate:** p95 turn-to-searchable-edge and queue delay at stated load (G-W49, RV10). The barrier returns within its timeout with a status.

### W-13 Durable derivation queue: C, leased outbox, at-least-once, idempotent effects
- **Decision:** persist membership, source versions, attempts, the lease and the result state. Deposit idempotently, then acknowledge. Backlog age and size are capped.
- **Verdict:** ok. G-19 A (inline runner) changes how it runs, not whether it is correct. The outbox is a table in the local SQL store drained by an in-process worker, and the lease detects a crashed holder rather than arbitrating between many workers. Option D (dbos or taskiq) stays an adapter.
- **What will happen:** no silent gaps in derived layers after a restart. A retried call costs one repeated model call. Touches $\mathcal{D}$, semantic, associative and episodic.
- **Where:** `workers/windows.py:326` deque, `:554` stop, `:568` jobs_abandoned, `:608` take_edges (clears before ack), `:808` pending, `config/schema.py:265` WorkersConfig -> `derive.DerivationOutbox`.
- **Depends on / affects:** W-02, W-10, W-14, W-20 (versioned keys make the deposit idempotent), W-25; O-03, O-10.
- **Migration:** v1 has no persisted jobs. On first start, every source after the last derived watermark is enqueued.
- **Test or gate:** RV01 fault injection after inference and after a partial deposit; each yields exactly one deposit after restart.

### W-14 Malformed output, retries and dead letters: C, per-item repair plus persisted failure state
- **Decision:** keep the valid items and re-ask once for the invalid ones, using the validator's errors. Record the failure class (raised, malformed, valid-empty, truncated, size skip, partial commit), the attempts and whether a retry is allowed. A truncated reply schedules its remaining lines. A dead-letter list is kept for review.
- **Verdict:** ok. It matches P-04 C (constrained decoding first), P-05 D and P-11 D.
- **What will happen:** valid items survive a bad sibling, at the cost of one bounded extra call that should be rare under constrained decoding. Touches $\mathcal{D}$, semantic and associative.
- **Where:** `workers/windows.py:199` validate_facts, `:236` validate_edges, `:375` and `:618` auto-split, `config/constants.py:507` WINDOW_SPLIT_FAIL_RATE -> `derive.ItemValidator`, `derive.RepairAsk` and `derive.FailureLedger`.
- **Depends on / affects:** W-13, W-15; P-04, P-05, P-11.
- **Migration:** none. v1 jobs_failed counters map onto failure classes going forward.
- **Test or gate:** G-P4: sections_dropped goes down with no new ungrounded items, and a valid empty reply is not counted as a failure (RV19).

### W-15 Schema of an extracted item: C, versioned assertion envelope (note "B + C")
- **Decision:** B is the **wire schema** the model emits (the compact G-W49 item list). C is the **stored envelope**, compiled in code from the B item plus the intake fields (`spk`, `source_at`, `source_tz`, source class, evidence spans, prompt hash) and an optional graph projection. B is declared envelope v1, and the model never emits the full envelope.
- **Verdict:** ok-combined. It matches M-06 B.
- **What will happen:** one contract for facts and the graph. No duplicate relations, and literal and n-ary facts are stored without fake entity nodes. Replies are shorter. Touches $\mathcal{D}$, semantic and associative.
- **Where:** `workers/windows.py:199`, `:236` (two validators), `workers/pipelines.py:1882` _edge_key, `engine.py:13794` _deposit_window_episode -> `schema.ItemV1`, `schema.AssertionEnvelope` and `derive.EnvelopeCompiler`.
- **Depends on / affects:** W-04, W-05, W-08, W-16 to W-22; M-06, M-16, M-26 (schema versioning).
- **Migration:** v1 facts and edges are re-shaped into envelopes on replay, with fields not present in v1 left null.
- **Test or gate:** every envelope field has a named consumer (RV03), and an unknown enum value fails closed per item.

### W-16 What the extraction call is shown: B, NEW + CONTEXT + KNOWN names
- **Decision:** a few previous lines are shown as CONTEXT (never extracted from), plus one KNOWN line of entity names for the window's subjects.
- **Verdict:** ok. It is the G-W49 prompt v2 shape. KNOWN can be fed by the P-23 GLiNER2 pre-pass and the online resolver. Note that it supplies no `replaces` alias (see W-21).
- **What will happen:** fixes pronouns and name drift across windows, for a small input-token cost. Touches $\mathcal{D}$, semantic and associative.
- **Where:** today the call sees only its own turns (`workers/windows.py`, citation check at `:148`) -> `derive.PromptContextBuilder`.
- **Depends on / affects:** W-15, W-19, W-21; P-09, P-23.
- **Migration:** none.
- **Test or gate:** coreference across windows improves on the free screens, and no item cites a CONTEXT line.

### W-17 How extracted items are grounded: C, rule checks plus selective adjudication
- **Decision:** a span per assertion and per endpoint. Numbers, units, qualifiers, negation and the bound names must appear in the cited evidence. Only ambiguous bindings (two people in one line) go to a small local entailment model or the decider. Failing items are kept as uncertain, not promoted.
- **Verdict:** needs-measurement. Rules and the adjudication trigger need a labelled cross-domain set.
- **What will happen:** quotes that support another person, the opposite polarity or a different number stop passing. Model time is spent on a minority of items (estimate). Touches $\mathcal{D}$, semantic and associative.
- **Where:** `workers/windows.py:148` grounded, `:199`, `:236` -> `derive.GroundingChecker` and `derive.Adjudicator`.
- **Depends on / affects:** W-15, W-08, W-05; P-23.
- **Migration:** v1 facts are not re-graded; new deposits only. They can be re-graded optionally at re-derive (P-25).
- **Test or gate:** grounding of at least 95% (G-W49) on the labelled set, with the adjudication share reported.

### W-18 Extraction prompt rules and versioning: C, v2 plus shared partials, soft caps
- **Decision:** prompt v2 (about 353 tokens), plus `same_language.j2` and a coarse-date-as-span partial in every write prompt. Length is a soft warning. Every derived item carries the prompt hash and version, and gold-lint covers the partials.
- **Verdict:** needs-measurement. RV15 requires each rule to be screened separately.
- **What will happen:** fixes the language, coarse-date and assent gaps. Fewer input tokens than v1's 958 (the 353 count is unverified, RV10). Touches $\mathcal{D}$, semantic and associative.
- **Where:** `workers/pipelines.py:2912` _fact_date (day-one parsing) and the window prompt in `prompts/` -> `prompts.write_extract` v2, `prompts/_partials` and `prompts.PromptIdentity`.
- **Depends on / affects:** W-15, W-26; P-07, P-10, P-11, P-12, P-13.
- **Migration:** v1 items carry the prompt id `v1` and are re-derived only under P-25.
- **Test or gate:** English output unchanged on the focus slice per partial, and the token count reproduced in a manifest.

### W-19 Entity resolution on the write path: B, online code resolution, pending merges
- **Decision:** the same ladder with `llm=None` and vectors cached per namespace (bounded). Uncertain new names are stored as `pending_merge` for LLM reconciliation at sleep. Homonyms are kept apart by type and context.
- **Verdict:** needs-measurement. RV17 requires the thresholds to be calibrated. It also depends on the M-02 reading (W-12), although facts need resolved names for their keys whether or not the graph stays.
- **What will happen:** fewer duplicate entities between sleeps, with no added model call. Touches $\mathcal{D}$ and $\mathcal{K}_C$, associative and semantic.
- **Where:** `memories/associative/resolution.py:164` EntityResolver, `:183` resolve, `:122` _minhash -> `graph.OnlineResolver`.
- **Depends on / affects:** W-15, W-16, W-20; O-10 "A + B", O-11 "A + B + C".
- **Migration:** none. The v1 alias table is reused.
- **Test or gate:** separate MinHash and cosine thresholds on held-out alias and homonym pairs, with no homonym merges above the stated rate.

### W-20 Identity key of a graph assertion: C, versioned assertion id plus event resolution
- **Decision:** the key is (ns, src, rel, dst, kind) plus an occurrence id from the cited turns, with a key version and old-key aliases. Assertion identity, event occurrence and semantic dedup are kept separate. A re-mention attaches as support when its date and qualifiers match.
- **Verdict:** ok. M-17 B is the storage key; W-20 C adds the occurrence resolver on top, as M-17's rationale allows.
- **What will happen:** A then B then A returns to A, two same-day events stay two, and re-mentions corroborate. The risk is in the migration. Touches $\mathcal{D}$, associative, episodic and semantic.
- **Where:** `workers/pipelines.py:1882` _edge_key -> `identity.AssertionKey` and `identity.OccurrenceResolver`.
- **Depends on / affects:** W-15, W-13 (idempotent deposit), W-21; M-17, M-26, G-23.
- **Migration:** replay with old-key aliases. A replay of an old-key store creates no duplicates (RV05).
- **Test or gate:** A-B-A returns to A, same-day events are kept apart, and reruns are idempotent.

### W-21 Who chooses the integration operator: B, ladder seeded by extraction hints
- **Decision:** kind, a `replaces` alias and a "changed from A to B" transition seed the ladder. The ladder still decides and names the rung, and supersede happens only at cardinality one.
- **Verdict:** conflict with **W-16**. The recommendation assumed W-16 C for the `replaces` alias, but W-16 is B. Proposal: ship with the reduced hint set (kind, transition, cardinality) and add `replaces` once W-16 C passes its gate. When there are no hints, defer to $\mathcal{K}_C$ (option D as the fallback).
- **What will happen:** a small gain now and more once `replaces` lands. The ladder stays the deterministic N1 guard. Touches $\mathcal{D}$ and $\mathcal{K}_C$, semantic and associative.
- **Where:** `core/policies/conflict.py:91` ConflictPolicy, `:144` decide, `workers/pipelines.py:2015`, `memories/semantic/write_pipeline.py:141` -> `integrate.ConflictLadder`.
- **Depends on / affects:** W-15, W-16, W-20, W-22; M-12, O-12, O-14.
- **Migration:** none. Rung names are stored on new verdicts.
- **Test or gate:** knowledge-update slices show no loss, and every supersede names its rung and its hint.

### W-22 Default when cardinality is undeclared: D, kind-aware default
- **Decision:** events and facts coexist. A state supersedes only when its relation is declared or learned to be one-valued, and contests otherwise.
- **Verdict:** conflict with **M-12**. M-12 B says unknown attributes default to many (coexist). Proposal: the M-12 registry is the single authority (unknown means coexist). Learned cardinality enters the registry only as reviewable proposals. Contest applies to an undeclared state only when the extractor marks a transition. Needs owner confirmation.
- **What will happen:** a second employer or pet no longer silently retires the first. Readers must present coexisting and disputed values. Touches $\mathcal{D}$ and $\mathcal{K}_C$, semantic and associative.
- **Where:** `core/policies/conflict.py:60` (contest option), `workers/pipelines.py:2015` (declared cardinality) -> `integrate.CardinalityRegistry` and `integrate.DefaultVerdict`.
- **Depends on / affects:** W-21; M-12, O-14, R-20.
- **Migration:** v1 operator-declared cardinality is loaded into the registry.
- **Test or gate:** an undeclared second value is not silently retired, and a declared one-valued state supersedes with history.

### W-23 Images and attachments at ingest: B, opt-in description (note "B + C")
- **Decision:** one AssetDescriber port with two triggers, both off by default (the BD-8 hold).
  - Ingest (B): a local vision model writes a caption as a derived resource record, with the asset hash, the model and the prompt as provenance.
  - On read (C): when a read needs unresolved visual evidence, the same port describes the asset and deposits the caption the same way, so it is paid once.

  An ingest caption wins. A read never re-describes an asset that already has a caption for the same model and prompt hash.
- **Verdict:** ok-combined. Scope later. The read trigger needs a hook in the R area.
- **What will happen:** visual evidence becomes searchable (ingest) or answerable (read). The ingest trigger costs a vision call per asset; the read trigger costs one only when used. Touches $\mathcal{D}$ and $\mathcal{R}$, resource and episodic.
- **Where:** `config/schema.py:1780` IngestConfig, `:1799` asset_vision, `engine.py:3031` _attachment_refs, `engine.py:4464` -> `intake.AssetDescriber`.
- **Depends on / affects:** W-01, W-07 (captions are derived and trust-capped); R-area read hook.
- **Migration:** none. v1 `asset:<id>` tags stay as identity.
- **Test or gate:** stays off until the owner lifts BD-8. Then: caption provenance complete, and no duplicate captions per hash.

### W-24 Re-ingesting an edited document: C, chunk diff on source versions
- **Decision:** each edit is a new source version event. Derived items record their version and are retired or re-pointed when it is superseded. As-of reads see old versions.
- **Verdict:** ok. Scope later.
- **What will happen:** stale facts from old versions stop lingering, and re-ingest is much cheaper. Document edits and message edits use the same mechanism. Touches $\mathcal{D}$ and $\mathcal{K}_C$, resource and semantic.
- **Where:** `memories/resource/store.py:54` ingest, `engine.py:11778` -> `intake.SourceVersioner`.
- **Depends on / affects:** W-03, W-13; M-11, G-13.
- **Migration:** existing resource records become version 1 of their source.
- **Test or gate:** editing one paragraph re-extracts only the changed chunks, retires the old facts, and leaves the as-of read unchanged.

### W-25 Backpressure when derivation falls behind: B, coalesce and mark underived
- **Decision:** adjacent pending windows of a session merge up to the token cap. Beyond the age or size caps, sources are marked underived and picked up by catch-up or sleep. Nothing is dropped.
- **Verdict:** ok. It fits O-10 "A + B" and W-11's batching stage.
- **What will happen:** derived freshness degrades under load but coverage does not. This replaces drop-oldest above 5,000 pending edge batches. Touches $\mathcal{D}$ and $\mathcal{K}_C$, episodic and semantic.
- **Where:** `workers/windows.py:808-812` (drop oldest), `config/constants.py:511` WINDOW_PENDING_EDGES_MAX, `engine.py:4302` -> `derive.Backpressure`.
- **Depends on / affects:** W-11, W-13, W-26; O-10.
- **Migration:** none.
- **Test or gate:** a load test at twice the extraction rate drops zero sources, and queue age and write amplification are reported.

### W-26 Write-side cost ledger and budget: D, manifest plus enforced budget
- **Decision:** per-layer counters, plus a per-call manifest (prompt hash, tokenizer, template, cache state, tokens, retries, queue delay, p50/p95 turn-to-searchable), plus an optional budget per namespace and period that pauses extraction and marks sources underived. The budget is off by default.
- **Verdict:** ok. It is the same ledger as P-22 C; build one component, not two.
- **What will happen:** cost claims become reproducible (RV10, RV20), and hosted extractors get a hard ceiling. Touches $\mathcal{D}$, $\mathcal{K}_C$ and $\mathcal{K}_P$, all derived layers.
- **Where:** `engine.py:13788` window_stats, `engine.py:13257` model_usage -> `ledger.WriteCostLedger` and `ledger.Budget`.
- **Depends on / affects:** W-18, W-25; P-12, P-14, P-22, G-25.
- **Migration:** v1 counters map onto ledger fields.
- **Test or gate:** a manifest reproduces a run's token count, and a spent budget drops nothing.

## Conflicts and open points

| id | issue | proposal | needs |
|---|---|---|---|
| W-12 (M-02) | M-02 D drops the graph, but W-12 deposits graph items online, and M-04, M-16, M-18, W-19 and W-20 keep a graph. | Read M-02 "B + D" as: a layer field exists, L0 raw and L1 facts are the always-built layers, and the graph is a code-compiled projection of L1 facts (no separate LLM layer). | owner confirmation |
| W-22 (M-12) | An undeclared state contests (W-22 D), but unknown attributes coexist (M-12 B). | The M-12 registry decides (unknown means coexist). Learned cardinality is only a reviewable proposal. Contest applies only on an extracted transition. | owner confirmation |
| W-21 (W-16) | The `replaces` hint assumed W-16 C, which was not chosen. | Ship the hints without `replaces`, and add it when W-16 C (advanced) passes its knowledge-update gate. | none (sequencing) |
| W-10 (M-18, M-04) | M-18 A dropped the capability contract, but the outbox apply needs idempotent upsert and tombstone checks per backend, including Neo4j (the M-04 note). | A minimal projector contract inside W-10 (upsert by assertion id, tombstone check, rebuild from the log), applied to Ladybug, SQL adjacency and Neo4j. | owner awareness; the M area should confirm |
| W-13, W-10 (G-19) | G-19 A means no named server mode or worker process. | The outbox and queue are SQL tables drained by in-process workers, and the lease covers crash recovery. External runners (dbos, taskiq) and a separate worker stay adapters. | none |
| W-05 | Should participants be on by default, or opt-in as in v1? | On by default as a typed column, with the v1 tag also written. This reads the owner's "keep participant tag too" as always-on. | none unless the owner disagrees |
| W-26 / P-22 | The same ledger is decided twice. | One `ledger` component owned by P-22, and W-26 adds the write-side fields. | none |

## Measurements to run before building

| what | which screen or dataset | gate |
|---|---|---|
| W-11 B-batching on top of the E windows | free retrieval-only screens plus a labelled cross-domain extraction set (invented names) | calls per turn down, with grounding and fact recall inside the noise band (G-01, G-03) |
| W-11 time-gap cut | the same screens, sliced on sessions with long internal silences | no loss on other slices; better on the gap slice |
| W-17 grounding rules and adjudication trigger | a labelled cross-domain grounding set, several languages | grounding of at least 95% (G-W49), with the adjudicated share of items reported |
| W-18 each prompt partial (same-language, coarse date, soft length) | free screens, focus slice, plus a multilingual set | English output unchanged on the focus slice and grounding unchanged (RV15); the 353-token count reproduced (RV10) |
| W-19 online resolver thresholds | held-out alias and homonym pairs (RV17) | MinHash and cosine calibrated separately; homonym merge rate under the stated cap |
| W-12 online deposit and barrier | a load test at stated rates | p95 turn-to-searchable-edge, queue delay and barrier timeouts reported (G-W49, RV10, P-21) |
| W-13 durable queue | fault injection after inference and after a partial deposit (RV01) | exactly one deposit after restart and no lost windows |


## Owner follow-up (2026-10-12)

- **Q1 Layer field, two layers on** (as proposed): Every record has an explicit `layer` field; raw (L0) and facts (L1) are populated by default; episodes, summaries, graph, profile, observations and communities are opt-in layers.
- **Q2 Durable outbox** (as proposed): Library-first inline runner draining a durable outbox in the same store; leftover work resumes on the next open; dbos and taskiq are optional drainers.
- **Q4 Neo4j is the server graph** (changed): Embedded profile: auto Ladybug else SQL adjacency (M-18 A). Server profile: Postgres + pgvector + Postgres full text for records, Neo4j for the graph, fed only through the outbox. W-10: the Neo4j projection is always fed through the outbox, never inline.
- **Q7 Coexist unless marked** (as proposed): Undeclared attributes coexist (registry default); a new value contests the old one only when extraction marks a change; learned cardinality only as reviewable proposals.

## Change log

- 2026-10-11: first version. All 26 write-path answers verified (13 ok, 6 ok-combined, 4 needs-measurement, 3 conflict with M-02, M-12 and W-16); the seven owner notes are defined, and the cross-area effects of M-02, G-19, M-18/M-04 and M-12 are recorded.
- 2026-10-12: owner follow-up answers recorded (Q1, Q2, Q4, Q7).
- 2026-10-13: reconciled with ER amendments and owner decisions (consistency audit)
