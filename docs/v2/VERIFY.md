# MemSpine v2: verification of the owner's answers

Six area reviews checked every answer against the owner's other answers, the v1 code and the measured results
(2026-10-11). Per-question detail: `areas/<area>.md`; machine-readable: `data/verify_<area>.json`.

| Verdict | Count | Meaning |
|---|---|---|
| ok | 100 | Holds as answered |
| ok-combined | 22 | A combining note ("A + B") read as a staged, fallback or mode design; compatible |
| needs-measurement | 17 | Decided, but a screen or dataset run must pass before it is built or made default |
| conflict | 13 | Clashes with another answer; a reconciling reading is proposed (most resolve with the owner points below) |
| needs-owner | 3 | Cannot be settled without the owner |

## 0. Owner answers to Q1-Q9 (2026-10-12)

| # | Answer | As proposed? | What it settles |
|---|---|---|---|
| Q1 | **Layer field, two layers on**. Every record has an explicit `layer` field; raw (L0) and facts (L1) are populated by default; episodes, summaries, graph, profile, observations and communities are opt-in layers. | yes | M-02, W-12, R-09, R-10, O-08, O-16 |
| Q2 | **Durable outbox**. Library-first inline runner draining a durable outbox in the same store; leftover work resumes on the next open; dbos and taskiq are optional drainers. | yes | G-19, P-21, W-02, W-10, W-13, W-25, O-01, O-02, O-03, O-10, P-25, G-12, G-14, G-17 |
| Q3 | **Value histories plus pattern observations**. Build the deterministic value-history view AND an observation layer for recurring patterns only (code-grouped, model-worded only for groups that changed). O-15 is reopened from 'none' to 'patterns only'; observations are an opt-in L4 layer (Q1). | **no, changed** | M-13, O-15, O-04, O-05, O-21, R-17, M-19 |
| Q4 | **Neo4j is the server graph**. Embedded profile: auto Ladybug else SQL adjacency (M-18 A). Server profile: Postgres + pgvector + Postgres full text for records, Neo4j for the graph, fed only through the outbox. | **no, changed** | M-04, M-18, W-10, G-13, M-25, M-05, R-09 |
| Q5 | **Jina on every read if installed**. Jina v3.5 reranks every read wherever the model is installed; the pre-run gate is a CPU-only mode; the eval harness forces it on. | yes | R-14, P-18 |
| Q6 | **Jina if installed, permissive fallback**. Jina stays for research and evaluation; the library default is 'Jina if installed', with a permissively licensed reranker named as the commercial fallback (licence check is a research task). | yes | R-14, P-17, P-18 |
| Q7 | **Coexist unless marked**. Undeclared attributes coexist (registry default); a new value contests the old one only when extraction marks a change; learned cardinality only as reviewable proposals. | yes | W-22, M-12 |
| Q8 | **Assistant exemption in the chat preset only**. Base firewall signals on every channel, extended and semantic on top; the assistant-role exemption only in the chat data-shape preset. | yes | G-06 |
| Q9 | **v2 stays inside `memspine`**, same package and principles (2026-10-11) | changed from the proposal | G-16, G-23, G-25 |

Two answers differ from the proposal and add work:

