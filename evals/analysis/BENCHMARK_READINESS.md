# Benchmark readiness, 2026-10-10

Scope: which memory benchmarks the harness can run, what is on disk, and how to run a fixed
dev / held-out slice of each. Nothing here was run through a model: every count below comes from
calling the dataset loaders directly (no GPU, no Ollama). Licence and size facts come from the HF
and GitHub APIs and the repos' LICENSE files, checked 2026-10-10.

**LongMemEval: excluded by user decision 2026-10-10.** It is in no tier, has no run commands here,
and no variant was downloaded. (The registry and the loader still exist; the pre-existing
`data/longmemeval_oracle.json` was left untouched.)

## 1. State of play

- Before this pass only `data/locomo10.json` (+ the excluded LongMemEval oracle file) was local.
- Downloaded now into `evals/data/` (git-ignored by the `data/` rule, nothing staged): MemoryAgentBench
  Conflict_Resolution (1.5 MB, MIT), BEAM 100K split (5.4 MB, CC BY-SA 4.0), TOFU (2.9 MB, MIT),
  MUSE-News privleak/verbmem/knowmem files (3.3 MB, CC BY 4.0), HaluMem-Medium (33 MB, CC BY-NC-ND 4.0),
  ASB (0.2 MB, MIT), CPB live (1 MB, MIT), PerLTQA en_v2 (19 MB, CC BY-NC 4.0 per LICENSE.txt),
  PrefEval benchmark_dataset without the 112 MB rag_retrieval folder (8 MB, CC BY-NC 4.0 per LICENSE),
  PersonaBench synthetic_data v1 (30 MB, CC BY-NC-SA 4.0 per LICENSE), and a 25-file ConvoMem sample
  (15 MB, CC BY-NC 4.0; the smallest file of each evidence-type x k stratum, 1,096 questions).
  All of these are NC licences or open licences that allow research use; none is redistributed.
- Everything that is present loads cleanly through its loader (section 3). Two loader gaps are noted below.
- **Wiring is the real blocker, not data.** `cli.py c0-1 --dataset` accepts only
  `convomem, locomo, locomo_plus, longmemeval, memoryagentbench, synthetic`. The 12 other adapters
  (beam, perltqa, prefeval, personabench, tofu, muse_news, halumem, halumem_proxy, cpb_live, asb, lamp2,
  memorycd, op_bench) are not reachable from `run.sh` or `_launch.py` today; they are used from Python
  (their tests, `registry.load`). Each needs a branch in `cli.py:_dataset()` and in the `--dataset`
  choices (one block, like the existing ones), plus a scorer for the non-R@k measures.

## 2. Readiness table

Modes: R = `--mode retrieval` (coverage / R@k from `gold_turn_ids`, no model), Q = `--mode qa`.
"CLI" = reachable through `run.sh` as is. n = questions in what is local.

