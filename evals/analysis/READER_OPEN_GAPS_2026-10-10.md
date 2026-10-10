# Reader-side open gaps: prompts, retry limits, preference scaffold, judge calibration (2026-10-10)

Scope: B3 / C4 / R2-6 (inference), C6 (lists), C9 (regressions), C10 (retry limits), I18 (preference
following), I13 (judge calibration). Development conversations only (conv-26, 30, 41, 42); no held-out
conversation was opened, no model was called, no gold is used at run time. Everything below is measured from
existing run files in `evals/runs` (`qa-full-qs-eq06-roff-fx`, `qa-full-qs-eq06-fix`, `rs-r0-ref-i2`, `rs-r3-all-i2`)
or is hand classification by one annotator (marked [H]). Nothing here has been screened with a reader yet: the new
prompts are untested on a model and carry no accuracy claim.

## 1. Prompts and clauses added (`evals/memspine_evals/readers.py`)

All are opt-in `--qa-prompt` names built on `grounded_generic` (generic wording, no benchmark names; a test
checks). Existing prompt ids are byte-identical (a test pins the sha256 of `default`, `grounded`,
`grounded_detail`, `grounded_generic`).

| name | adds to `grounded_generic` | targets |
|---|---|---|
| `grounded_generic_infer` | `INFER_CLAUSE` + `DETAIL_CLAUSE` | B3, C4, R2-6, C9 |
| `grounded_generic_list` | `LIST_CLAUSE` | C6 |
| `grounded_generic_prefs` | `PREFERENCE_CLAUSE`; the exact-refusal sentence is limited to questions; the "keep the answer short" rule is replaced | I18 |
| `routed_generic` (routed) | per question: `infer` for would / likely / might questions, `list` for count and list questions, else plain `grounded_generic` (dates stay plain: the generic prompt already resolves them) | B3 + C6 together |

- **INFER_CLAUSE.** For a "would / might / is likely to" message or "what might someone do or be": do not refuse, give
  the most likely answer (yes or no, or a short phrase) plus a one-line reason from what the memories show about the
  person, bridging with ordinary knowledge; "not mentioned" only for a factual question (name, date, number, place) the
  memories do not state. The exact refusal string stays in the prompt, so unanswerable factual questions still refuse.
- **DETAIL_CLAUSE (C9).** Keep the specific detail the question asks for; when similar events appear on several dates,
  use the line that matches every detail in the question.
- **LIST_CLAUSE (C6).** Go through every memory line, collect each distinct item, merge repeated mentions, list them
  all, and count the merged list when a number is asked; do not add items the memories do not state.
- **PREFERENCE_CLAUSE (I18).** For a request for help, advice or a recommendation: answer as a helpful assistant; follow
  stored preferences, dislikes, constraints and habits that bear on the request without announcing or quoting them; never
  apply a preference to an unrelated request.

`RoutedQAPrompt` gained optional `shape_fn`, `router_version` and `name` (defaults reproduce `routed` byte-identically;
its `describe()` is unchanged).

### What `grounded_detail` already did for lists (C6)

Its single list sentence is "when the question asks for several items or 'how many', list every matching item found
across the memories, then count them", inside a one-sentence-answer prompt with the hit-marker legend. It does not say to
read every line, does not merge repeats, and counts whatever was listed. The screen of record
(`DEV_GAP_REASONING_2026-10-10.md`: R3, which used the detail prompt) gained two list questions but lost others to the
longer answers. `LIST_CLAUSE` is the same idea in a prompt without the one-sentence cap and without the marker legend,
and only reaches list-shaped questions when used through `routed_generic`, which limits the length risk to those.

## 2. B3 / C4 / R2-6: the five dev failures the inference clause targets

From `qa-full-qs-eq06-fix`, development conversations. Of 22 inference-shaped questions (`query_shape.is_inference`) 12
are correct and 10 wrong; five quotes:

| id | question | gold | final answer | what the clause changes |
|---|---|---|---|---|
| conv-26/0-22 | Would Caroline likely have Dr. Seuss books on her bookshelf? | Yes, since she collects classic children's books | "No, it is not mentioned that Caroline has Dr. Seuss books; she specifically lists classics ..." | commit to the likely answer (Yes) from the collecting-classics line instead of treating absence as No |
| conv-26/0-59 | Would Caroline be considered religious? | Somewhat, but not extremely religious | "No, Caroline would not be considered religious; she identifies as LGBTQ+ ..." (the baseline refused) | still committed and wrong; the clause asks for a one-line reason, which may expose the nuance, but this one is a judgement call |
| conv-41/2-17 | What might John's degree be in? | Political science, Public administration, Public affairs | "likely in a field related to education or public policy" | already commits; the reason line should name policy-making; likely stays partial |
| conv-41/2-45 | Would John be open to moving to another country? | No, he has goals specifically in the U.S. like joining the military and running for office | "The memories do not mention whether John would be open to moving ..." | the exact failure: a refusal on a would-question; the clause forbids it and asks for a likely No with the goals as reason (retrieval also missed the goals line, so it may not be reachable) |
| conv-42/3-12 | What underlying condition might Joanna have based on her allergies? | asthma | "... The text does not specify the underlying medical condition ..." | refusal on a "might"; with general knowledge bridge the reader may say "possibly asthma" (allergy with puffy, itchy face); unverified |

