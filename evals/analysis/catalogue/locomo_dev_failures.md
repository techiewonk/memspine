# LoCoMo dev failure catalogue (xb-loc-dev, 154 wrong of 757)

One row per wrong question. Text-free by design (no question, gold or answer text: the register keeps per-question corpus-derived text out of git); `evals/runs/_analysis/locomo_dev_failures_full.jsonl` (gitignored) holds the same rows with text. Schema: `evals/schemas/failure_catalogue.schema.json`. Stage codes: a write path, b recall, c fusion/rerank/assembly cut, d reader, e judge, f gold/dataset. Analysis: `FAILURE_FORENSICS_BASELINE_2026-10-10.md`.

| query_id | cat | code | stage | lost at (gold turns) | gaps | built feature (screen) | errata |
|---|---|---|---|---|---|---|---|
| conv-26:0-3 | multi-hop | c1 | c | D2:8:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-26:0-5 | temporal | f | f | D2:1:in_context | A5 | evals/analysis/locomo_errata.json | gold_error |
| conv-26:0-11 | multi-hop | c1 | c | D3:13:in_context,D4:3:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-26:0-22 | open-domain | d-inf | d | D6:9:in_context | B3,C3,C4,R2-6,I3,I28 | grounded_generic_infer / routed_generic (r3c-routed) |  |
| conv-26:0-24 | multi-hop | e | e | D7:22:in_context,D5:4:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-26:0-26 | temporal | d-ref | d | D7:8:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal |  |
| conv-26:0-34 | multi-hop | b | b | D9:2:in_context,D3:3:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-26:0-35 | temporal | d-date | d | D9:1:in_context | C5,C8,B4,I12,I57 | relative_dates_anchored annotates; judge-date-check only gra |  |
| conv-26:0-40 | multi-hop | d-count | d | D10:8:in_context,D6:16:in_context | C6,I31,I56 | routed_generic list branch (partial); scr-dedupe (partial) |  |
| conv-26:0-42 | open-domain | d-inf | d | D10:12:rescued,D10:14:rescued | B3,C3,C4,R2-6,I3,I28 | grounded_generic_infer / routed_generic (r3c-routed) |  |
| conv-26:0-43 | multi-hop | d-detail | d | D11:12:rescued,D11:8:in_context,D9:14:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-26:0-49 | temporal | d-ref | d | D12:15:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal |  |
| conv-26:0-59 | open-domain | c1 | c | D14:19:fusion,D12:1:in_context | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-26:0-64 | open-domain | d-inf | d | D15:28:rescued | B3,C3,C4,R2-6,I3,I28 | grounded_generic_infer / routed_generic (r3c-routed) |  |
| conv-26:0-66 | multi-hop | d-list | d | D16:4:in_context,D10:12:in_context | C6,I4,I31,B1 | read.list_trigger=intent; routed_generic list branch; scr-de |  |
| conv-26:0-69 | open-domain | b | b | D16:18:recall,D13:16:fusion,D7:4:in_context | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-26:0-70 | multi-hop | e | e | D17:19:in_context,D15:13:recall | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user | evidence_label_error |
| conv-26:0-71 | multi-hop | d-ref | d | D7:11:rescued,D17:10:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal |  |
| conv-26:0-124 | single-hop | e | e | D13:4:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-26:0-135 | single-hop | e | e | D17:8:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-26:0-151 | single-hop | d-detail | d | D18:17:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 | evidence_label_error |
| conv-26:0-154 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-161 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-165 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-166 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-168 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-170 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-172 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-26:0-175 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-176 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-179 | adversarial | c5-entity | d | - | I60 | none |  |
| conv-26:0-180 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-181 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-182 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-184 | adversarial | c5-answerable | f | - | A5,I62 | evals/analysis/locomo_errata.json | NEW: cat 5 but answerable: Caroline plays acoustic guitar (D15:21) |
| conv-26:0-186 | adversarial | c5-answerable | f | - | A5,I62 | evals/analysis/locomo_errata.json | NEW: cat 5 but answerable: Caroline names "Brave" by Sara Bareilles (D |
| conv-26:0-187 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-26:0-194 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-30:1-3 | multi-hop | b | b | D1:2:fusion,D1:3:recall,D1:4:recall,D2:1:fusion | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-30:1-9 | multi-hop | f | f | D2:5:in_context,D15:1:in_context | A5 | evals/analysis/locomo_errata.json | gold_error |
| conv-30:1-20 | temporal | d-distr | d | D12:1:in_context | C1,R2-4,B10 | scr-rctx1/2 |  |
| conv-30:1-23 | multi-hop | b | b | D5:5:in_context,D16:3:recall,D8:4:fusion,D13:4:fusion | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-30:1-31 | multi-hop | c1 | c | D1:2:rescued,D15:13:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-30:1-43 | single-hop | d-detail | d | D1:25:rescued | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 | evidence_label_error |
| conv-30:1-44 | single-hop | b | b | D1:26:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod | evidence_label_error |
| conv-30:1-48 | single-hop | d-distr | d | D3:4:in_context | C1,R2-4,B10 | scr-rctx1/2 | evidence_label_error |
| conv-30:1-49 | single-hop | d-detail | d | D3:6:in_context,D3:8:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-30:1-57 | single-hop | f | f | D7:5:rescued | A5 | evals/analysis/locomo_errata.json | gold_error |
| conv-30:1-63 | single-hop | d-ref | d | D12:1:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal | NEW: question date (May 23) contradicts the transcript (May 27); gold  |
| conv-30:1-71 | single-hop | e | e | D15:6:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-30:1-83 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-30:1-84 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-30:1-85 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-30:1-91 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-30:1-92 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-30:1-95 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-8 | open-domain | b | b | D5:5:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-41:2-11 | multi-hop | b | b | D7:5:fusion,D6:5:rescued,D27:8:recall,D21:19:fusion | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-41:2-19 | multi-hop | d-list | d | D11:1:in_context,D4:2:in_context | C6,I4,I31,B1 | read.list_trigger=intent; routed_generic list branch; scr-de |  |
| conv-41:2-21 | multi-hop | e | e | D11:5:in_context,D12:17:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-41:2-31 | temporal | d-ref | d | D17:1:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal |  |
| conv-41:2-35 | multi-hop | c1 | c | D19:23:in_context,D18:3:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-41:2-40 | multi-hop | b | b | D8:4:rescued,D22:7:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-41:2-41 | open-domain | b | b | D22:15:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-41:2-42 | multi-hop | c1 | c | D14:21:fusion,D23:1:in_context | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-41:2-44 | multi-hop | e | e | D25:2:in_context,D24:6:in_context,D28:5:recall | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user | evidence_label_error |
| conv-41:2-45 | open-domain | b | b | D24:3:recall,D7:2:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-41:2-49 | multi-hop | b | b | D26:1:in_context,D25:19:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod | evidence_label_error |
| conv-41:2-50 | open-domain | b | b | D26:6:recall,D2:14:recall,D3:5:recall,D4:6:rescued | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-41:2-64 | open-domain | b | b | D32:14:recall,D5:8:recall,D11:10:recall,D27:4:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-41:2-68 | single-hop | d-ref | d | D1:3:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal | NEW: question year (Dec 2023) contradicts the transcript (Dec 2022); g |
| conv-41:2-69 | single-hop | f | f | D2:1:in_context | A5 | evals/analysis/locomo_errata.json | gold_error |
| conv-41:2-85 | single-hop | d-detail | d | D10:13:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-41:2-99 | single-hop | d-list | d | D15:13:in_context | C6,I4,I31,B1 | read.list_trigger=intent; routed_generic list branch; scr-de |  |
| conv-41:2-107 | single-hop | c1 | c | D17:12:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-41:2-120 | single-hop | e | e | D23:1:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-41:2-154 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-41:2-156 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-160 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-161 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-168 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-169 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-172 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-174 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-176 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-41:2-185 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-41:2-186 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-41:2-189 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-1 | multi-hop | b | b | D1:10:in_context,D1:11:rescued,D1:12:in_context,D3:4:recall,D4:9:fusio | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-4 | open-domain | d-inf | d | D2:23:in_context | B3,C3,C4,R2-6,I3,I28 | grounded_generic_infer / routed_generic (r3c-routed) |  |
| conv-42:3-5 | multi-hop | d-list | d | D1:10:in_context,D2:25:in_context | C6,I4,I31,B1 | read.list_trigger=intent; routed_generic list branch; scr-de |  |
| conv-42:3-11 | multi-hop | e | e | D4:4:fusion,D5:11:in_context,D2:23:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-42:3-12 | open-domain | d-inf | d | D5:11:in_context,D2:23:in_context | B3,C3,C4,R2-6,I3,I28 | grounded_generic_infer / routed_generic (r3c-routed) |  |
| conv-42:3-14 | open-domain | b | b | D7:1:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-19 | multi-hop | d-count | d | D8:4:in_context,D11:3:in_context | C6,I31,I56 | routed_generic list branch (partial); scr-dedupe (partial) |  |
| conv-42:3-22 | temporal | d-date | d | D12:13:in_context,D12:14:rescued | C5,C8,B4,I12,I57 | relative_dates_anchored annotates; judge-date-check only gra |  |
| conv-42:3-23 | multi-hop | d-detail | d | D14:1:in_context,D3:1:in_context,D2:7:rescued,D24:12:in_context,D24:13 | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-42:3-24 | temporal | f | f | D14:20:in_context | A5 | evals/analysis/locomo_errata.json | gold_error |
| conv-42:3-31 | temporal | d-date | d | D17:1:in_context | C5,C8,B4,I12,I57 | relative_dates_anchored annotates; judge-date-check only gra |  |
| conv-42:3-34 | multi-hop | b | b | D3:17:recall,D19:14:in_context,D19:16:rescued | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-35 | temporal | d-date | d | D19:9:in_context | C5,C8,B4,I12,I57 | relative_dates_anchored annotates; judge-date-check only gra |  |
| conv-42:3-41 | temporal | d-date | d | D22:1:in_context | C5,C8,B4,I12,I57 | relative_dates_anchored annotates; judge-date-check only gra |  |
| conv-42:3-42 | multi-hop | b | b | D3:17:recall,D10:1:in_context,D22:8:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-43 | temporal | d-ref | d | D17:14:in_context,D22:9:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal |  |
| conv-42:3-49 | multi-hop | d-count | d | D14:1:in_context,D24:12:in_context | C6,I31,I56 | routed_generic list branch (partial); scr-dedupe (partial) |  |
| conv-42:3-52 | multi-hop | d-count | d | D15:1:in_context,D25:2:in_context | C6,I31,I56 | routed_generic list branch (partial); scr-dedupe (partial) |  |
| conv-42:3-54 | temporal | d-date | d | D25:1:rescued | C5,C8,B4,I12,I57 | relative_dates_anchored annotates; judge-date-check only gra |  |
| conv-42:3-55 | multi-hop | b | b | D4:6:recall,D7:6:fusion,D11:11:in_context,D26:3:fusion,D26:7:fusion,D2 | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-60 | open-domain | d-inf | d | D27:23:rescued | B3,C3,C4,R2-6,I3,I28 | grounded_generic_infer / routed_generic (r3c-routed) |  |
| conv-42:3-61 | multi-hop | c1 | c | D22:2:fusion,D27:21:rescued,D27:15:rescued | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-42:3-62 | multi-hop | c1 | c | D14:1:fusion,D18:5:in_context | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-42:3-66 | open-domain | b | b | D5:8:recall,D19:3:rescued,D25:19:recall,D28:25:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-68 | open-domain | c1 | c | D7:6:fusion,D11:5:in_context,D14:21:rescued,D28:22:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-42:3-70 | multi-hop | d-list | d | D25:21:in_context,D25:23:in_context,D28:31:in_context | C6,I4,I31,B1 | read.list_trigger=intent; routed_generic list branch; scr-de |  |
| conv-42:3-73 | open-domain | d-ref | d | D28:22:in_context | C8,C10,I1,I3,I22,I61 | --qa-prompt routed_generic (r3c-routed); --retry-refusal |  |
| conv-42:3-74 | multi-hop | b | b | D1:16:recall,D3:17:recall,D15:14:recall,D15:15:recall,D19:15:recall,D1 | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-75 | multi-hop | c1 | c | D3:4:rescued,D3:10:rescued,D21:10:in_context,D3:12:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-42:3-79 | multi-hop | b | b | D2:3:in_context,D4:10:in_context,D5:1:fusion,D12:13:recall,D12:14:reca | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-80 | multi-hop | d-count | d | D1:3:in_context,D10:4:in_context,D14:8:in_context,D17:1:in_context,D19 | C6,I31,I56 | routed_generic list branch (partial); scr-dedupe (partial) |  |
| conv-42:3-81 | multi-hop | b | b | D10:9:in_context,D10:11:rescued,D19:8:in_context,D20:2:in_context,D20: | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-83 | multi-hop | b | b | D18:8:recall,D26:12:in_context,D14:16:in_context | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-84 | open-domain | c2 | c | D20:1:rerank,D21:1:rerank | B9,B10,I9 | scr-rctx1/2 |  |
| conv-42:3-85 | open-domain | d-detail | d | D29:1:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-42:3-88 | single-hop | b | b | D1:18:rescued,D:recall,D1:20:rescued | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod | NEW: evidence list holds a malformed turn id "D" |
| conv-42:3-91 | single-hop | f | f | D9:14:in_context | A5 | evals/analysis/locomo_errata.json | needs_image |
| conv-42:3-92 | single-hop | f | f | D10:2:in_context | A5 | evals/analysis/locomo_errata.json | needs_image |
| conv-42:3-94 | single-hop | d-detail | d | D12:13:in_context,D12:14:rescued | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-42:3-95 | single-hop | c1 | c | D27:22:fusion,D27:23:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-42:3-113 | single-hop | d-detail | d | D9:10:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-42:3-116 | single-hop | d-detail | d | D9:10:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-42:3-122 | single-hop | c1 | c | D13:9:fusion | B7,B10,I9 | read.candidate_pool; chunked rerank (I9); rerank_context 1/2 |  |
| conv-42:3-134 | single-hop | b | b | D16:10:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-148 | single-hop | d-detail | d | D18:12:rescued | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-42:3-151 | single-hop | b | b | D19:9:recall | B1,B7,B8,R2-2,I4 | read.list_trigger=intent (scr-intent); read.speaker_vote_mod |  |
| conv-42:3-183 | single-hop | e | e | D27:23:in_context | A1,A2,A3,I13,I58 | --judge-guards (partly); A1 deferred by the user |  |
| conv-42:3-195 | single-hop | d-detail | d | D28:29:in_context | C1,C2,R2-4,B10,I6 | read.replay_window_unit=tokens (I6); scr-rctx1/2 |  |
| conv-42:3-200 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-210 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-212 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-215 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-42:3-216 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-42:3-218 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-220 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-42:3-234 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-235 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-236 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-237 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-238 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-239 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-241 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-42:3-243 | adversarial | c5-near | d | - | I32,I29,I30,I3 | --no-record-hint (r3-norecord); read.relevance_gate (r3-relg |  |
| conv-42:3-244 | adversarial | c5-answerable | f | - | A5,I62 | evals/analysis/locomo_errata.json | NEW: cat 5 but answerable: Joanna relies on her stuffed dog Tilly (D24 |
| conv-42:3-245 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
| conv-42:3-256 | adversarial | c5-swap | d | - | I39,I47,I54,I59 | read.speaker_vote_mode=subject + perspective axes (scr-persp |  |