| dataset | measures | source (as in loader/registry) | size | licence | local? | loader OK? | modes | judge | n questions | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| locomo | single-hop, multi-hop, temporal, open-domain, adversarial QA over 10 long chats | github snap-research/locomo `locomo10.json` | 2.8 MB (7.7 MB repo) | GitHub: none asserted; registry: "check release" | yes | yes | R + Q, CLI | LLM rubric, abstention-aware (cat 5 routed to the abstention judge); optional date pre-check | 1,986 (cat1 282, cat2 321, cat3 96, cat4 841, cat5 446); 10 convs, 5,882 turns | cats 1-4 = 1,540. Cat 5 handled correctly (section 4) |
| longmemeval | | | | | | | | | | **excluded by user decision 2026-10-10** |
| memoryagentbench (Conflict_Resolution only) | selective forgetting / fact supersession (FactConsolidation, single-hop and multi-hop, 6k-262k contexts) | HF ai-hyz/MemoryAgentBench @ 7ea0669 | 1.5 MB (other 3 splits 73 MB, no adapter) | MIT | yes (new) | yes | R + Q, CLI | alias substring match (deterministic, no model) | 800 in 8 items: mh/sh x 6k/32k/64k/262k, 100 each | gold-turn mapping covers 798/800 (2 unmapped). Contexts 455 / 2,310 / 4,580 / 18,332 facts: ingestion of the 262k items is the cost risk |
| convomem | evidence-count scaling: user, assistant, changing, preference, implicit, abstention facts | HF Salesforce/ConvoMem @ e3e9b39 `core_benchmark/evidence_questions` | full repo 27.5 GB; evidence_questions 1.2 GB; local sample 15 MB | CC BY-NC 4.0 (data) | partial sample (new) | yes | R + Q, CLI (`--per-stratum`, `--filler` via --flags) | exact match for user / assistant / changing; semantic match for preference / implicit (judge not reproduced); abstention unspecified | 1,096 in the sample: assistant_facts 344, changing 206, user 197, abstention 193, preference 83, implicit_connection 73 (25 type/k strata); avg 56 turns per item | **Loader gap:** abstention items (193) have `meta.abstention=False`, gold "There is no information in prior conversations...", so they go to the default rubric route, not the abstention judge. 205 questions have no gold turns |
| halumem | memory QA (6 types) over 20 long user histories | HF IAAR-Shanghai/HaluMem @ cb04336 `HaluMem-Medium.jsonl` | 33.5 MB (Long 106.5 MB not fetched) | CC BY-NC-ND 4.0 | yes (new) | yes | Q only in principle (no turn gold) | official: LLM labels Correct / Hallucination / Omission; **no such judge in the harness**, so a Q run would use the generic rubric judge, not comparable to published numbers | 3,467: Memory Boundary 828, Memory Conflict 769, Basic Fact Recall 746, Generalization 746, Multi-hop 198, Dynamic Update 180; 60,146 turns (about 3,000 per user) | "Memory Boundary" (828) is abstention-like (gold "Unknown, not provided by the user") but not flagged `abstention` |
| halumem_proxy | same questions, lexical memory-point -> turn proxy gold | same file | same | same | yes | yes (49 s to build) | R, no CLI | none (retrieval) | 3,467; 1,080 with no proxy gold; 828 flagged abstention | proxy gold is a heuristic (overlap >= 0.5); report as such |
| beam (100K) | 10 abilities: abstention, contradiction resolution, event ordering, info extraction, instruction following, knowledge update, multi-session, preference, summarisation, temporal | HF Mohammadta/BEAM @ 3205395 | 100K 5.4 MB (500K 34 MB, 1M 66 MB, 10M 344 MB not fetched) | CC BY-SA 4.0 | yes (new) | yes (needs the venv's pyarrow) | R, no CLI. Q is not wired (rubric-based answers; registry says "QA judge not run") | none yet | 400 = 20 probes x 20 convs, 40 per ability; 5,732 turns (about 287 per conv); 46 with no gold ids | 40 abstention probes have no source ids by design |
| perltqa (en_v2) | personal long-term memory QA by memory type (profile, social, events, dialogues) | github Elvin-Yiming-Du/PerLTQA @ 8d9e198 | 19 MB | CC BY-NC 4.0 | yes (new) | yes | R, no CLI | none (retrieval) | 8,316 over 31 characters: events 4,356, dialogues 2,744, social 871, profile 345; about 69 records per character | 11 questions without gold ids. Far too many questions for 5 s/q without a cap |
| prefeval | preference following under filler: explicit, choice-based, persona-driven x 20 topics | github amazon-science/PrefEval | 8.4 MB (rag_retrieval folder skipped; repo 27 MB) | CC BY-NC 4.0 | yes (new) | yes | R, no CLI | none (retrieval) | 3,000 (1,000 per form, 60 form/topic labels); 166 without gold turns | mirrors on HF `siyanzhao/prefeval_*` carry no licence tag, so the GitHub copy is the one used |
| personabench | persona recall by noise level 0.0 / 0.3 / 0.5 / 0.7: basic info, social, preference easy/hard, subjective | github SalesforceAIResearch/personabench @ 151e8c9 | 30 MB | CC BY-NC-SA 4.0 | yes (new) | yes | R, no CLI | none (retrieval) | 1,052 over 24 (community, person, noise) items; Basic info 440, Social 212, Pref/hard 164, Subjective 132, Pref/easy 104; community_0 532, community_1 520 | Subjective is excluded by the official protocol; drop it from the headline |
| tofu | memory erasure: residual R@k after hard delete, retain R@k | HF locuslab/TOFU @ 324592d | 2.9 MB used (6.3 MB repo) | MIT | yes (new) | yes | R-style erase probe, no CLI | none | 320 queries: retain 100, retain_paraphrase 100, forget 40, forget_paraphrase 40, holdout 40; one item of 4,000 turns | forget set is hard-deleted by a driver, so it needs a custom run loop, not plain `c0-1` |
| muse_news | memory erasure on news passages | HF muse-bench/MUSE-News @ 506bd5b | 3.3 MB used | CC BY 4.0 | yes (new) | yes | erase probe, no CLI | none | 300 (forget / retain / holdout 100 each), 200 turns | knowmem is not adapted |
| cpb_live | correlated-promotion poisoning: true R@k, false-retrieval rate, true-above-false order | github lxy1134/iclr_2027 | 1 MB | MIT | yes (new) | yes | R, no CLI | none | 160 queries / 160 scenarios, 440 turns; 65 without gold ids (false-only families) | |
| asb | memory firewall TPR / FPR on injected tool texts | github agiresearch/ASB @ 544540f | 0.2 MB used | MIT | yes (new) | yes | deterministic, no model, no CLI | none | 51 task queries, 10 agents, 420 turns; plus 400 attack and 20 normal tool texts | injection templates are reconstructed from the benchmark card, not vendored |
| locomo_plus | implicit (cue / trigger) recall planted in LoCoMo | github xjtuleeyf/Locomo-Plus @ 059f4e3 | 1.2 MB | **no licence in repo** | no | not testable (no data) | R + Q, CLI (needs `--locomo-path`) | official locomo-plus-v2 (correct 1 / partial 0.5 / wrong 0) or `constraint` | 401 per registry | not downloaded: licence does not clearly allow use |
| op_bench | over-personalisation proxies on LoCoMo | github yulinlp/OP-Bench @ 17c7efd | 5.1 MB | **none chosen** | no | not testable | R proxies, no CLI | none | not counted | not downloaded: no licence |
| lamp2 | movie tagging by kNN over the user's profile | ciir.cs.umass.edu LaMP-2 dev | about 10s of MB, manual fetch | no licence file; research only | no | not testable | R proxy, no CLI | kNN vote | not counted | host not an API; manual download |
| memorycd | cross-domain rating proxy | HF WZDavid/MemoryCD @ 14b934c | users file 108 MB (390 MB with meta) | Amazon-derived, no card licence | no | not testable | R proxy, no CLI | kNN MAE | not counted | over the 50 MB cap and no clear licence |
| synthetic | smoke test | generated | n/a | n/a | n/a | yes | R + Q, CLI | any | 6 for (2 items, 3 facts) | |
| statemembench | evolving-state tracking | arXiv 2608.19652 | n/a | unknown | no | registry marks it unreleased, no adapter | none | n/a | 234 scenarios claimed | wait for the data release |

## 3. Loaded counts (direct loader calls, no model)

LoCoMo, per conversation (all cats / cats 1-4): conv-26 199/152, conv-30 105/81, conv-41 193/152,
conv-42 260/199, conv-43 242/178, conv-44 158/123, conv-47 190/150, conv-48 239/191, conv-49 196/156,
conv-50 204/158. Turns per conversation 369-689.

MemoryAgentBench items (turns / questions): sh and mh at 6k = 455/100, 32k = 2,310/100, 64k = 4,580/100,
262k = 18,332/100. ConvoMem sample: 1,096 items, one question each, 61,777 turns. HaluMem: 20 users,
145-190 questions each. BEAM: 20 conversations x 20 probes (2 per ability per conversation). PerLTQA: 31 characters, 40-394 questions each. PersonaBench: 2 communities x
persons x 4 noise levels.

## 4. LoCoMo category 5 and the judge path

- Loader (`datasets/locomo.py`): category 5 gets `meta.abstention=True`, `gold = ABSTENTION_GOLD`
  ("Not mentioned in the conversation"), never the file's `adversarial_answer` (kept only as
  `meta.adversarial_answer`), and **no gold turn ids** (its `evidence` is the distractor source, stored as
  `meta.distractor_evidence`). All 446 cat-5 questions load that way (446 abstention, 446 without gold turns). The
  manifest note is `cat5-gold=abstention-v1: cat 5 gold is the refusal ..., never adversarial_answer; cat 5
  has no R@k gold`. Cats 1-4 have gold turns except 4 cat-3 questions.
