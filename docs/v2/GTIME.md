# GTIME: the graph build still loses "when" questions after the lift

Follows `docs/v2/GTEMP.md` and `docs/v2/GLIFT.md`. The G2F full run (`v2-G2F-full`: GC graph base plus
`read.derived_lift=evidence+match`) scored 80.6% against the J reference `clean-jina-full` (81.4%). The lift fixed
the display (cat1 +5, cat3 +2, cat4 +1 net) but cat2 stayed at net -19 (7 fixed, 26 broken by the local
forensics; the brief counted 27, the ids are in the JSON). This note asks why.

Script `evals/analysis/gtime_diagnosis.py` reads the local forensics, results and the dataset and writes
`evals/analysis/gtime_diagnosis.json` (ids and counts only). The text-bearing dump goes to the gitignored
`evals/runs/_analysis/gtime_text.jsonl`. Command:

    PYTHONPATH=src python evals/analysis/gtime_diagnosis.py --runs <evals/runs> --data <locomo10.json> \
        --text-out evals/runs/_analysis/gtime_text.jsonl

## 1. Diagnosis

**It is not mainly a ranking problem.** The 26 broken cat2 questions (22 are `is_temporal`, 4 more are caught by
the wider `is_time_shaped`), by cause in rule order:

| Cause | Questions |
|---|---|
| gold turn missing from the G2F context and not in the pool (retrieval loss) | 5 |
| gold present, no date-looking error in the answer (reader or judge variance, relative wording) | 11 |
| gold present, the answer names a wrong month that the turns also contain | 6 |
| gold present, the answer's month occurs only in the "About" block, and is wrong | 1 |
| gold present, the answer's month occurs only in the "About" block, but is not a wrong month | 3 |

* **No loss is a pool displacement.** The class "gold in the pool but cut from the final" is empty. G2F's final
  holds all gold turns for 262 of the 314 cat2 time-shaped questions, J's for 267.
* **The five retrieval losses are base-leg losses.** In each, the gold turn sits in J's vector or lexical list
  (vector rank 8 to 33) and is absent from both lists of the graph run: the derived records of the GC store share the
  vector and BM25 index with the turns and take slots of the fetch window. That is what `read.raw_turn_floor`
  (GTEMP, W19) widens against. Neither the graph leg nor the lift causes it.
* **Where the final turns come from.** Turns in G2F's final that J's final lacks, over the broken questions:
  52 are in the shared vector or lexical legs (the reranker reordered), 40 arrived only through the lift
  (genuine finds), 10 came only from the graph legs. No derived record is in the final of any of the 321 cat2
  questions: the lift works as built.
* **What differs on every cat2 question is the lead block.** The entity-summary "About" block is in the G2F context
  of 321 of 321 cat2 questions and in none of J's. Its lines are dated with the session date or the date a fact was
  mined, not the date of the event, and sit next to turns that say "yesterday" or "last Friday".
* **Run-to-run noise on this category is large.** J-config full runs without graph and summaries, against the same
  `clean-jina-full`, give cat2 "when" questions at +4/-9 (`v2-P3J-full`), +4/-10 (`r7-protect-full`) and +6/-10
  (`r7-post-full`): net -4 to -6 with no graph. G2F is +4/-22 (net -18). So 12 to 14 of the 18 are beyond the
  noise; that is the part the block can explain.
* **Question shape.** `is_temporal` covers 264 of the 321 cat2 questions and 22 of the 26 losses. The 50 cat2 questions that are time-shaped only (durations, a named date) are net -3; the
  147 time-shaped-only cat4 questions are net +4 for G2F (the graph helps there). The "when" questions outside
  cat2 are 40 (cat1 10, cat4 30) with net +1. So the guard defaults to `is_temporal`; the wider shape is a switch.

## 2. The fix: `read.graph_time_guard`

Default `off` (byte-identical). Rules only, no model call; the context gets shorter.

| Mode | Effect on a covered question |
|---|---|
| `blocks` | no entity-summary ("About") block and no graph-facts block in the header; graph leg, node search and `derived_lift` unchanged |
| `blocks+legs` | also no graph leg, no graph node search, no lift (ablation) |

`read.graph_time_guard_shape` is `temporal` (default, `is_temporal`) or `time_shaped` (adds "how long has",
"which year", a date named in the question; `shape.is_time_shaped`).

Wiring: `compose/pipeline/pack.py:_read_headers` (header blocks), `read/pipeline/router.py:_search` (graph leg, node
search, lift), rule in `read/pipeline/time_guard.py`. Tap event `read.graph_time_guard`.

Why `blocks`, not options (a) or (c) of the brief: the data does not show the legs or the lift pushing gold out, and
the simulation below says removing them costs gold. Option (b), a time-window filter on candidates, has no support
either, since no loss is a displacement in the pool.

## 3. Offline estimate

Simulation on the forensics for time-shaped questions: remove from G2F's pool the candidates supplied only by the
graph legs or by the lift, re-cut the final top 10 from G2F's own rerank scores. The refill from the base legs is not
modelled, so this is a floor on what a guarded read keeps.

| Category | time-shaped | all gold in final: J / G2F / simulated | gained / lost by the simulation |
|---|---|---|---|
| cat1 | 21 | 12 / 11 / 11 | 0 / 0 |
| cat2 | 314 | 267 / 262 / 254 | 0 / 8 |
| cat3 | 7 | 5 / 5 / 5 | 0 / 0 |
| cat4 | 177 | 154 / 153 / 151 | 1 / 3 |

So `blocks+legs` is expected to lose gold turns and is not the pick; the simulation recovers none of the cat2 losses.

Expected effect of `blocks` on cat2 (J accuracy given the gold in context: 0.866): a lower bound of 3.5 questions
(the 4 losses whose month appears only in the About block) and an upper bound of 18.2 (all 21 gold-present
losses). The beyond-noise part is 12 to 14 questions, which is the realistic ceiling; a central guess is about 8.
Cost: questions G2F fixed whose answer date occurs only in the About block: 1 in cat1 and 1 in cat2 (time-shaped
scope), 0 in cat3 and cat4, so at most 2 lost outside the 5 retrieval losses. The five retrieval losses are not
helped; they need `read.raw_turn_floor` (parked in GTEMP), to try after G4F if cat2 stays more than 5 below J.

## 4. Arms and queue

* `evals/arms/v2-GTIME-graph-lift-guard.json` = `v2-GLIFT-graph-lift` + `read.graph_time_guard=blocks` (that key only).
* `evals/arms/v2-GTIME-pick3-graph-lift-guard.json` = `v2-GLIFT-pick3-graph-lift` + the same key.
* Queue: `G4F` (GC, REPORT, F, ref `clean-jina-full`, est 190 min) and `G5F` (GC, PROMOTE, Fob, est 300 min).
  Not launched. Compare G4F with G2F paired (`eval_paired.py`) on the cat2 "when" questions first.