- **Q3 pattern observations:** O-15 moves from 'none' to 'patterns only'. A sleep stage `observe_patterns` runs after value histories: code groups records by (subject, predicate family) and recurrence, and the model words only groups that changed since the last cycle, citing every member. Needs measurement before default: OP-Bench, PrefEval and MAB-CR non-inferior, sleep calls per turn within the O-05 budget (estimate: under 0.05 extra calls per turn when worded only on change).
- **Q4 Neo4j server graph:** the server profile spans two systems (Postgres for records, Neo4j for the graph). Required with it: a real Neo4j adapter (v1's is a stub, `services/graph/neo4j.py:16-18`) fed only through the outbox (W-10); erasure through the reverse-lineage index must delete in Neo4j and prove it (G-13, M-25); scope keys in every Neo4j node and edge (M-05); encryption and backup per system; a rebuild of Neo4j from the retained event log (M-01). Gate: isolation, erasure and rebuild tests pass on the server profile before it ships.

Q1 amended 2026-10-13 (I-7): the graph layer L3 is also on by default when a worker model is configured.

## 1. Points for the owner (Q9 answered; Q1-Q8 settle 15 of the 16 conflict and needs-owner verdicts)

| # | Question | Proposed reading | Settles |
|---|---|---|---|
| Q1 | **M-02 "B + D"**: is the layer stack an explicit `layer` field on every record, with only raw (L0) and facts (L1) populated by default and every other layer (episodes, summaries, graph, profile, communities) opt-in, rather than those layers removed? | Yes: explicit field, two layers on by default, the rest opt-in per template | M-02, W-12, R-09, R-10, O-08, O-16 (and keeps M-14, M-16, M-19, M-20, O-07, O-17, O-19, O-21 valid) |
| Q2 | **G-19 A**: with the library-first inline runner, should derived work survive a crash through a durable local outbox in the same store (B in substance, without three named modes), or stay non-durable? | Durable local outbox drained by the inline runner; dbos and taskiq as alternative drainers | G-19, P-21, and keeps W-02, W-10, W-13, W-25, O-01..O-03, O-10, P-25, M-04, G-12, G-14, G-17 valid |
| Q3 | **M-13 vs O-15**: value histories only, or also an observation layer for patterns? | Value histories only now (M-13 half, O-14 B); observations stay specified, unbuilt, until O-15 is reopened | M-13, O-15 |
| Q4 | **M-04 note "SQL graph to neo4j" vs M-18 A**: in the server profile, should Neo4j replace the SQL graph, or be an optional adapter beside Postgres graph tables? (v1's Neo4j adapter is a stub, `services/graph/neo4j.py:16-18`.) | Embedded: auto Ladybug else SQL adjacency (M-18 A). Server: Postgres graph tables by default, Neo4j optional adapter through the outbox | M-04, M-18 |
| Q5 | **R-14 vs P-18**: Jina v3.5 on every read where installed, or only when a pre-run gate fires? | On every read where the model is installed (matches the 81.4% reference); the gate is a CPU-only mode; the harness forces always-on | R-14, P-18 |
| Q6 | **Jina licence**: jina-reranker-v3.5 weights are CC-BY-NC-4.0 and need `trust_remote_code`, while MemSpine is Apache-2.0. Keep it as the default for a commercial-use library? | Keep Jina for research and evaluation; ship the default as "Jina if installed", with a permissive fallback named in the docs (needs a licence check) | R-14, P-17/P-18 (research) |
| Q7 | **W-22 vs M-12**: undeclared attributes coexist (registry default) and a change only contests when extraction marks it as a change? | Yes: the M-12 registry decides; learned cardinality only as reviewable proposals | W-22, M-12 |
| Q8 | **G-06 "A + C"**: put the assistant-role bridge exemption in the chat preset rather than the global default? | Yes | G-06 |
| Q9 | **Separation** | **Answered by the owner (2026-10-11):** v2 stays inside `memspine`, same package and principles; plan in `docs/v2/`; components land behind flags; `memspine.compat.v1` aliases for one release | G-16, G-23, G-25 |

Not an owner point: **W-21** (`replaces` hint) simply waits for W-16 C, which was not chosen; the ladder is seeded
by kind, transition wording and cardinality instead.

## 2. Combined answers (22), as read

The recommended option stays the default and the other becomes a base layer, first stage, fallback or mode:

| Ids | Reading |
|---|---|
| W-04, W-08, W-15 | The chosen option already contains the other (W-15: the model emits the short B item list; code compiles it into the stored C envelope) |
| W-05 | Typed participant list on every raw record, and the v1 participant tag still written |
| W-10 | Split (hot inline, heavy via outbox) is default; "everything through the outbox" is a mode; an external graph always goes through the outbox |
| W-11 | Adaptive window + time-gap cut sets boundaries; token batching sends several closed windows in one call, each its own block |
| W-23 | One image-description component, two triggers (ingest, read), both off by default |
| M-10 | Tri-temporal record; Graphiti-style edge timestamps are a view of the same fields |
| M-16 | Assertion-centric relations plus untyped links (temporal, semantic, causal) as a second projection |
| M-19 | Communities stay; entity summaries added as an extra grouping kind |
| O-07, O-09 | Call 2 at session close is default; sleep re-runs a session task only when call 2 failed or never ran; window fragments stay searchable beside the stitched episode |
| O-10 | Persisted backlog with an early pass on size/age caps, and every sleep drains the rest |
| O-11, O-12 | Staged ladders: rules, then scoped candidates, then a batched LLM call; rules-only mode when no model or no budget |
| O-14, O-18, O-23, O-24 | Today's mechanism stays as the base (superseded records, caller persona, stated plans, template lessons); the recommended addition sits on top |
| R-01 | Config sets the default tier and the allowed tiers; the per-call `depth` overrides within that |
| R-11 | Correlated legs merged within a family first, then RRF k=60 across families; plain RRF when only the core legs run |
| G-06 | Today's three firewall signals on every channel as the base; extended and semantic signals on top |
| P-18 | Jina is the model; the gate decides when it runs (see Q5) |

## 3. Measurements to run before building (17 decisions)

| Id | Measurement | Gate |
|---|---|---|
| R-03 | contract-jina-full (1,540, queued) vs clean-jina-full 81.4, then MAB-CR and ConvoMem | Paired gate PROMOTE on the full set, then CONFIRMS blind (focus slice: +15/-7, net +8, HOLD under the new gate) |
| R-22 | New `f-suff` focus screen vs f-jref2-i2 | Net > about +7, no category loss, wall time at most 2x (the slot loop was net -3 at about 8x) |
| R-12 | Source-family pool vs per-leg pool on the focus slice | Non-inferior within band, rerank pairs not higher |
| R-18 | Block shares vs none at equal budget (the early-break fix needs no screen) | No QA loss, tokens down |
| R-21 | Relevance gate on OP-Bench, MAB-CR, ConvoMem, LoCoMo | OP-Bench up, no recall loss beyond band; fired rate reported |
| W-11 | Token batching on top of windows | Calls per turn down, grounding inside band |
| W-17 | Labelled cross-domain grounding set (invented names, several languages) | Grounding at least 95% |
| W-18 | Each prompt partial on its own (same language, coarse dates, length warning) | English output unchanged on the focus slice |
| W-19, O-13 | Threshold calibration on alias/homonym and paraphrase/numeric pairs | No homonym or numeric merges above a stated rate |
| P-01 | Worker-tier model screen per role | Grounded facts not down beyond band |
| P-04 | Schema-constrained vs prompted output on the window arm | Items dropped down, grounding not worse |
| P-10, P-17, M-23 | Multilingual embedder vs bge-small (retrieval-only LoCoMo, ConvoMem) plus a non-English probe | English R@k within band |
| P-23 | GLiNER2 pre-pass on/off on the window arm | Calls down, grounded facts not down |
| M-22 | Multi-resolution text on the focus slice and one blind run | Context tokens down at no QA loss |

Every measurement uses the hardened gate (G-01, G-E1): paired against a named reference, all in-scope sets present,
per-conversation uncertainty, and one sealed blind run before adoption. `eval_focus.py` and `eval_blind.py` now use the paired gate (G-E1, built 2026-10-12).

## 4. Facts from 2026-10-11 that the plan relies on

- v1 frozen reference: clean-jina-full **81.4%** on 1,540 LoCoMo cat 1-4 (multi-hop 66.7, temporal 83.2, open 42.7, single 90.0).
- Screens: f-contract (A03) net +9 on the focus slice at no extra cost; f-chain net -1; f-slot net -3 at about 8x time;
  re-running the reference after the environment change gave identical answers on all 437 questions.
- P0 contamination fixed in v1 (`11853ed`): single-word gold answers had been used as prompt examples; the lint now
  covers single words, partials and harness prompts (v2 P-13).
- Environment: a stray `uv sync` broke the runs (huggingface_hub 1.16.1); the lock file still pins it and must be
  regenerated before per-run environments (G-24) can be built.

## Change log
- 2026-10-11: first verification of all 155 answers across six areas.
- 2026-10-11 (later): Q9 answered by the owner (v2 inside `memspine`); lock file bumped (huggingface-hub 1.33.0, commit 1d15194), so the G-24 prerequisite is met.
- 2026-10-12: owner answered Q1-Q8 (six as proposed; Q3 pattern observations and Q4 Neo4j server graph changed).
- 2026-10-12: G-E1 paired gate built in v1; A03 focus result re-scored as HOLD (net +8, sign p 0.134).
- 2026-10-13: Q1 amended (graph on by default); pipeline, runtime and modularity decisions recorded in `pipelines/README.md`.
- 2026-10-13: reconciled with ER amendments and owner decisions (consistency audit)