- Judge: the default `--judge-prompt rubric` routes `abstention` questions to `memspine/abstention`
  (`handles_abstention=True`). The runner (`runner.py` ~953) and `experiments.py` (~652) refuse a run that has
  abstention questions under a judge without `handles_abstention` (so `omnimemeval` and `mem0-official`, and
  `constraint`, cannot be used with cat 5). `GuardedJudge` keeps `handles_abstention` from the inner judge.
- Date check (`--judge-date-check`): runs before the LLM judge only if the gold parses as one single day.
  The cat-5 gold is not a date and refusals / denials are never credited, so the pre-check cannot
  misfire on cat 5; it falls through to the abstention judge. The empty-answer guard scores an empty
  answer as wrong, which is also correct for abstention (a refusal must be written out).
- Reporting: `_adversarial_split` in `runner.py` reports the headline without cat 5 next to the cat-5
  accuracy; in retrieval mode the cat-5 accuracy is `None`. So for cat 5, retrieval runs measure nothing
  (no gold), only QA runs do.
- `run.sh` default `--categories 1,2,3,4` excludes cat 5. For the abstention row use `--categories all`
  and `--questions` = the all-category count.
- Caveat for QA: a 9B reader that refuses by default scores well on cat 5 and loses on cats 1-4, so
  report both numbers (the harness already does).