Others of the 10: `0-2` and `0-69` (partial: committed, incomplete), `2-8`, `2-64`, `3-66` (retrieval misses: the clause cannot
help without the line). Honest reach: of the 10, three are clear refusals (0-22, 2-45, 3-12) that the clause targets,
two committed-and-wrong (0-59, 2-17), three retrieval misses, two partial. The prompt can convert the refusals; the
register's earlier finding that the reader converts only ~25% of inference questions even with all gold still applies.
Not included: world-knowledge lookups (`3-73` "which state is Fort Wayne in", `3-87`) are factual questions, so the
clause deliberately leaves them to the factual path.

Offline guard against the opposite risk (turning correct refusals into fabrications): the clause is limited to
would / might / likely messages and the router uses `is_inference`, which on LoCoMo hits open-domain almost only
(21 of 22 dev matches). Whether it also fires on the cat-5 probes ("Would ..." about a swapped speaker) cannot be
measured on existing runs (they are retrieval-only); it must be checked in the first screen with cat-5 included.

## 3. C9: regressions from the grounded prompt with a bigger window

Paired comparison, development conversations, `qa-full-qs-eq06-roff-fx` (baseline) against `qa-full-qs-eq06-fix`, 584
questions (`evals/paired_regressions.py`): 70 gained, 24 lost, net +46. Per category (gained / lost): multi-hop 19/4,
open-domain 11/2, single-hop 25/8, temporal 15/10. Mean context lines 40.3 to 54.3 for the lost rows (the window grew by
about a third); 13 of the 24 answers are under 0.8 of the baseline length; gold was fully in context for 18 of 24.

Hand classification of the 24 losses [H]:

| cause | n | ids | reader-fixable |
|---|---|---|---|
| baseline credited a refusal / non-answer (judge false positive in the baseline), so not a regression of the reader | 5 | 0-59, 1-43, 2-32, 3-43, 3-92 | no (judge) |
| judge false negative on an equivalent answer (resolved date, small wording difference) | 8 | 0-41, 3-48, 3-64, 3-167, 2-142, 0-68, 3-24, 1-44 | no (judge, A2 date check, I13) |
| distractor: a later or wrong line about the same topic won | 4 | 1-14, 1-20, 3-44, 3-54 | yes: C9 clause "use the line that matches every detail" |
| short answer dropped the specific detail | 6 | 0-138, 0-66, 1-23, 3-143, 3-135, 0-2 | yes: C9 clause "keep the specific detail" |
| list over-inclusion (extra events) | 1 | 0-70 | partly: `LIST_CLAUSE` says do not add unstated items |

So 13 of 24 are not reader regressions (judge side), and 11 are: 4 distractors and 6 detail drops (the "short, direct
answer" rule plus a larger window) and 1 over-inclusion. The distractors are exactly the "bigger context" signature
(`1-14`: April vs June, `1-20`: 10 vs 27 May, `3-44`, `3-54`). This reproduces the all-conversation analysis in
`READER_GAPS_FORENSIC.md` section 2.3 (14 of 49: lists, detail, caption; 9 distractors; 15 judge) on the dev subset
only. The same pair on the two earliest dev screens (`rs-r0-ref-i2` against `rs-r3-all-i2`, 233 questions): 7 lost /
7 gained, single-hop net -4 (5 lost), reader prompt change plus detail prompt: consistent with detail drops and
distractors, no clear single cause beyond noise.

Cause is clear for 11 of 24 (reader side) and the proposed fix is `DETAIL_CLAUSE`, shipped inside
`grounded_generic_infer` (it applies to every question type, not only inference). It is an association and a hypothesis,
not an ablation: the run changed prompt, window, anchored dates and retry together. Test it as a paired screen on
the dev set (`grounded_generic` vs `grounded_generic_infer` and `routed_generic`) with `paired_regressions.py` before use.

## 4. C10: retry limits

Measured (`READER_GAPS_FORENSIC.md` section 3, all conversations; dev slice here): 107 fires on 1,540 questions, 77
accepted, 30 returned a second refusal, 31 of 107 end correct. Development slice of `qa-full-qs-eq06-fix`: 33 fires,
21 accepted, 14 end correct; by category (fired, correct) open-domain 13/7, multi-hop 8/2, temporal 6/2, single-hop 6/3.
`rs-r0-ref-i2`: 11 fires, 7 correct; `rs-r3-all-i2`: 11 fires, 8 correct.

Limits now documented and tested in `evals/memspine_evals/refusal.py`:

1. **One retry, never a loop** (`MAX_RETRIES = 1`); a retry that is again a refusal is discarded and the first answer
   kept.
2. **No retry on an empty context** (already the behaviour; now pinned by a test).
3. **New, opt-in `--retry-guard`** (`RefusalRetryReader(guard_absent_entity=True)`, reader id gets `-entityguard`): no
   retry when the question names at least one capitalised entity and none of them appears in the retrieved context,
   i.e. a question about someone the memories do not mention. The skip is recorded (`meta.retry_skipped`). First
   version flagged any absent name and would have skipped `0-64` ("Would Melanie likely enjoy ... Vivaldi?", answered
   correctly after the retry, Vivaldi absent from context); requiring that all names be absent keeps it retryable.
   On the three dev runs it fires on nothing (dev categories 1-4 have no unanswerable questions), as intended.
4. **Not detected, honestly**: a swapped-speaker cat-5 probe (both people are in the store, the pairing is wrong). A
   generic rule for it needs the speaker of each retrieved line and is not built. Existing cat-5 rows are retrieval-only
   (no context text), so the guard cannot be measured on them; screen with cat-5 included before claiming it.

The retry's use stays where the data says it pays (open-domain, 15 of 42 fires correct); it is off by default.

## 5. I18: preference following

`PREFERENCE_CLAUSE` / `grounded_generic_prefs` (section 1). Adherence scaffold in `evals/forensics_report.py`:

- `preference_label(result)` reads the preference from the benchmark's own gold (`meta.preference` or
  `meta.preference_label`, else the `gold` text of a row whose `type_label` starts `explicit/`, `choice/`, `persona/` or
  `preference`); LoCoMo rows have none, so the block is `null` there and the schema validates.
