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
| Word-vector leg (`read.word_vector_*`, B15) | `areas/govern.md` G-22 ("The word-vector leg (B15) and other measured negatives are not carried into v2"); `GAP_REGISTER.md` B15 (-12 net vs BM25 on 2 conversations, 77.6 vs 81.6; replace and plus-BM25 arms both negative) | `models/encoders/embedding/word_vectors.py`, `read/pipeline/legs._word_vector_leg`, `Engine._word_vector_leg` and its state, router wiring, `READ_LEGS["word_vector"]`, keys `read.word_vector_leg/_provider/_model/_top_k`, six `*-wv-*` arms, `run_screen_wv*.sh`, `run_wv_replace.sh`, `tests/unit/test_word_vector_leg.py` | `read.hybrid` (BM25 + vector legs). The E4 static-embedding prefilter (`StaticEmbedder`, `[static]`) is a different feature and stays | LoCoMo conv-26/30 (233 q) and a 4-conversation paired screen. Revisit for a corpus where lexical overlap is structurally absent (paraphrase-heavy, non-English) |
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