## 5. What run.sh supports, and what blocks other datasets

Flags: `--arm --run-id --mode --topk --items --max-queries --questions --flags --forensics --engine-src
--data --dataset --categories --batch-turns --force --think` (`--help` prints only the header comment).

- It does **not** hard-code locomo: `--dataset` (default `locomo`) is forwarded to `_launch.py c0-1`. But:
  1. `cli.py --dataset` choices restrict it to the 6 names above (needs the branch per adapter).
  2. The expected question count is derived only for LoCoMo cats 1-4 (1,540 / 152 for `--items 1`), so
     every other dataset, and LoCoMo with other categories, must pass `--questions N` (it is the exact
     row-count check; with `--max-queries` it becomes an upper bound).
  3. `--data` must be a **file**: `[ -f "$data" ]` rejects a directory, so ConvoMem and every
     directory-rooted adapter (perltqa, prefeval, personabench, tofu, muse_news, cpb, asb) fail at the
     check. Change `-f` to `-e` (and the same in the default-path loop) to allow them.
  4. The default data path loop only knows `locomo10.json`; always pass `--data` (relative to `evals/`,
     because the script `cd`s there; e.g. `data/mab/Conflict_Resolution.parquet`).
  5. `--categories` is always forwarded but only LoCoMo uses it.
  6. `--with-memspine --only-systems memspine` and the Ollama reader/judge (`READER_MODEL`, `JUDGE_MODEL`,
     `BASE_URL`, default `qwen3.5:9b`) are fixed; judge choice and item selection go through `--flags`
     (`--judge-prompt`, `--item-ids`, `--judge-guards`, `--judge-date-check`, `--qa-prompt`).
  7. `locomo_plus` also needs `--locomo-path ../data/locomo10.json` through `--flags`.
  8. Datasets with non-R@k scorers (kNN, firewall rates, erase-and-verify, ASB) are not answered by the
     runner's `results.jsonl` at all; they need a small driver script around `run_c0_1` or the pure
     functions in each adapter module.
- Splits: `memspine_evals split` supports only locomo / longmemeval / synthetic. The LoCoMo split is saved
  in `evals/analysis/locomo_split.json` (dev conv-26, 30, 41, 42; held-out conv-43, 44, 47, 48, 49, 50).
  For the other benchmarks I propose the same rule, whole items on one side only, and the id lists should be
  written to a saved split file before any tuning.

## 6. Time model

- Retrieval-only: 5 s per question (reranker, from the LoCoMo runs). QA: 5 s + 1 s per answer = 6 s per
  question, about 7 s with an LLM judge call (alias and deterministic judges stay at 6 s). Ingestion is
  not in these numbers; for the LoCoMo-sized conversations it is amortised, but the 262k MemoryAgentBench
  items (18k facts each) are not covered by the 5 s figure and need a measured pilot.

## 7. Recommended suite

### Tier 1: runnable now (data local, CLI-wired, judge exists)

