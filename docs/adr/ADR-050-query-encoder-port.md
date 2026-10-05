# ADR-050: Query encoder port, with a cue-matching encoder (no LLM at read)

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** master absorb list #61 (EP-H26). Builds on H8 anticipatory cues (C8',
  `Engine.add_cues`, `consolidation.anticipate`).

## Context

Madeleine reaches 66.6 on LoCoMo-Plus (official protocol) with no LLM at read: a learned query
encoder maps a later question onto the memory written to answer it. memspine already writes the
other half at write time: H8 cues, short future questions attached to the turn that answers them.
With `read.anticipatory_cues` the cue *text* is searched as an ordinary record, so a cue only helps
when the embedding or BM25 leg ranks it; nothing maps the query onto the cue set itself.

## Decision

1. **Port.** `services/query_encoder`: a `QueryEncoder` protocol with `encode(namespace, query)`
   returning an `EncodedQuery` (matched key texts as `expansions`; `CueMatch(cue_id, target_id,
   score, text)` as `matches`) and a write-time hook `observe(record)`. Default:
   `NoopQueryEncoder`.
2. **`cues` encoder** (`CueQueryEncoder`), training-free: a per-namespace cue-to-target index of
   content words, filled as `add_cues` writes cues and loaded from storage on first use after each
   start. A cue matches when the query holds at least `QUERY_ENCODER_CUE_MIN_OVERLAP` (0.5) of its
   content words; the best `QUERY_ENCODER_MAX_TARGETS` (10) targets are proposed.
3. **Read.** `read.query_encoder: none | cues`, default `none`: no leg, reads byte-identical. With
   `cues`, the engine re-checks each match (cue live, unquarantined, same namespace, trust at or
   above `read.cue_min_trust`) and adds the targets as one extra RRF leg. The targets then pass
   every read gate (status, quarantine, consent, passive sessions, filters). An encoder error
   degrades to no leg.
4. **Eviction** (2026-10-06 review). The index holds cue text in process memory, so a cue that
   is forgotten (soft, hard or by cascade), quarantined or archived is evicted when the engine
   appends that event (`QueryEncoder.evict`), and the per-namespace load indexes only live,
   unquarantined cues. Erased text never stays in the index or in `expansions`.

## Future work: a trained encoder

Madeleine trains its encoder. A trained `QueryEncoder` would plug into the same port. It needs:

- (question, answering record) pairs per user: LoCoMo-Plus or LongMemEval question-to-evidence
  links, or the engine's own `feedback` likes on retrieved records;
- hard negatives from the same conversation, and the anticipatory cues as weak positives;
- a small bi-encoder fine-tuned on those pairs (or a linear map over the existing embedder), so the
  read stays local and LLM-free;
- a held-out split by user, so LoCoMo-Plus is never trained on.

None of this is built here.

## Consequences

- No LLM call and no embedding call at read; the cost is a token-set scan of the namespace's cues.
- Tests: `tests/unit/test_query_encoder.py` (`none` byte-identical; a query matching a stored cue
  retrieves the cued record; low-trust cues ignored; the index reloads after a restart; forgotten
  targets drop out).
