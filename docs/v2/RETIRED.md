# Retired features (ER-40 retirement record)

One row per item removed by F-14a under G-22 (flag graduation and retirement) and ER-40 (retire the
implementation burden; keep the evidence and the rationale). There are no compatibility shims
(ADR-067): a retired key or value in YAML raises `ConfigError` naming the replacement
(`RETIRED` and `RETIRED_VALUES` in `src/memspine/config/flags.py`). Every item was default-off, so the
simple-profile defaults and the read goldens are unchanged. Experiments that used these keys stay
reproducible from the pinned commit before F-14a (`6562cc5`); the screen rounds, run ids and verdicts
are in `evals/analysis/GAP_REGISTER.md`.

Commit: `bbf0280` on `wp/F-14a` (all rows below).

## Retired

| Item | Decision / evidence | Removed | Replacement | Workload tested and condition to revisit |
|---|---|---|---|---|
| Slot loop (`read.agentic_mode: slot`, E02) | `areas/read.md` R-22 ("the slot loop (A, f-slot net -3) ... retired under G-22"); `GAP_REGISTER.md` E02 (f-slot-i2: net -3, fired on 150 of 437, 3 h 25 min against about 25 min) | `core/slot_loop.py`, `Engine._agentic_slot_loop/_slot_expand/_slot_neighbours` and `read/pipeline/deep.py` bodies, prompt `sufficiency@agentic_slot` + its golden, `SlotStepOut`, constants `SLOT_*`, key `read.agentic_mode`, arms `scr-agentic-slot*.json`, `evals/slotloop_offline.py`, `tests/unit/test_slot_loop.py` | `read.agentic` (single query mode); R-22 B (`read.deep` sufficiency round) is the decided successor | LoCoMo focus slice (437 q). Revisit only if a bounded deep tier (R-22 B, `f-suff`) shows a net gain and a typed-step loop is proposed against it |
| Read-time fact chain (`read.fact_chain`, E01 read arm) | `areas/read.md` R-22 ("read-time fact chain (f-chain net -1) ... retired under G-22"); `GAP_REGISTER.md` E01 (f-chain-i2 net -1) | `Engine._fact_chain_extend`, `read/pipeline/deep._fact_chain_extend`, chain join/render/`relation_unresolved`/`Chain` in `core/fact_chain.py`, constants `FACT_CHAIN_*`, keys `read.fact_chain`, `read.fact_chain_trigger`, arms `scr-factchain*.json` | None for the read arm. The write-time arm (`memories.semantic.policies.fact_projection`) is a separate, undecided arm and stays; `core/fact_chain.py` keeps only its assertion validation | LoCoMo focus slice. Revisit if the write-time projection screen shows multi-hop gain that a read-time join could add |
| Word-vector leg (`read.word_vector_*`, B15) | `areas/govern.md` G-22 ("The word-vector leg (B15) and other measured negatives are not carried into v2"); `GAP_REGISTER.md` B15 (-12 net vs BM25 on 2 conversations, 77.6 vs 81.6; replace and plus-BM25 arms both negative) | `services/embedding/word_vectors.py`, `read/pipeline/legs._word_vector_leg`, `Engine._word_vector_leg` and its state, router wiring, `READ_LEGS["word_vector"]`, keys `read.word_vector_leg/_provider/_model/_top_k`, six `*-wv-*` arms, `run_screen_wv*.sh`, `run_wv_replace.sh`, `tests/unit/test_word_vector_leg.py` | `read.hybrid` (BM25 + vector legs). The E4 static-embedding prefilter (`StaticEmbedder`, `[static]`) is a different feature and stays | LoCoMo conv-26/30 (233 q) and a 4-conversation paired screen. Revisit for a corpus where lexical overlap is structurally absent (paraphrase-heavy, non-English) |
| Decider relevance gate (`read.relevance_gate: decider`, I29) | `GAP_REGISTER.md` I29 and I74 (r3-relgate: LoCoMo 81.6 -> 59.2, 119 of 304 contexts emptied; OP-Bench 21.2 -> 43.8; "REJECT as a guard"); `areas/read.md` R-21 (store-calibrated gate is the decided gate) | the `decider` value of `read.relevance_gate`, the decider branch of `read/pipeline/gate._relevance_gate` and of `Engine._decider_active`, 4 arms `scr-relgate*.json` / `scr-protect-relgate.json`, `test_relevance_gate_injects_nothing_when_sure` | `read.relevance_gate: store_calibrated` (built, screen pending). The decider port and its other tasks (`list_mode`, `bridge_hop`, ...) stay; the `relevance` task stays in the port | LoCoMo + OP-Bench. Revisit only if the store-calibrated gate fails its screen and a decider gate with a named-reference bypass clears LoCoMo within the band |
| Kuzu graph backend (`graph.provider: kuzu`, `[kuzu]`) | ADR-034 ("the `kuzu` alias goes in the next release, together with `clients/kuzu.py`"); Kuzu archived upstream 2025-10-10; `kuzu` and `ladybug` cannot share a process | `clients/kuzu.py`, `services/graph/kuzu.py`, `Wiring.kuzu`, `Engine._kuzu`, the container branch, `GRAPH_STORES["kuzu"]`, kuzu tests (stub and alias tests, parity param) | `graph.provider: ladybug` (or `auto`) | Not a measured negative: superseded. Revisit never (upstream archived) |
| OpenSearch lexical stub (`read.lexical_provider: opensearch`) | F-14a row of `docs/v2/PLAN.md` (owner-listed scope). The stub only ever raised `MissingServiceError`; no measured verdict, no adapter was built | `services/lexical/opensearch.py`, the container branch, `LEXICAL_INDEXES["opensearch"]`, docs mentions | `read.lexical_provider: tantivy` | A server-scale BM25 index needs a real adapter against the `LexicalStore` port (plug-in registry, MOD-02); revisit when a multi-node deployment is on the roadmap |