| benchmark | dev slice | held-out slice | R time (dev / held) | QA time (dev / held) |
|---|---|---|---|---|
| LoCoMo cats 1-4 (+ cat 5 in QA) | conv-26, 30, 41, 42: 584 q (757 with cat 5) | conv-43, 44, 47, 48, 49, 50: 956 q (1,229 with cat 5) | 49 min / 80 min | 757 q: 88 min; 1,229 q: 2.4 h (at 7 s) |
| MemoryAgentBench Conflict_Resolution | 6k + 32k items (sh and mh): 400 q | 64k + 262k items: 400 q | 33 min / 33 min + ingestion | 40 min / 40 min + ingestion |
| ConvoMem sample (1,096 q) | every 5th item by sorted id: about 220 q | the other about 876 q (cap at 440 for QA) | 18 min / 73 min | 26 min / 51 min (440 q at 7 s) |

The ConvoMem split ids are not saved anywhere yet; create the file first (rule: sort item ids, items
at index % 5 == 0 are dev).

### Tier 2: small download, data already fetched, needs CLI/driver wiring (retrieval only)

| benchmark | dev slice | held-out slice | R time (dev / held) |
|---|---|---|---|
| BEAM 100K | conversations 1-5: 100 q (10 per ability) | conversations 6-20: 300 q | 8 min / 25 min |
| PersonaBench | community_0: 532 q (drop Subjective: about 432) | community_1: 520 q (about 420) | 36 min / 35 min |
| PerLTQA en_v2 | first 6 characters, capped at 40 q each: 240 q | remaining 25 characters, capped at 20 q each: 500 q (needs a per-character cap, not in the adapter) | 20 min / 42 min |
| PrefEval | per_topic 5 over 3 forms x 20 topics: 300 q | per_topic 10 from the remaining rows: 600 q (disjoint ids needed) | 25 min / 50 min |
| CPB live | stages 1-3: about 50 q | stages 4-6: about 110 q | 4 min / 9 min |
| HaluMem proxy | 4 users: 705 q | 16 users: 2,762 q (cap 800) | 59 min / 67 min at 800; ingestion of about 3,000 turns per user dominates |
| TOFU forget01, MUSE-News privleak, ASB | single small probes, no dev / held-out split meaningful | | under 5 min each, driver script |
| HaluMem QA | needs an official-style Correct / Hallucination / Omission judge first | | not scheduled |

### Tier 3: large, gated, unlicensed or unavailable

- ConvoMem beyond the sample: 1.2 GB evidence_questions (27.5 GB full); fetch more strata only if the
  sample is not enough.
- HaluMem-Long (106.5 MB), BEAM 500K / 1M / 10M (34 / 66 / 344 MB), MemoryAgentBench other three splits
  (73 MB; no adapter), MemoryCD (391 MB, no card licence).
- LoCoMo-Plus (1.2 MB) and OP-Bench (5.1 MB): small but **no licence**; do not run until clearer terms.
- LaMP-2: manual fetch from a university host, research use only.
- StateMemBench: unreleased.

## 8. Run commands

Placeholders: `ARM` = any arm in `evals/arms/` (the recent LoCoMo arm is `qs-eq06-rq4b4-fix`); all paths
are relative to `evals/` because `run.sh` changes into it; run as `bash evals/run.sh ...` from the repo root.
`READER_MODEL`, `JUDGE_MODEL`, `BASE_URL` come from the environment as usual. Run IDs must be new or use `--force`.

### LoCoMo (works today)

```bash
DEV=conv-26,conv-30,conv-41,conv-42
HELD=conv-43,conv-44,conv-47,conv-48,conv-49,conv-50
# retrieval, cats 1-4
bash evals/run.sh --arm ARM --run-id lc-dev-ret --mode retrieval --topk 10 --questions 584  --batch-turns 32 --flags "--item-ids $DEV"
bash evals/run.sh --arm ARM --run-id lc-ho-ret  --mode retrieval --topk 10 --questions 956  --batch-turns 32 --flags "--item-ids $HELD"
# QA with cat 5 (abstention); drop --categories all and use --questions 584 / 956 for cats 1-4 only
bash evals/run.sh --arm ARM --run-id lc-dev-qa --mode qa --topk 10 --categories all --questions 757 --forensics --batch-turns 32 \
  --flags "--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check --item-ids $DEV"
bash evals/run.sh --arm ARM --run-id lc-ho-qa  --mode qa --topk 10 --categories all --questions 1229 --forensics --batch-turns 32 \
  --flags "--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check --item-ids $HELD"
```

