# GLIFT: derived records only find turns

Follows `docs/v2/GTEMP.md`. GTEMP showed that on the GC graph base derived records (graph edge facts,
entity summaries, session or episode text) take about half of the 10 reranked slots, shrink the turn
windows (cat2 mean turns in context 51 to 26) and carry session dates. The GTEMP fixes (`raw_turn_floor`,
`derived_dates`, `derived_cap`) were parked after G1 (they do not help once the LadybugDB FTS works).
GLIFT takes the other route: a derived hit is a **finder**, never evidence. It is replaced by its source
turns before the rerank, so the reader sees turns only.

The older `read.lift_before_rerank` (`read/pipeline/lift.py:lift_stage`) was rejected for this: a
session-granularity fact lists the whole session as parents, so it picked turns by rank, not by match.
`read.derived_lift` uses the narrowest lineage and, where the lineage is wide, the query to choose.

## 1. Offline gap analysis

Script `evals/analysis/glift_gaps.py` (reads local forensics, results and the dataset; writes
`evals/analysis/glift_gaps.json`, ids and counts only; the text-bearing per-question dump goes to the
gitignored `evals/runs/_analysis/glift_gaps_text.jsonl`). Command:

    python evals/analysis/glift_gaps.py --runs <evals/runs> --data <locomo10.json> \
        --p v2-P2REF-full --p v2-P2REF2-full --text-out evals/runs/_analysis/glift_gaps_text.jsonl

**What is and is not available.** The graph runs use a temporary store; nothing was persisted. The
forensics (`forensics.jsonl`, `ingest.jsonl`) carry the pool, the reranked list and the context, with the
record id and text of each derived hit, but **no record to parents mapping and no envelope**. So the
evidence of a derived hit is a proxy, built from its text: a composite window (session or episode text)
lists its turns, so the turns whose text it contains are exact; a fact or entity summary gets the top 3
turns of its conversation by idf-weighted word overlap (restricted to the session of its date when it
has one). That is the signal `evidence+match` uses (similarity of parents to the query), seen from the
fact side. Treat (i) as an estimate; the exact lineage is only known at run time (the tap event
`read.derived_lift` records it).

Runs: graph base `v2-P2REF-full` (all 1,540 questions, FTS broken, 79.35%) and `v2-P2REF2-full` (FTS
fixed; **1,030 of 1,540 rows were done** when this was written, the analysis covers the common rows;
re-run when the run is complete), against `clean-jina-full` (J reference, 81.36%). "Broken" = J right,
graph wrong; "fixed" = the reverse. Gold turns counted are those the J context held and the graph
context lost (so a question where J itself missed the gold is category c0).

Categories for the broken questions (rule order: c0, then iv, then i, iii, ii):

| Category | Meaning |
|---|---|
| c0 | the J context held no gold turn: not lift related |
| iv | gold is present in the graph context, but the answer's date or year occurs only in derived text (a date error carried by derived text) |
| i | a gold turn the graph run lost is in the evidence of some derived hit in the final: the lift rescues it |
| iii | gold lost, in no derived evidence, but it ranks within the top 10 of the turn-only rerank: freeing the slots rescues it (displacement only) |
| ii | gold lost, not recoverable: no useful lineage (session-wide parents that do not hold it), or the gold turn was never in the pool (retrieval loss) |
| heavy | gold present, no date error, at least 4 derived hits in the final (reader noise and displacement of context) |
| other | gold present, fewer than 4 derived hits |

| Run, category (broken) | c0 | iv date | i evidence has gold | iii displacement | ii no lineage | heavy | other |
|---|---|---|---|---|---|---|---|
| P2REF cat1 (30) | 1 | 0 | 3 | 1 | 9 | 12 | 4 |
| P2REF cat2 (30) | 1 | 7 | 1 | 3 | 2 | 15 | 1 |
| P2REF cat3 (3) | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| P2REF cat4 (26) | 2 | 0 | 2 | 0 | 0 | 11 | 11 |
| **P2REF total (89)** | 7 | 7 | 6 | 4 | 11 | 38 | 16 |
| P2REF2 partial (1,030 rows; 65 broken) | 5 | 6 | 5 | 1 | 10 | 29 | 9 |