## Not retired (reported, nothing removed)

| Item | Why it was skipped |
|---|---|
| Full / compose read modes (`default_mode`, `mode="full"` / `"compose"`) | Not default-off. `read(mode="auto")`, the default, returns the whole store as mode `full` when it fits the budget and routes aggregation questions to `compose` (`read/pipeline/router.py`, `mode in ("auto", "full")` and `is_aggregation`). Removing them changes default behaviour and the goldens. `retrieve.md` R-Q7 retires both, but the replacement route (list mode covers aggregation) has to land first as a separate behaviour change |
| v1 read headers (cards, profile, novelty, slots, session digest, recent turns, count and duration sections) | Every header key is default-off, but `retrieve.md` R-Q8 retires them only "unless screened", keeps the profile card as `[profile]`, and folds facts into a single-pool notes block that is not built yet. The headers are consumed by the full / compose / replay paths above. Needs the owner's per-key list and the notes block first |

## Follow-ups outside F-14a

- `pyproject.toml` still declares the `kuzu` and `opensearch` extras (and `opensearch` in `all`) and `uv.lock` still pins `kuzu`: removing them needs `uv lock`, which was not run here.
- `evals/run_screens_*.sh` round drivers still name the deleted `scr-relgate*` arms; they are historical, rerun them from the pinned commit.

## Temporal rules: old -> v2

FU-TEMPORAL (2026-10-13). Under `read.legs_v2` the registry's `TemporalLeg` (`read/legs/temporal.py`) replaces the old `temporal_leg` block of `_metadata_legs` (`read/pipeline/legs.py`; implementation `models/encoders/query/temporal_query.py::temporal_leg`). Nothing here is retired: every old key is read by the v2 leg. The old function stays as the `legs_v2: false` path. Parity is tested in `tests/unit/wiring/test_fu_temporal_parity.py` (same fixture, flag off and on: same ids, same order, same leg position among the extra legs).