### MemoryAgentBench Conflict_Resolution (works today)

```bash
DEV=factconsolidation_sh_6k,factconsolidation_mh_6k,factconsolidation_sh_32k,factconsolidation_mh_32k
HELD=factconsolidation_sh_64k,factconsolidation_mh_64k,factconsolidation_sh_262k,factconsolidation_mh_262k
bash evals/run.sh --arm ARM --run-id mab-dev-ret --mode retrieval --topk 10 --dataset memoryagentbench \
  --data data/mab/Conflict_Resolution.parquet --questions 400 --flags "--item-ids $DEV"
bash evals/run.sh --arm ARM --run-id mab-dev-qa  --mode qa --topk 10 --dataset memoryagentbench \
  --data data/mab/Conflict_Resolution.parquet --questions 400 --flags "--judge-prompt alias --item-ids $DEV"
# held-out: same with $HELD and run ids mab-ho-*
```

### ConvoMem sample (works after one run.sh edit: `-f` -> `-e` for `--data`)

```bash
# ids: python -c "list item ids sorted; DEV = ids[::5]; HELD = the rest" -> write to evals/analysis/convomem_split.json
bash evals/run.sh --arm ARM --run-id cm-dev-ret --mode retrieval --topk 10 --dataset convomem --data data/convomem \
  --questions 220 --flags "--per-stratum 1000 --item-ids $(paste -sd, convomem_dev_ids.txt)"
bash evals/run.sh --arm ARM --run-id cm-ho-qa --mode qa --topk 10 --dataset convomem --data data/convomem \
  --questions 440 --flags "--per-stratum 1000 --judge-prompt rubric --item-ids $(paste -sd, convomem_held_ids_440.txt)"
```

`--per-stratum 1000` keeps every question in the sample; the default of 20 would select a different subset.

### Tier 2 (not runnable until `cli.py` has the branches; the command would then be)

```bash
bash evals/run.sh --arm ARM --run-id beam-dev-ret --mode retrieval --topk 10 --dataset beam \
  --data data/beam/data/100K-00000-of-00001.parquet --questions 100 --flags "--item-ids 1,2,3,4,5"
bash evals/run.sh --arm ARM --run-id pb-dev-ret --mode retrieval --topk 10 --dataset personabench \
  --data data/personabench --questions 532      # needs the -f/-e edit and community filter via item ids
# perltqa (data/perltqa), prefeval (data/prefeval), cpb_live (data/cpb), halumem_proxy
# (data/halumem/HaluMem-Medium.jsonl) follow the same form; each needs --questions and --item-ids.
```

## 9. Decisions needed from the user

1. **Wiring approval.** Tier 2 and part of tier 1 need edits to `cli.py` (dataset branches, split support) and
   `run.sh` (`-f` -> `-e`, question counts), which I was told not to touch. Which of the two files may be
   edited, and after the GPU queue drains?
2. **Licences.** Everything fetched is open or non-commercial research (MIT, CC BY, CC BY-SA, CC BY-NC, CC BY-NC-SA,
   CC BY-NC-ND). HaluMem is ND: usable locally, but no derived copies; the PersonaBench terms say not to train
   competing models; the SA / NC terms matter if the processed data or the numbers' tables are published with data.
   Confirm that publishing scores only (no data) is the intended use.
3. **Unlicensed small sets** (LoCoMo-Plus, OP-Bench): approve running them locally without a licence, or leave out.
4. **Larger downloads** (all over 50 MB, so not fetched): HaluMem-Long 107 MB, BEAM 500K / 1M 100 MB, MemoryAgentBench
   other splits 73 MB, MemoryCD 391 MB, more ConvoMem strata (up to 1.2 GB). None is gated on HF (all `gated=False`).
5. **LaMP-2** needs a manual download from the UMass host; StateMemBench has no data yet.
6. **Loader fixes to decide on:** flag ConvoMem abstention items and HaluMem Memory Boundary as `abstention`
   so they use the abstention judge; add an official-style HaluMem judge; add a per-character query cap to PerLTQA /
   PrefEval so a retrieval run is bounded.
7. **ConvoMem and MemoryAgentBench split files:** approve the proposed rule (every 5th sorted id as dev; whole size
   tiers as dev / held-out) before any tuning on them.