In P2REF the "ii" cases are all "gold never in the pool" (11 of 11): the lift cannot help them. In
P2REF2 one "ii" case had the gold in the pool.

Per derived hit in the final context (broken questions, P2REF; ids per question are in the JSON):

* kind: undated fact 294, dated fact 105, composite window 54 (the entity-summary header is a lead block,
  not a pool hit, and is outside this count);
* channel (a hit can be in several legs): vector 430, lexical 306, perspective 243, extra (graph and
  entity legs) 77;
* rank held in the final: spread over all 10 ranks (cat2 broken: rank 1 holds a derived hit in 24 of 30
  questions; ranks 2 to 10 hold between 11 and 21 each);
* turn slots taken: mean derived hits among the 10 final slots on broken questions 6.3 (cat1), 5.4 (cat2),
  4.0 (cat3), 3.5 (cat4); on the questions the graph fixed 8.0, 5.3, 5.3, 4.4. So the share is as high on
  the questions the graph wins (see risk below).

The same shape holds with the FTS fixed (P2REF2: 7.3, 5.2, 5.5, 6.1 derived slots on the broken
questions), so the FTS fix did not reduce the displacement.

The reverse direction (graph right, J wrong, P2REF: 58 questions): in 48 of them gold is in the graph
context (in 34 a derived hit's evidence also holds the gold, in 14 no derived evidence is needed to hold
it); in 10 the graph run answered right **without** the gold turn in its context, i.e. the derived text
itself carried the answer. Those 10 are the lift's risk: they only survive if the source turns of the
derived hit say the same.

## 2. What was built

`read.derived_lift` (`off`, `evidence`, `evidence+match`; default `off`) and
`read.derived_lift_max_parents` (default 3). Code `read/pipeline/derived.py:derived_lift_stage`, wired in
`read/pipeline/router.py:_search` right before the existing `lift_before_rerank` stage, i.e. at the point
the derived hits are in the pool and before the rerank.

For each pool hit that is derived (has `source.parents` and is not episodic; lead/header records are
left alone):

1. lineage: the turns cited in the record's envelope (`fingerprint.envelope.evidence`, roles `support`
   and `binding`; `context` lines are not evidence) if it has any, else `source.parents`; a derived
   parent is followed to its own turns (3 levels at most);
2. visibility: every candidate turn passes the read gates again (status, quarantine, consent, trust,
   date and scope filters, the header `hide`); a derived hit with no visible source turn is dropped;
3. more than K = `derived_lift_max_parents` turns: `evidence` keeps the first K in lineage order;
   `evidence+match` keeps the K most similar to the query, by cosine on the stored vectors (new
   `scores_for(namespace, vector, embedder_id, ids)` on the LanceDB store, a filtered cosine query, no
   embedding call); a store that cannot score ids falls back to query-turn word overlap;
4. the lifted turns take the derived hit's fused score (the larger one if the turn is already in the
   pool) and are deduplicated; the pool is sorted by score and cut back to its incoming size, protected
   hits first. The derived record itself never reaches the reranker or the context.

Header blocks (entity-summary header, graph facts, cards, profile) are built outside the pool and are not
changed; the entity summaries and graph still work as finders (legs unchanged). Tap event
`read.derived_lift`: `n_in`, `derived`, `lifted` (ids), `dropped` (ids), `per_kind` (in / lifted /
dropped by record type), `mode`. Off is byte-identical (default config dump and golden unchanged except
the two new default keys).

Tests (`tests/unit/wiring/test_glift_derived_lift.py`, invented text, hash embedder, stub store):
evidence first (context line ignored), session-wide parents ranked by stored-vector similarity, lexical
fallback, `evidence` keeps lineage order, dedupe to the max score, drop without a visible source and lead
block untouched, pool cut, no derived hits returns the same list, a real LanceDB `scores_for`, and
three engine tests (search on replaces the fact by its turn, off keeps it, replay `read` never has the
derived record in the context).