| Old rule (key) | What it does | Old home | v2 status |
|---|---|---|---|
| `temporal_leg` | Master switch: the leg runs at all | `_metadata_legs` | **Ported.** The v2 leg runs only when `temporal_leg` is on (R3-04 ran it always). Production `base.yaml` has it on, so the tuned arms are unchanged |
| absolute dates | "7 May 2023", ISO, "May 2023", "in 2023" name the window | `query_interval` | **Covered** (`TimeWindow.from_query` calls the same function) |
| `temporal_relative`, `relative_week` | A question with no absolute date but a relative phrase ("last week") resolves against the as-of or the read clock | `anchored` dict in `_metadata_legs` | **Ported.** `registry_base_legs` sets `reference_time` only when `temporal_relative` is on or an as-of is active (R3-04 anchored always). Without the key a relative question names no window, as before |
| as-of anchor (`active_as_of`) | An active as-of anchors relative phrases even without `temporal_relative` | same | **Ported** |
| `temporal_infer_year` (N45) | "on 7 May", "in March" take the latest year on or before the newest live record (or the as-of) | `year_ref` in `_metadata_legs` | **Ported.** `year_ref` is set only under the key, and the as-of wins over the newest record (R3-04 inferred always and ignored the as-of) |
| `temporal_rank` (N61) | `midpoint` or `overlap`: in-window records sharing more content words with the question first | `temporal_leg(rank=...)` | **Ported** (`LegContext.temporal_rank`) |
| `temporal_soft` (N44) | Fills a short leg with the nearest records outside the window, within one window length (at least `TEMPORAL_SOFT_MARGIN_DAYS`) | `temporal_leg(soft=...)` | **Ported** (`_nearest_outside`); soft hits score below every hard hit |
| `temporal_leg_mentions` (N30) | A turn naming a date ("last weekend", "on 7 May"), resolved against its own time, enters when that date overlaps the window | `mentioned_spans` | **Ported** (same function and cache, so `forget_mentions` erasure still applies) |
| `temporal_leg_event_dates` (B2) | A mined fact enters by its `happened:` date; distance is taken from the start of the overlap | `_happened_in` | **Ported.** Also on when `temporal_precision_aware` is on (that flag reads the same stored field). R3-04 always included `happened:` and measured it to the interval, so a long period centred on the window ranked first; the old start-of-overlap distance is back unless precision-aware |
| ranking and ties | Sort by (-shared words, distance to the window middle, `chrono_key`) | `temporal_leg` | **Ported** (same key) |
| leg position | The temporal leg is the first leg of `_metadata_legs`, after the core-terms leg | `_metadata_legs` | **Ported.** The registry hits are handed to `_metadata_legs(temporal=...)`, which inserts them at the old place, so RRF tie-breaks and pool-protect order are unchanged |
| hit score | Constant 1.0 | `LegHit(.., 1.0)` | **Documented difference.** v2 scores `weight / (1 + days)`, never rising down the list. Rank-based fusion (RRF, family fusion) is unaffected; `minmax` fusion and `leg_min_scores["temporal"]` see the graded scores |
| closed validity (`valid_to`) | Old: only `valid_from` counted. v2: a closed `[valid_from, valid_to)` matches while it overlaps the window (R-07) | n/a | **Intentional improvement**, not a parity case: a record with no `valid_to` ranks as before |
| `temporal_precision_aware` (W2-14) | Weights a `happened:` hit by the share of its period the window covers | n/a | New in v2; off = the rows above |
| `resolve_relative_dates`, `relative_dates_anchored`, `relative_dates_weekdays/_happened`, `order_by_time_for_ordering` | Compose side: annotate dates in rendered turns, time-order list and ordering questions | `compose/pipeline/{pack,render,sections}.py` | **Unaffected.** Not read-leg rules; they run after fusion and never looked at `legs_v2` (covered by the end-to-end parity test) |