- `preference_adherence_block(rows)` (summary key `preference_adherence`, also per form): `judged_adherence` (the
  dataset's adherence judge verdict), `preference_turn_in_context` (retrieval side), `announced_rate` (answers that say
  "as you mentioned ...", which the clause forbids), `violation_proxy` (share of answers containing a word the stored
  preference rules out; lexical, misses paraphrase and over-flags a refusal that names the item, read it next to the
  judge). No model call.

Not done: a real adherence judge run (needs a reader and judge) and an unrelated-request control set (a request the
preference should not touch) to measure over-application; both need a preference dataset locally.

## 6. I13: judge calibration set

`evals/analysis/judge_calibration_dev.jsonl`: 43 development items with a hand verdict and a recorded reason, built by
`evals/build_judge_calibration.py` from `qa-full-qs-eq06-fix` (labels in the script, so a change is a visible diff). Mix:
13 correct (5 paraphrases, 4 date equivalents, 2 short lists, 2 complete lists), 10 partial lists, 9 refusals (8 plain and
1 hedged), 11 wrong but plausible. 7 rows are marked `borderline` (a second reader could differ). Verdicts: CORRECT /
PARTIAL / WRONG, where a list is CORRECT only with every gold item.

`evals/judge_agreement.py --run <run>` (or `--labels file` for a judge run over the set itself) reports binary
agreement, Cohen's kappa (PARTIAL excluded, the judge is binary), false negatives and positives, partial-credit rate,
refusal-credit rate and per-kind confusion, with and without borderline rows; `--fail-below` gates a script. For a run
it compares only rows where the run's answer text equals the set's answer (a different reader yields a different
answer); the unmatched count is printed.

Result on the source run (guarded rubric judge, same Qwen reader and judge): 33 binary items, agreement 84.8%, kappa
0.66 (89.3% / 0.76 without the 7 borderline rows); 5 false negatives (2 equivalent dates, 3 paraphrases), 0 false
positives; refusals credited 0 of 9; partial lists credited 4 of 10 (the over-credit named in I13). The baseline run's
answers differ, so only 1 item matches there. Caveats: the set was drawn mostly from rows the judge marked wrong
(the failure forensics) so false negatives are over-represented and the false-positive rate is not an estimate; 43 items
and one annotator give a wide interval (kappa 0.66 is a point on 33 items); it is development-set only. It is a
regression harness for judge changes, not a population estimate; the 150-row stratified set in
`READER_GAPS_FORENSIC.md` 4.5 is still needed for headline claims.

## 7. Tests

`evals/tests/test_reader_open_gaps.py` (31 tests): prompt ids unchanged, new prompts render and are generic, infer keeps
the factual refusal, list/prefs wording, router cases, CLI flags, retry cap / empty context / default unchanged / guard,
named-entity helper, preference block, calibration set shape (dev only, label table equals the file), agreement script
(perfect, lenient judge, `--fail-below`, `--run` matching), paired comparison. `test_routed_qa.py`'s prompt-name table
gained the three names. `evals/tests`: 942 passed, 13 skipped; `tests/unit`: 2,666 passed, 35 skipped.