## 3. Offline estimate (how many broken questions the lift rescues)

Rescue ceiling = i + iii + iv (the derived text is gone, so a date error carried by it is gone; the lifted
turns or freed slots bring back gold). Expected = ceiling times the J reference's accuracy given gold in
its context (cat1 0.755, cat2 0.866, cat4 0.922; cat3 not counted).

| Run | cat1 | cat2 | cat4 | Total ceiling | Expected recovered |
|---|---|---|---|---|---|
| P2REF (89 broken) | 4 | 11 | 2 | 17 | about 14.3 |
| P2REF2 partial (65 broken) | 4 | 7 | 1 | 12 | about 10.0 |

About 14 of the 89 broken questions on the full 1,540: roughly 0.9 point (14 / 1,540). The gap to the J
reference is 2.0 points (31 questions). Not counted and not safe to assume: the heavy group (38, plus 16
"other"), where the gold is present and only the derived noise differs; removing derived text from the
context would help some of them and can only be measured by running. Against it: up to 10 questions the
graph fixed through derived text alone may be lost. The ii group (11) is retrieval loss, which no lift
changes. Net: expect a recovery of +0.5 to +1.5 points, inside reader noise, which is why the targeted
slices come first.

## 4. Arms, queue rows, slices (nothing launched)

* `evals/arms/v2-GLIFT-graph-lift.json` = `scr-gc3-graph-jina` + `read.derived_lift=evidence+match`
  (nothing else; `test_v2_arms.py` checks the diff).
* `evals/arms/v2-GLIFT-pick3-graph-lift.json` = the adopted `v2-PICK3-j` winners + the GC graph sleep
  build (the S02 delta over the J reference: `graph`, the `llm.roles` extract/summarize pair, the
  associative and semantic memory policies, `read.graph_leg`, `graph_leg_k`, `graph_depth`,
  `graph_node_search`, `entity_summaries`) + `read.derived_lift`. It validates as a GC arm: this is the
  question "does a lift-only graph add on top of the adopted default".
* Queue rows (B11): `G2` (GC, NONINF, set f, 150 min, ref `v2-S02-i2`, dep S02) with slices
  `fail_temporal` and `fail_mix_120`; `G3` (GC, NONINF, set f, 170 min, same ref, dep S02) with
  `fail_temporal`, `fail_mix_120` and `fail_retrieval`. Registered in
  `evals/build_targeted_slices.py:SCREEN_OVERRIDES`; `evals/v2_slice_map.tsv` regenerated (5 rows added,
  no slice file changed). G3 has no PICK3 sample run to compare against, so its reference on the slice is
  `v2-S02-i2`; the PICK3 comparison for G3 comes from the full run `clean-jina-full` / `P3J`.
* Because GC slices use only the top-3 conversations (`_c3`), a slice result is a signal, not a decision.
  **The decisive test is a full run (1,540 questions) against `clean-jina-full`** (J reference 81.36%),
  with `eval_paired.py`. Run it only if a slice shows the temporal targets recovered without losing the
  controls (pass: targets up, controls not down by more than the band).

Pass / fail for G2: temporal and mix targets recovered, no control loss. For G3: additionally no loss on
`fail_retrieval` against the S02 reference (a lift-only graph must not cost retrieval).

## 5. Extra model calls and gaps

No extra model call on the read path (cosine on stored vectors, or word overlap). One filtered LanceDB
query per read that has at least one derived hit with more than K source turns; lineage is fetched from
the record store.

Gaps: the lineage categories of section 1 are a text proxy, since no run left a store with lineage;
`scores_for` exists on the LanceDB and per-namespace stores only (other vector stores use the lexical
fallback); the lift is in `_search`, so a read path that bypasses it (the deep/planned reads reuse it) is
covered but a custom leg that adds derived records after the pool is not.