### Other read rules the v2 registry path touched

| Rule (key) | v2 status |
|---|---|
| `lexical_strip_names` (B5): drop speaker names from the BM25 query | **Ported.** `registry_base_legs` sets `LegContext.lexical_query`; the lexical leg scores it. The vector leg keeps the full query |
| `lexical_dates` (N30 write side): index date words with the text | **Ported.** `LexicalLeg` indexes `content + date_words(record)` when set (part of the index cache key) |
| `lexical_analyzer: english` (N58) | **Superseded** by `lexical_language` (default `en`: stop words and Snowball stemming); the tantivy analyser key does not touch the v2 leg |
| `lexical_provider`, tantivy tie settling | **Superseded by design** (R3-04: the v2 leg is its own BM25; equal scores settle chronologically). Scores are on the same Okapi scale but not byte-identical, so `leg_min_scores["lexical"]` floors need re-screening (S38) |
| `short_query_lexical_weight` | **Unaffected** (`_leg_weights_for`, applied in `_search` for either path) |
| `parallel_legs` | **Not needed** under `legs_v2`: the registry runs vector, lexical, temporal (and episode) together with `asyncio.gather`; the metadata, encoder and probe legs run after, as in the sequential path |
| metadata, subject, role-aware, sentence, recency, view-tag, `entity_leg`, `speaker_probe`, `core_terms_leg` legs | **Unchanged.** `registry_base_legs` still calls `_metadata_legs` (only the temporal block moved); same ids and order, tested |
| encoder and probe legs | **Unchanged** (called from `registry_base_legs`) |
| `raw_turn_floor`, `pool_protect_per_leg` / `pool_protect_mode`, `list_mode`, `perspective_mode`, `candidate_pool`, `replay_window_before/after`, `leg_min_scores`, `family_fusion` | **Unchanged.** All sit in `_search` after the legs and key off leg names (`vector`, `lexical`, `temporal`, extra legs by `name`); the registry path keeps those names. The end-to-end parity test runs the `scr-pool-protect-jina` settings with the flag off and on |
| router `legs_off` (`read.router_rules`) | **Fixed.** It was honoured for `graph` (v1 leg and `graph_leg_v2`) and the v1 `entity` / `episode` legs but not for `episode_leg_v2` or `entity_leg_v2`. `registry_base_legs` now drops any leg named in `legs_off()` (never `vector`, which is required), and `entity_leg_v2` checks `entity` |

Behaviour change to note: with `legs_v2` on and `temporal_leg` off, the temporal leg no longer runs; and relative or yearless questions resolve only under their old keys. Both match the flag-off path, which is the point of this follow-up. Revisit if a screen shows the always-anchored R3-04 behaviour is better: it would then be a new default for `temporal_relative` and `temporal_infer_year`, not a silent difference between the two paths.

## `read.neighbours_v2` in replay mode (FX-READPATH, 2026-10-13)

The flag was read only by the compose-mode neighbour expansion (`_expand_neighbours`), so the replay
read the LoCoMo harness uses (`--memspine-read-mode replay`, `_window_turns`) never consulted it and
screens S19 and the S63 bundle could not show an effect. Decision: in replay the LIB-24 library is not
called (replay already de-duplicates, gates every neighbour, and fits the budget hit first, so the
library would reproduce today's window). The flag instead shapes the replay window: with
`neighbours_v2` on, `_window_turns` (turn unit) skips pure filler neighbours (`derive.prepass.is_filler`:
greetings, "ok", "haha", emoji only) without charging a window slot, and extends the reach over them by
at most the side's quota again (-2/+4 by default, so at most -4/+8 turns inspected). The hit, the viewer
gates, the token budget and `seen` are unchanged; flag off keeps the fixed span. Compose mode keeps the
library. `tokens` unit and `single` hits are not changed.
