"""R14 DEEP-READ: aggregate the per-question CSV into tables and write r14_reader_judge.json.

Input : evals/analysis/r14_reader_judge_features.csv   (made by r14_reader_judge.py)
        evals/analysis/locomo_errata.json
Output: docs/v2/research/r14_reader_judge.json
No dataset text is read or written. Run from the repo root with the memspine .venv python.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import LogisticRegression

WT = Path(__file__).resolve().parents[2]
CSV = WT / "evals/analysis/r14_reader_judge_features.csv"
OUT = WT / "docs/v2/research/r14_reader_judge.json"


def ids(df: pd.DataFrame) -> list[str]:
    return [f"{a}/{b}" for a, b in zip(df["item"], df["qid"])]


def logit(df: pd.DataFrame, cols: list[str], reps: int = 200) -> dict:
    X = pd.get_dummies(
        df[cols],
        columns=[c for c in cols if not pd.api.types.is_numeric_dtype(df[c])],
        drop_first=True,
    ).astype(float)
    Xs = (X - X.mean()) / X.std().replace(0, 1)
    m = LogisticRegression(C=1e6, max_iter=3000).fit(Xs, df["correct"])
    rng = np.random.default_rng(1)
    B = []
    for _ in range(reps):
        i = rng.integers(0, len(df), len(df))
        B.append(
            LogisticRegression(C=1e6, max_iter=3000).fit(Xs.iloc[i], df["correct"].iloc[i]).coef_[0]
        )
    se = np.std(B, axis=0)
    return {
        "n": len(df),
        "coef_per_sd": {c: round(float(b), 3) for c, b in zip(Xs.columns, m.coef_[0])},
        "z": {c: round(float(b / s), 1) for c, b, s in zip(Xs.columns, m.coef_[0], se)},
    }


def acc(df: pd.DataFrame) -> dict:
    return {"n": len(df), "acc": round(float(df["correct"].mean()), 3) if len(df) else None}


def main() -> None:
    d = pd.read_csv(CSV)
    for c in [
        "stage",
        "rclass",
        "errata_tags",
        "hand_fn",
        "hand_fp",
        "fn_flags",
        "fp_flags",
        "answer_source",
    ]:
        d[c] = d[c].fillna("")
    wrong = d[d.correct == 0]
    A = d[d.all_gold_in_ctx == 1]
    W = A[A.correct == 0]
    T: dict = {}

    # ------------------------------------------------------------------ funnel
    T["funnel"] = {
        "questions": len(d),
        "correct": int(d.correct.sum()),
        "wrong": len(wrong),
        "acc": round(float(d.correct.mean()), 4),
        "all_gold_in_context": len(A),
        "acc_all_gold_in_context": round(float(A.correct.mean()), 4),
        "wrong_with_all_gold_in_context": len(W),
        "of_which_reader_stage_d_label": int(W.stage.str.startswith("d").sum()),
        "of_which_gold_error_label_f": int((W.stage == "f").sum()),
        "of_which_judge_label_e": int((W.stage == "e").sum()),
        "wrong_without_all_gold": int(len(wrong) - len(W)),
        "method": "measured: all_gold_in_ctx = every evidence id that exists in the conversation is among the context turn ids; labels from evals/analysis/locomo_problem_labels_clean_jina_full.txt",
    }
    T["by_category"] = {
        c: {
            "n": len(g),
            "acc": round(float(g.correct.mean()), 3),
            "acc_gold_in_ctx": acc(g[g.all_gold_in_ctx == 1])["acc"],
            "n_gold_in_ctx": int((g.all_gold_in_ctx == 1).sum()),
        }
        for c, g in d.groupby("cat")
    }

    # ------------------------------------------------------------------ position / length / rank / multi-hop
    S = A[(A.n_ev_valid == 1)].dropna(subset=["gold_pos_first", "gold_best_final_rank"]).copy()
    S["pbin"] = pd.cut(S.gold_pos_first, [-0.01, 0.2, 0.4, 0.6, 0.8, 1.0]).astype(str)
    T["position_single_evidence_in_ctx"] = {
        "context_order": "chronological in 1540 of 1540 contexts (position = position in time, not rank)",
        "by_quintile_of_gold_position": {k: acc(g) for k, g in S.groupby("pbin")},
        "early_third_vs_late_third": {
            "early": acc(S[S.gold_pos_first <= 1 / 3]),
            "late": acc(S[S.gold_pos_first >= 2 / 3]),
            "fisher_p": round(
                float(
                    stats.fisher_exact(
                        [
                            [
                                int(S[S.gold_pos_first <= 1 / 3].correct.sum()),
                                int(
                                    (S.gold_pos_first <= 1 / 3).sum()
                                    - S[S.gold_pos_first <= 1 / 3].correct.sum()
                                ),
                            ],
                            [
                                int(S[S.gold_pos_first >= 2 / 3].correct.sum()),
                                int(
                                    (S.gold_pos_first >= 2 / 3).sum()
                                    - S[S.gold_pos_first >= 2 / 3].correct.sum()
                                ),
                            ],
                        ]
                    )[1]
                ),
                3,
            ),
        },
        "logit_single_ev_cat_nctx_pos_rank": logit(
            S, ["cat", "n_ctx", "gold_pos_first", "gold_best_final_rank"]
        ),
        "method": "measured; logistic regression of correct on standardised covariates, bootstrap SE (200 resamples)",
    }
    A2 = A.dropna(subset=["gold_pos_first", "gold_best_final_rank"]).copy()
    A2["n_ev_c"] = A2.n_ev_valid.clip(upper=4)
    A2["n_sess_c"] = A2.n_ev_sessions.clip(upper=3)
    T["context_length"] = {
        "by_n_context_turns": {
            k: acc(g) for k, g in A.groupby(pd.cut(A.n_ctx, [0, 40, 50, 60, 200]).astype(str))
        },
        "single_ev_by_tertile_n_ctx": {str(k): acc(g) for k, g in S.groupby(pd.qcut(S.n_ctx, 3))},
        "single_ev_by_tertile_ctx_tokens": {
            str(k): acc(g) for k, g in S.groupby(pd.qcut(S.ctx_tokens, 3))
        },
        "logit_all_in_ctx": logit(
            A2, ["cat", "n_ev_c", "n_sess_c", "n_ctx", "gold_pos_first", "gold_best_final_rank"]
        ),
        "method": "measured",
    }
    clean = A[~A.stage.isin(["f", "e"])]
    T["retrieval_rank_of_gold"] = {
        "clean_in_ctx_rank_le3": acc(clean[clean.gold_best_final_rank <= 3]),
        "clean_in_ctx_rank_gt3": acc(clean[clean.gold_best_final_rank > 3]),
        "wrong_in_ctx_best_rank_gt3": int((W.gold_best_final_rank > 3).sum()),
        "wrong_in_ctx_best_rank_le3": int((W.gold_best_final_rank <= 3).sum()),
        "wrong_in_ctx_no_final_rank": int(W.gold_best_final_rank.isna().sum()),
        "note": "'clean' drops stage f (gold error) and e (judge). Hits-first and hit_blocks presentation were screened and rejected (GAP_REGISTER R2-4), so this is an association, not a presentation lever.",
        "method": "measured",
    }
    T["multi_turn"] = {
        "by_n_evidence_turns": {int(k): acc(g) for k, g in A.groupby("n_ev_valid")},
        "by_n_evidence_sessions": {int(k): acc(g) for k, g in A.groupby("n_ev_sessions")},
        "stage_d_with_ge2_evidence_turns": int(
            ((d.stage.str.startswith("d")) & (d.n_ev_valid >= 2)).sum()
        ),
        "stage_d_with_ge2_sessions": int(
            ((d.stage.str.startswith("d")) & (d.n_ev_sessions >= 2)).sum()
        ),
        "stage_d_total": int(d.stage.str.startswith("d").sum()),
        "multi_evidence_questions": {
            "n": int((d.n_ev_valid >= 2).sum()),
            "acc": round(float(d[d.n_ev_valid >= 2].correct.mean()), 3),
            "all_gold_in_ctx_share": round(float(d[d.n_ev_valid >= 2].all_gold_in_ctx.mean()), 3),
        },
        "method": "measured",
    }
    lr = pd.cut(A.len_ratio, [0, 1, 2, 4, 8, 16, 1000]).astype(str)
    T["verbosity"] = {
        "in_ctx_by_answer_over_gold_word_ratio": {k: acc(g) for k, g in A.groupby(lr)},
        "wrong_in_ctx_ratio_gt8": int((W.len_ratio > 8).sum()),
        "wrong_in_ctx": len(W),
        "correct_in_ctx_ratio_gt8": int(((A.correct == 1) & (A.len_ratio > 8)).sum()),
        "correct_in_ctx": int((A.correct == 1).sum()),
        "method": "measured (word counts); a correlate of difficulty as much as a cause",
    }
    T["question_shape_in_ctx"] = {k: acc(g) for k, g in A.groupby("q_shape")}
    T["gold_type_in_ctx"] = {k: acc(g) for k, g in A.groupby("gold_type")}
    T["refusals"] = {
        "refusal_like_answers": int(d.refusal.sum()),
        "credited": int(d[d.refusal == 1].correct.sum()),
        "with_all_gold_in_ctx": int(((d.refusal == 1) & (d.all_gold_in_ctx == 1)).sum()),
        "retry_fired_rows": int(d.retried.sum()),
        "retry_fired_correct": int(d[d.retried == 1].correct.sum()),
    }
    T["caption"] = {
        "caption_only_gold_in_ctx": acc(A[A.caption_only_gold == 1]),
        "other_in_ctx": acc(A[A.caption_only_gold == 0]),
        "gold_turn_has_caption_in_ctx": acc(A[A.caption_gold == 1]),
        "no_caption_in_ctx": acc(A[A.caption_gold == 0]),
        "rule": "caption-only: a content token of the gold answer (not in the question) occurs in the gold turn's image caption and in none of its spoken text",
    }
    T["answer_source_in_ctx"] = {
        k: {"n": len(g), "wrong": int((g.correct == 0).sum())}
        for k, g in A.groupby("answer_source")
    }
    T["distractor_hand_check"] = {
        "reviewed": int(d.hand_distractor.notna().sum()),
        "true_distractor_use": int((d.hand_distractor == 1).sum()),
        "sibling_evidence_among_true": int(d.hand_sibling_evidence.sum()),
        "heuristic_wrong_in_ctx": int((W.answer_source == "distractor").sum()),
        "estimate_true_distractor": "about 43 (13/18 precision applied to 59; Wilson 95% interval for 13/18 is 0.49-0.88, i.e. 29-52)",
    }
    # ------------------------------------------------------------------ judge
    ht = lambda col, tiers: d[d[col].isin(tiers)]
    fn_c, fn_l, fn_b = (int((d.hand_fn == t).sum()) for t in ("clear", "likely", "borderline"))
    fp_c, fp_l, fp_b = (int((d.hand_fp == t).sum()) for t in ("clear", "likely", "borderline"))
    fn_central = fn_c + 0.75 * fn_l + 0.4 * fn_b
    fp_central = (
        fp_c + 0.7 * fp_l + 0.3 * fp_b + 3
    )  # +3 for the unflagged rows (0 of 70 sampled, rule of three)
    raw = int(d.correct.sum())
    adj = lambda fp, fn: round((raw - fp + fn) / len(d), 4)
    errata_ex = d.errata_tags.str.contains("gold_error|needs_image")
    T["judge"] = {
        "hand_read_wrong_rows": len(wrong),
        "hand_read_correct_rows": {
            "flagged_by_rules": int((d[d.correct == 1].fp_flags != "").sum()),
            "random_unflagged": 70,
        },
        "false_negative_hand": {
            "clear": fn_c,
            "likely": fn_l,
            "borderline": fn_b,
            "total": fn_c + fn_l + fn_b,
        },
        "false_positive_hand": {
            "clear": fp_c,
            "likely": fp_l,
            "borderline": fp_b,
            "total": fp_c + fp_l + fp_b,
            "unflagged_sample": "0 of 70 random unflagged corrects; rule-of-three 95% upper bound 4.3%, so up to ~40 unseen of 933 is not excluded; 3 added as central",
        },
        "central_estimate": {
            "false_negatives": round(fn_central, 1),
            "false_positives": round(fp_central, 1),
            "weights": "FN clear 1.0 / likely 0.75 / borderline 0.4; FP clear 1.0 / likely 0.7 / borderline 0.3 (judgement)",
        },
        "headline_raw": round(raw / len(d), 4),
        "headline_adjusted_central": adj(fp_central, fn_central),
        "headline_adjusted_range": [
            adj(45, 10),
            adj(17, 23),
        ],  # FP 17-45, FN 10-23 (see R14-JUDGE-3)
        "errata_excluded_rows": int(errata_ex.sum()),
        "errata_excluded_correct": int(d[errata_ex].correct.sum()),
        "headline_errata_excluded": round(
            float((raw - d[errata_ex].correct.sum()) / (len(d) - errata_ex.sum())), 4
        ),
        "deterministic_flag_recall": {
            "fn_rules_flagged_wrong_rows": int((wrong.fn_flags != "").sum()),
            "fn_clear_or_likely_flagged": int(
                ((d.hand_fn.isin(["clear", "likely"])) & (d.fn_flags != "")).sum()
            ),
            "fn_clear_or_likely_total": int(d.hand_fn.isin(["clear", "likely"]).sum()),
            "fp_rules_flagged_correct_rows": int((d[d.correct == 1].fp_flags != "").sum()),
            "fp_clear_or_likely_flagged": int(
                ((d.hand_fp.isin(["clear", "likely"])) & (d.fp_flags != "")).sum()
            ),
            "fp_clear_or_likely_total": int(d.hand_fp.isin(["clear", "likely"]).sum()),
        },
        "deterministic_date_guard_credits": 89,
        "judge_repeat_noise": {
            "identical_answer_groups_across_6_runs": 1696,
            "groups_with_verdict_flip": 4,
        },
    }
    # category-level adjusted accuracy
    cat = {}
    for c, g in d.groupby("cat"):
        fp = (
            (g.hand_fp == "clear").sum()
            + 0.7 * (g.hand_fp == "likely").sum()
            + 0.3 * (g.hand_fp == "borderline").sum()
        )
        fn = (
            (g.hand_fn == "clear").sum()
            + 0.75 * (g.hand_fn == "likely").sum()
            + 0.4 * (g.hand_fn == "borderline").sum()
        )
        cat[c] = {
            "n": len(g),
            "raw": round(float(g.correct.mean()), 3),
            "adjusted_central": round(float((g.correct.sum() - fp + fn) / len(g)), 3),
            "fp_weighted": round(float(fp), 1),
            "fn_weighted": round(float(fn), 1),
        }
    T["judge"]["by_category_adjusted"] = cat
    # speaker / gold
    sp = d[d.speaker_mismatch_gold == 1]
    T["gold"] = {
        "errata_entries_by_tag": json.loads(
            pd.Series([t for x in d.errata_tags for t in x.split(";") if t])
            .value_counts()
            .to_json()
        ),
        "speaker_mismatch_rows": len(sp),
        "speaker_mismatch_correct": int(sp.correct.sum()),
        "speaker_mismatch_wrong_not_in_errata": ids(sp[(sp.correct == 0) & (sp.errata_tags == "")]),
        "speaker_mismatch_correct_not_in_errata": int(
            ((sp.correct == 1) & (sp.errata_tags == "")).sum()
        ),
        "bad_or_missing_evidence_rows": int(((d.bad_ev_ids > 0) | (d.n_ev_valid == 0)).sum()),
        "new_candidates_from_hand_read": ids(d[d.hand_gold_candidate == 1]),
    }
    # ------------------------------------------------------------------ items
    R = d.rclass
    items = ITEMS(d, W, A, wrong, T, ids)
    out = {
        "schema": "r14_reader_judge/v1",
        "package": "DEEP-READ",
        "run": "clean-jina-full",
        "tag": "R14",
        "licence_note": "ids, counts and paraphrases only; no dataset text",
        "tables": T,
        "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            out,
            indent=1,
            ensure_ascii=False,
            default=lambda o: o.item() if hasattr(o, "item") else str(o),
        ),
        encoding="utf8",
    )
    print("wrote", OUT, len(items), "items")


def sol(name, mech, source, cost, risk):
    return {"name": name, "generic_mechanism": mech, "source": source, "cost": cost, "risk": risk}


def ITEMS(d, W, A, wrong, T, ids):
    R = d.rclass
    it = []

    def add(**k):
        k["tag"] = "R14"
        it.append(k)

    # ---------------------------------------------------------------- reader
    ref = d[R.isin(["refusal_premise_denial", "inference_open_domain"])]
    add(
        id="R14-READ-1",
        layer="reader",
        problem="Over-refusal and premise denial: the reader answers 'not mentioned' or denies the premise when the evidence supports a committed inference (hypothetical, 'likely', world-knowledge hop).",
        evidence={
            "count": len(ref),
            "question_ids": ids(ref),
            "method": "measured: label classes refusal_premise_denial (14) + inference_open_domain (26, all cat3); refusal-like answers (memspine_evals.refusal.is_refusal) 25 in total, 0 credited, 11 with all gold in context; the retry fired on 25 rows and rescued 0",
            "extra": {"cat3_acc": 0.427, "cat3_acc_gold_in_ctx": 0.469},
        },
        existing=[
            "GAP_REGISTER C3 (yes/no rule rejected), C4 (grounded_generic_infer / routed_generic, screen pending), C10, I1, I61",
            "SOL-READERPROMPT",
            "SOL-DEEPREAD / read.deep (S55 queued)",
            "read.query_contract + read.contract_rules (S06 +1/-0, S11 0)",
            "neutral refusal retry",
        ],
        gap="No answer-type contract says 'this question asks for a judgement, commit to yes/no/likely and cite the supporting line'. Prompt-only yes/no rules misfired on non-yes/no questions (C3: +5/-17), and the retry does not rescue (0 of 25 here).",
        hypothesis="Gating the commit route on the typed contract (shape yes/no or would/likely/might, decided by code) rather than by prompt wording keeps the C3 regression away and converts about one third of the 26 cat3 failures (+6 to +10 q, +0.4 to +0.6 points) with no change on other categories.",
        solutions=[
            sol(
                "Typed commit route",
                "query_contract marks the question 'judgement'; reader prompt asks for 'Yes/No/Likely + one supporting line' only for that type; code checks the polarity token",
                "ours: read.query_contract + contract_rules; CoT-style note-then-answer (Yu et al. 2023, Chain-of-Note)",
                "0 extra calls",
                "mis-typing a factual question as judgement; guard with the cat5 and OP-Bench slices",
            ),
            sol(
                "Self-consistency vote on judgement questions only",
                "3-5 samples at temperature>0, majority polarity",
                "Wang et al. 2022 (self-consistency)",
                "+2 to +4 calls on ~6% of questions",
                "votes of a 9B reader may agree on the same wrong prior",
            ),
            sol(
                "Evidence obligations for 'latest/join' questions before abstaining",
                "refuse only when obligations report unmet; otherwise answer with caveat",
                "ours: read.obligations, deep",
                "0-2 calls",
                "obligation derivation is regex-based",
            ),
        ],
        test="Slice: the 40 ids plus the 25 refusal rows; guard slices cat5 (answerable/abstention split) and OP-Bench irrelevance; paired screen on dev conversations, adoption rule average-led.",
        priority="H",
    )
    det = d[R == "wrong_detail"]
    add(
        id="R14-READ-2",
        layer="reader",
        problem="Wrong or vague detail from the right region of the context: the answer is topical, near the gold line, but states a different or generic detail (median gold-token recall 0.0).",
        evidence={
            "count": len(det),
            "question_ids": ids(det),
            "method": "measured: label class wrong_detail (46 of the 140 stage-d rows). Heuristic: 28 of 46 are classed 'distractor' source (answer tokens come from a non-gold line), 9 'from_gold_turn', 7 mixed, 2 ungrounded (rule in R14-DIST-1)",
        },
        existing=[
            "GAP_REGISTER C2 (answer-style clause, dev neutral), C9",
            "SOL-READERPROMPT grounded_det",
            "SOL-PRESENT (hits-first rejected, R2-4)",
            "SOL-BUNDLES S19/S52-S54 queued",
            "SOL-VERIFY / --verify-answer",
        ],
        gap="The reader sees ~53 lines with several about the same topic and nothing tells it which line the question's specific noun phrase binds to. Presentation tricks (hits-first, markers) were neutral or negative; verification was not screened.",
        hypothesis="A quote-then-answer step restricted to 'what kind / which / what did X say' shapes, where the quoted line must contain the question's head noun phrase, removes a third of the cases whose answer is a plausible sibling line (+5 to +10 q).",
        solutions=[
            sol(
                "Quote-then-answer",
                "reader first copies the one supporting line, code checks the line is in context and shares the question's rarest content word, then answers from that line",
                "Chain-of-Verification (Dhuliawala et al. 2023); ours: slot_verify pattern",
                "+1 call or longer completion",
                "terse outputs lost 3.6 points in dated3; screen per shape",
            ),
            sol(
                "Typed slot check",
                "slot_verify: the answer must contain an entity of the question's expected type and a token of the cited line",
                "ours: slot_verify.py (E06), count_verify.py",
                "0-1 call on defects",
                "false defects trigger a repair that drops a right answer",
            ),
            sol(
                "Bundle composition",
                "evidence bundles: claim + speaker + time + antecedent kept together",
                "ours: read.evidence_bundles, neighbours, exact_budget",
                "0",
                "S54 not run yet",
            ),
        ],
        test="Slice: the 46 ids + the 59 distractor-source ids; screen on the 304-q dev slice, paired; report per-shape deltas.",
        priority="H",
    )
    lst = d[R.isin(["list_incomplete", "count_wrong"])]
    add(
        id="R14-READ-3",
        layer="reader",
        problem="Incomplete lists and wrong counts: the reader returns one or two of several items, over-counts one event mentioned on two dates, or gives 'at least N'.",
        evidence={
            "count": len(lst),
            "question_ids": ids(lst),
            "method": "measured: label classes list_incomplete (16) + count_wrong (9) at stage d. Lists dominate in-ctx wrong rows by gold type: 47 of 169 wrong in-ctx rows have a list-type gold (gold-type rule: >=2 items split on commas/and). how_many shape: 26 in-ctx questions at 65.4% with 9 wrong; all 38 how_many questions 55.3%",
        },
        existing=[
            "GAP_REGISTER C6, I56 (count-verify single REJECTED +1/-16; two_call and evidence_table unscreened)",
            "SOL-LISTMODE (in reference)",
            "SOL-CONTRACT S06/S11",
            "SOL-OBLIG S56 queued",
            "SOL-COUNT (no screen)",
            "SOL-STANDING",
        ],
        gap="Retrieval of list members is addressed (list_mode) but the reader neither enumerates nor dedupes events; the only screened count wrapper collapsed sets to one item. The two_call and evidence-table variants have never been screened.",
        hypothesis="An evidence table (one row per distinct event: actor, object, date) built by code from the context lines, followed by a count/list over the rows, fixes the over-count class (7 of 9 counts) without the collapse failure of single-call count-verify (+4 to +8 q).",
        solutions=[
            sol(
                "Evidence table + enumerate-then-count",
                "extract rows with a JSON reader call, merge mentions by actor+object+date window, answer from the merged rows",
                "ours: count_verify two_call, evidence_table.py (A04); Program-aided reasoning (Gao et al. 2022)",
                "+1 call on count/list shapes (~6%)",
                "extraction misses a row; collapse failure seen in single mode",
            ),
            sol(
                "Obligations: list/count coverage",
                "answer_with_caveat unless coverage is complete; follow-up retrieval for missing members",
                "ours: read.obligations + deep",
                "0-2 calls",
                "regex-derived obligations",
            ),
            sol(
                "Contract-shaped output",
                "'list every item, one per line' only for list contract; 'one number' for count",
                "ours: contract_rules",
                "0",
                "S11 is neutral so far",
            ),
        ],
        test="Slice: the 25 ids + list/count shaped questions (shape rule how_many / list); screens S06, S11, S56 and an unqueued two_call/evidence_table arm.",
        priority="H",
    )
    dar = d[R.isin(["date_duration_arithmetic", "date_wrong_event", "date_offset_small"])]
    add(
        id="R14-READ-4",
        layer="reader",
        problem="Date and duration arithmetic: spans between two events, 'for N years' start dates and 'N months now' are answered by the 9B reader's own subtraction; wrong event line picked for a 'when' question.",
        evidence={
            "count": len(dar),
            "question_ids": ids(dar),
            "method": "measured: label classes date_duration_arithmetic 10, date_wrong_event 8, date_offset_small 1. how_long shape: 34 in-ctx questions at 58.8% with 15 wrong (all 38: 57.9%); gold_type number 50.0% (n=36), relative-phrase gold 50.0% (n=40); abs-day gold 89.0% (n=82)",
        },
        existing=[
            "GAP_REGISTER C5, I57 (date_repair +1/0, 7 fired), I76 (duration_solve built, no screen), I79 (annotator), E05 milestones",
            "SOL-DURATION, SOL-RELANCHOR (S62 queued), SOL-TEMPLEG",
        ],
        gap="The date path is the best-covered class on paper but duration_solve and milestones have no screen; 15 how_long failures remain although every endpoint is annotated.",
        hypothesis="duration_solve + milestones on the 38 how_long/how_many-unit questions converts at least a third of the 15 in-ctx failures (+5 q, +0.3 points) with zero model calls beyond the endpoint extraction.",
        solutions=[
            sol(
                "Calculator for dates",
                "reader extracts the two endpoint events as JSON, code subtracts in the requested unit and rounds",
                "ours: duration_solve.py (I76), milestones.py (E05); PAL (Gao et al. 2022); date-reasoning benchmarks show LLM span arithmetic is unreliable (Fatemi et al. 2024, Test of Time)",
                "+1 call on ~5% of questions",
                "wrong endpoint pairing gives a confident wrong number (abstain rules exist)",
            ),
            sol(
                "Start-date annotation",
                "'for N years' / 'N months now' -> [= since ~date] in the context line",
                "ours: I79 read.relative_dates_durations",
                "0",
                "annotation of retrospective statements",
            ),
            sol(
                "Date repair post-step",
                "weekday/date consistency and relative-phrase rewrite",
                "ours: date_repair.py (I57)",
                "0",
                "fail-closed; small effect",
            ),
        ],
        test="Slice: the 19 ids + the 38 how_long/how_many-unit questions; run duration_solve and milestones on the dev slice, paired; check abstain rate.",
        priority="H",
    )
    img = d[R == "image_caption_misuse"]
    add(
        id="R14-READ-5",
        layer="reader",
        problem="Image-caption misuse: the answer is in an [image: ...] caption but the reader uses a caption from another turn, or the gold fact is only in the photo.",
        evidence={
            "count": len(img),
            "question_ids": ids(img),
            "method": "measured: label class 6 rows at stage d. caption-only gold (rule: a gold content token occurs in the gold turn's caption and in no spoken text): 28 in-ctx questions at 67.9% vs 87.6% for others",
        },
        existing=[
            "GAP_REGISTER C7 (caption rule screened 85.8 vs 86.7, +3/-5, not adopted)",
            "SOL-ASSET (ingest.assets, read.asset_evidence)",
            "SOL-READERPROMPT",
        ],
        gap="Captions are rendered inline in the same sentence as speech, so the 9B reader cannot tell whose caption belongs to which turn; photo-only answers need the asset.",
        hypothesis="Rendering each caption on its own 'Photo (turn id, speaker, date): ...' line next to its turn lowers cross-turn caption mixing (3 of the 6) but cannot fix photo-only golds (+2 to +3 q).",
        solutions=[
            sol(
                "Caption as its own line with turn metadata",
                "separate sentence + speaker + date",
                "ours: renderer change",
                "0",
                "prompt-only caption rule was already neutral/negative (C7/C8)",
            ),
            sol(
                "Asset evidence",
                "describe the original image once at ingest",
                "ours: ingest.assets / read.asset_evidence",
                "1 vision call per image at ingest",
                "needs image access and licence check",
            ),
        ],
        test="Slice: the 6 ids + 28 caption-only questions.",
        priority="L",
    )
    spk = d[(R == "speaker_confusion") | (d.wrong_speaker_answer == 1)]
    add(
        id="R14-READ-6",
        layer="reader",
        problem="Answer about the wrong speaker: the fact exists in context but is attributed to the person the question does not name.",
        evidence={
            "count": len(spk),
            "question_ids": ids(spk),
            "method": "measured/heuristic: label speaker_confusion 1; heuristic wrong_speaker_answer (question names exactly one of the two speakers, wrong answer names only the other) 1. The visible effect is mostly on the data side (R14-GOLD-2)",
        },
        existing=[
            "SOL-PERSPECTIVE (in reference)",
            "I59 owner_check (built, no screen)",
            "GAP_REGISTER I39/I47/I54",
        ],
        gap="Rare as a reader failure here (1 row) but the gold is itself speaker-swapped in 39 rows; any attribution enforcement is penalised there.",
        hypothesis="Owner tags (I59) stay a tag, not a filter; expect <=1 q gain, risk of losing credited swapped-speaker rows.",
        solutions=[
            sol(
                "Owner/subject tag",
                "'stated by X' tag on hits where the speaker is not the named person",
                "ours: core/owner_check.py",
                "0",
                "see R14-GOLD-2",
            )
        ],
        test="Screen only with R14-GOLD-2's 23 credited swapped-speaker rows reported separately.",
        priority="L",
    )
    ung = W[W.answer_source == "ungrounded"]
    add(
        id="R14-READ-7",
        layer="reader",
        problem="Ungrounded answers: the answer's new content words appear nowhere in the context (inference, arithmetic output, world knowledge or fabrication).",
        evidence={
            "count": len(ung),
            "question_ids": ids(ung),
            "method": "heuristic: wrong, all gold in ctx, >=50% of the answer's content stems that are not in the question nor the gold answer occur in no context line. Rate: 37 wrong of 225 answers of this source (16%) vs 10 wrong of 447 'no_novel' answers (2%)",
        },
        existing=[
            "SOL-VERIFY (--verify-answer, --verify-slots diagnosed: unsupported-claim defect)",
            "defect_diagnosis.py",
            "SOL-GAP-BIGREADER",
        ],
        gap="diagnosed mode already flags 'a name no memory line contains' but is unscreened; ungrounded wrong answers are 22% of in-ctx wrong rows.",
        hypothesis="slot_verify diagnosed on ungrounded answers repairs 1 in 4 (+8 q) at ~1.8% extra calls; without a gold, abstaining is the safe alternative on cat1-4.",
        solutions=[
            sol(
                "Diagnosed repair",
                "code detects unsupported tokens and asks once with the defect named",
                "ours: slot_verify diagnosed (G01)",
                "+1 call on ~10% of answers",
                "numbers and derived words look unsupported by construction",
            ),
            sol(
                "Larger reader or thinking mode",
                "reader size",
                "ours: SOL-GAP-BIGREADER; A9 thinking mode not adopted",
                "high",
                "GPU memory",
            ),
        ],
        test="Slice: the 37 ids; false-defect rate on the 188 correct 'ungrounded' answers.",
        priority="M",
    )
    ver = W[W.len_ratio > 8]
    add(
        id="R14-READ-8",
        layer="reader",
        problem="Verbosity versus a short gold: wrong answers are enumerations or explanations many times the gold length; long answers carry both hedges and the judge's partial-credit habits.",
        evidence={
            "count": len(ver),
            "question_ids": ids(ver),
            "method": "measured: answer words / gold words > 8. 49 of 169 wrong in-ctx rows (29%) vs 124 of 1154 correct in-ctx (10.7%); accuracy by ratio bucket 0-1: 0.890, 1-2: 0.885, 2-4: 0.923, 4-8: 0.871, 8-16: 0.789, >16: 0.576",
        },
        existing=[
            "READER_GAPS_FORENSIC section 0.4 (answers got 44% shorter)",
            "SOL-CONTRACT answer contracts",
            "GROUNDED prompts in readers.py (short, direct answer)",
            "judge_conventions list_superset",
        ],
        gap="The signal is a difficulty correlate (long answers follow refusals, lists and uncertain counts), so length-capping is not a fix; the lever is an answer-type contract that fixes the output shape per question type.",
        hypothesis="Contract-fixed output shape (single fact: one phrase; list: bullet per item; count: a number plus the list) lowers the >8-ratio rate without lowering list recall.",
        solutions=[
            sol(
                "Answer-type contract",
                "typed output template chosen by code",
                "ours: read.query_contract + contract_rules; slot_verify strict",
                "0",
                "S11 neutral so far",
            )
        ],
        test="Slice: ratio>8 ids; compare list recall and judge verdicts.",
        priority="M",
    )
    # ---------------------------------------------------------------- distractors
    dist = W[W.answer_source == "distractor"]
    add(
        id="R14-DIST-1",
        layer="reader",
        problem="Answering from a distractor turn: the context holds a non-gold line with answer-like content (same topic, another event, another speaker's similar remark) and the reader uses it.",
        evidence={
            "count": len(dist),
            "question_ids": ids(dist),
            "method": "heuristic: wrong, all gold in ctx; novel stems = stems of the answer not in the question and not in the gold answer; source 'distractor' when >=2 and >=40% of novel stems occur only in non-gold context lines (not in any gold turn) and exceed the novel stems found in gold turns. Hand check of 18: 13 true (72%), 5 other (count, premise, refusal, inference, vague detail from the right turn). Distractor rate by source over all 1323 in-ctx rows: 59 wrong of 207",
            "extra": {
                "same_session_as_gold": 24,
                "within_4_turns_of_gold": 19,
                "distractor_turn_in_final_top10": 32,
                "distractor_outranks_gold": 11,
                "estimated_true": "about 43 (29-52)",
            },
        },
        existing=[
            "GAP_REGISTER C1, C9, R2-4 (hits-first rejected: 86.7 vs 88.4)",
            "SOL-PRESENT",
            "SOL-VERIFY",
            "SOL-GAP-DISTRACTOR (not built)",
            "SOL-BUNDLES",
            "I31 dedupe",
        ],
        gap="The distractor is a window neighbour of the gold in 19 of 59 cases and a retrieval hit in 32; reordering was rejected, verification not screened, owner/event check not built.",
        hypothesis="A binding check that the cited line shares the question's event noun phrase and date (code) plus one repair call removes about a third of the true cases (+10 to +14 q); a per-shape window (1/1 for non-temporal single-hop) removes the 19 window-neighbour cases at the cost of the +19 net gains window 2/4 gave.",
        solutions=[
            sol(
                "Provenance check (owner/event/date binding)",
                "answer must cite a turn id; code checks the turn id's speaker, date and head noun phrase against the question",
                "ours: SOL-GAP-DISTRACTOR; irrelevant-context robustness (Shi et al. 2023, 'LLMs can be easily distracted by irrelevant context'; Yoran et al. 2023)",
                "+1 call on mismatches only",
                "citation errors by a 9B reader",
            ),
            sol(
                "Per-shape window",
                "wide window for temporal/multi-turn shapes, narrow elsewhere",
                "ours: replay_window_before/after per shape (SOL-PRESENT)",
                "0",
                "window 2/4 produced 49 of the 154 gains in the forensic run",
            ),
            sol(
                "Contrastive selection",
                "reader picks among the top-3 candidate lines before answering",
                "Self-Ask style decomposition (Press et al. 2022); RECOMP-style extractive compression (Xu et al. 2023)",
                "+1 call",
                "adds latency",
            ),
        ],
        test="Slice: 59 ids + 13 hand-confirmed; paired dev screen with per-shape report; count window-neighbour vs hit cases separately.",
        priority="H",
    )
    add(
        id="R14-DIST-2",
        layer="data",
        problem="Sibling evidence: for a single-answer question, other turns in the same conversation also answer it correctly; the gold names only one, so a right answer from the sibling turn is scored wrong.",
        evidence={
            "count": 3,
            "question_ids": sorted(d[d.hand_sibling_evidence == 1].pipe(ids)),
            "method": "hand: 3 of the 18 hand-checked distractor rows (17%); extrapolated to about 7-10 of the 59 heuristic rows (low confidence). The reader's answer restates a true fact from another turn about the same subject",
        },
        existing=[
            "R2-5 evidence_label_error entries",
            "judge_conventions superset",
            "none for multi-gold",
        ],
        gap="No multi-gold or accept-set annotation exists; errata tags evidence labels but not 'alternative valid answers'.",
        hypothesis="Re-judging the hand-flagged rows with a gold set extended by the sibling answer lifts 3 of 18 sampled rows; if it holds at 7-10 of 59, the true reader error rate is lower by ~0.5 points.",
        solutions=[
            sol(
                "Accept-set errata",
                "errata tag sibling_valid with the alternative answer paraphrased and turn ids",
                "ours: errata.py new tag",
                "0",
                "adjudication cost",
            ),
            sol(
                "Second judge with evidence access",
                "judge sees the cited evidence lines, not only the gold",
                "LLM-as-judge with references (Zheng et al. 2023)",
                "1 call per disputed row",
                "same-model blind spots (A1)",
            ),
        ],
        test="Hand-adjudicate the 59 ids once (about 1 hour), freeze labels.",
        priority="M",
    )
    # ---------------------------------------------------------------- position / length / multi-hop
    add(
        id="R14-POS-1",
        layer="compose",
        problem="Reader position effects (lost-in-the-middle): accuracy depends on where the gold turn sits in the context.",
        evidence={
            "count": 0,
            "question_ids": [],
            "method": "measured: single-evidence questions with the gold in context (n=991 after dropping rows without ranks). Early third 89.7% vs late third 90.2% (Fisher p=0.90); quintile accuracy 0.89-0.92 flat; logistic coefficient of gold position z=-0.4 controlling category, context size and gold rank. Context order is chronological in 1540/1540, so position is position in time. No dependence seen",
            "extra": T["position_single_evidence_in_ctx"]["early_third_vs_late_third"],
        },
        existing=[
            "SOL-PRESENT / read.present_order (hits-first, hit_blocks rejected)",
            "R2-4",
            "Lost in the Middle (Liu et al. 2023) as the prior",
        ],
        gap="A negative result: nothing to fix for a 2k-token context with a 9B reader; the U-shape does not reproduce at this length.",
        hypothesis="Reordering by position will not move accuracy (consistent with R2-4); the effect may appear at BEAM-scale contexts (I20), so re-measure there, not here.",
        solutions=[
            sol(
                "None needed here; re-measure on long-context benchmarks",
                "same regression on BEAM / long-history runs",
                "Liu et al. 2023; Peysakhovich and Lerer 2023 (attention sorting)",
                "0",
                "n/a",
            )
        ],
        test="Repeat r14_build_report on any run with >=8k-token contexts.",
        priority="L",
    )
    add(
        id="R14-POS-2",
        layer="retrieve",
        problem="Gold retrieved but ranked low in the final candidate list: accuracy given gold in context falls when the best gold turn is not in the top 3.",
        evidence={
            "count": int(T["retrieval_rank_of_gold"]["wrong_in_ctx_best_rank_gt3"]),
            "question_ids": ids(W[W.gold_best_final_rank > 3]),
            "method": "measured: clean in-ctx rows (gold errors and judge labels dropped) 90.8% (n=1101) when best gold rank <=3 vs 74.8% (n=107) when >3; logistic z=-2.6 to -3.5 for the rank covariate. 29 of 169 wrong in-ctx rows have best rank >3 and 17 more have no final rank (in context through the window only)",
        },
        existing=[
            "R2-4 hits_first and hit_blocks rejected",
            "SOL-RRPOOL / SOL-POOLPROT / SOL-FAMFUS",
            "GAP_REGISTER B9, B10",
        ],
        gap="The gap is large but presentation reordering was rejected; it is probably a difficulty correlate and a signal that rank >3 hits are weakly related to the question.",
        hypothesis="Improving the rank of the gold (reranker v2 over a wider pool) helps more than reordering the same context; expect up to +5 q if half of the 29 rank>3 failures move into the top 3.",
        solutions=[
            sol(
                "Reranker v2 over a wider pool",
                "rerank 30 candidates, keep 10",
                "ours: read.rerank (S16 iterate 0/0)",
                "GPU rerank time",
                "S16 neutral so far",
            )
        ],
        test="Track the rank>3 share per screen; report accuracy by rank bucket for every screen.",
        priority="M",
    )
    add(
        id="R14-LEN-1",
        layer="compose",
        problem="Context length effect: accuracy given all gold in context falls as the number of context turns and tokens grows.",
        evidence={
            "count": 0,
            "question_ids": [],
            "method": "measured: all in-ctx rows: n_ctx <=40: ~91%, 41-50: 87.6%, 51-60: 87.7%, >60: 83.6%. Single-evidence tertiles of n_ctx: 92.9 / 89.4 / 89.0%; of tokens 92.7 / 90.0 / 89.1%. Controlled logistic coefficients per sd: n_ctx -0.12 to -0.19 (z -1.3 to -1.6), ctx_tokens -0.25 (z -2.2); weak but consistent in sign",
            "extra": T["context_length"]["by_n_context_turns"],
        },
        existing=[
            "GAP_REGISTER I6 (window counted in turns, 4,096 budget)",
            "SOL-PRESENT per-shape window",
            "SOL-BUNDLES S52/S53 exact budget (queued)",
            "READER_GAPS_FORENSIC 2.3: 9 DIST regressions with 53 vs 39 lines",
        ],
        gap="No per-shape context size; the window that helps temporal questions (+19 net) is paid for by single-hop distractors.",
        hypothesis="A per-shape budget (narrow for single-fact, wide for temporal/multi-turn) recovers about 1 point on single-hop at no loss on temporal; effect size about 0.2 sd per extra 10 turns.",
        solutions=[
            sol(
                "Per-shape window / budget",
                "context size chosen by the contract shape",
                "ours: replay_window_* per shape; exact_budget",
                "0",
                "interacts with R14-DIST-1",
            ),
            sol(
                "Evidence compression",
                "extract the supporting sentences before the reader",
                "RECOMP (Xu et al. 2023); Chain-of-Note (Yu et al. 2023)",
                "+1 call",
                "extraction drops needed context",
            ),
            sol(
                "Check at length",
                "context-length-alone degradation even with perfect retrieval",
                "Levy et al. 2024 (Same Task, More Tokens); Du et al. 2025 (from memory, not re-fetched)",
                "0",
                "n/a",
            ),
        ],
        test="2x2 narrow/wide window by shape on the dev slice; paired.",
        priority="M",
    )
    mh = A[(A.n_ev_sessions >= 2) & (A.correct == 0)]
    add(
        id="R14-MH-1",
        layer="reader",
        problem="Questions that need two or more turns, especially across sessions: the reader does not join them (or misses one of them) even when all are in context.",
        evidence={
            "count": len(mh),
            "question_ids": ids(mh),
            "method": "measured: in-ctx accuracy by number of evidence sessions 1: 89.9% (n=1146), 2: 72.9% (144), 3: 62.5% (24), 4+: 3 of 9; by evidence turns 1: 90.0%, 2: 78.5%, 3: 68.2%, 4: 64.7%. Logistic: sessions z=-4.4 controlling category and rank. 52 of the 140 stage-d rows are multi-session",
        },
        existing=[
            "SOL-AGENTIC / second_round (not screened)",
            "SOL-OBLIG join obligations (S56 queued)",
            "SOL-GRAPH",
            "SOL-DEEPREAD S55",
            "R2-1 bridge hop (gated)",
        ],
        gap="Retrieval-side hop work is plentiful; reader-side decomposition for cases where all evidence is already in context is not covered. Multi-session ones are 52 of 140.",
        hypothesis="Decomposition into sub-questions answered per turn and joined by code lifts multi-session in-ctx accuracy from 72.9% by +5 points (about +7 q).",
        solutions=[
            sol(
                "Decompose then join",
                "reader emits 2 sub-questions; each answered over the cited lines; code or one call joins",
                "Self-Ask (Press et al. 2022); Least-to-Most (Zhou et al. 2022)",
                "+2 to +3 calls on ~10% of questions",
                "wrong decomposition; screen on cat1 only",
            ),
            sol(
                "Evidence obligations join",
                "link fact + attribute fact for each linked entity",
                "ours: read.obligations join kind",
                "0-2",
                "regex derivation",
            ),
            sol(
                "Evidence table across sessions",
                "rows keyed by actor+object+date",
                "ours: evidence_table.py",
                "+1 call",
                "extraction cost",
            ),
        ],
        test="Slice: 52 multi-session stage-d ids + 144 two-session in-ctx questions.",
        priority="M",
    )
    # ---------------------------------------------------------------- judge
    fnall = d[d.hand_fn != ""]
    add(
        id="R14-JUDGE-1",
        layer="judge",
        problem="Judge false negatives after the date guard: WRONG verdicts whose answer states the gold fact (superset or concluding list, equivalent annotation, typo, containment plus extra detail, country/city or paraphrase).",
        evidence={
            "count": len(fnall),
            "question_ids": ids(fnall),
            "method": "hand: all 287 WRONG verdicts read, one annotator. clear 10, likely 4, borderline 9. Deterministic rules (date equivalence, gold containment, token recall >=0.8, conventions v1/v2, number-in-answer) flag 9 wrong rows and 6 of the 14 clear/likely ones, so their recall is 43%. Mechanisms seen: a concluding list that holds all gold items after a hedged longer list, list supersets, an annotation-equivalent year, a typo copied from the transcript, the gold fact plus extra date detail, city/country or county/state pairs, paraphrase",
        },
        existing=[
            "GAP_REGISTER A2 (date guard in reference), I58 (conventions v1), I77 (conventions v2 built; v3 not built)",
            "SOL-DATEGUARD",
            "SOL-JUDGE-I77 (not_built)",
            "judge_conventions.py check_conventions_v2",
            "second_view.py (Mem0 official judge, batch prepared, no model run)",
        ],
        gap="The relative-date class that dominated earlier runs (7 of 24) is closed by the date guard (89 deterministic credits). What remains is list/containment/annotation/geography; the existing v1/v2 conventions reach only 6 of the 14.",
        hypothesis="Conventions v3 (contains_all_gold_items_anywhere_in_answer incl. a concluding list; annotation-equivalent year; containment with extra detail when no contradiction; edit-distance names) credits 7 to 10 of the 14 clear/likely rows with no new false credit on the 17+12 hand-FP rows.",
        solutions=[
            sol(
                "Conventions v3",
                "item-level containment over the whole answer, concluding-list extraction ('Therefore, ...'), annotation equivalence ('N years ago [= ~YYYY]'), fuzzy proper nouns",
                "ours: judge_conventions.py + I77",
                "0",
                "can credit shotgun lists; keep as a second column",
            ),
            sol(
                "Second judge on disagreement",
                "independent-family or larger judge on rows where rules and the 9B disagree, plus all list golds with >=3 items",
                "ours: A1 deferred; PoLL panel (Verga et al. 2024); Zheng et al. 2023",
                "~230 calls per run",
                "judge drift; pin versions",
            ),
            sol(
                "Judge sees evidence",
                "give the judge the cited lines to separate gold errors from reader errors",
                "Zheng et al. 2023 (reference-guided)",
                "longer prompts",
                "n/a",
            ),
        ],
        test="Score every convention change on judge_calibration_dev.jsonl (43 hand labels) and on these 23 + 34 hand rows; require 0 new FP.",
        priority="H",
    )
    fpall = d[d.hand_fp != ""]
    add(
        id="R14-JUDGE-2",
        layer="judge",
        problem="Judge false positives: CORRECT verdicts for answers that omit one or more required gold items, state a vague description instead of the named item, or give a coarse stand-in.",
        evidence={
            "count": len(fpall),
            "question_ids": ids(fpall),
            "method": "hand: 193 rule-flagged CORRECT rows read (list_item_missing 136, low_gold_recall 33, hedged_or 31, long_answer 26, month/year/date mismatch 10, overlapping) plus 70 random unflagged. clear 17, likely 12, borderline 5; 0 of 70 unflagged. Flag rule list_item_missing: gold has >=2 items (comma/and split) and fewer than all have >=60% of their content stems in the answer: 136 flagged, 27 of them clear/likely (20%). By category: multi-hop (cat1) 25 of the 29 clear/likely",
            "extra": {"cat1_correct": 188, "cat1_fp_weighted": 22.9},
        },
        existing=[
            "GAP_REGISTER A3 (list_recall column), I58",
            "judge_calibration_dev.jsonl complete_list convention (CORRECT with extra true items; the reverse - missing items - is not calibrated)",
            "list_recall in reports",
        ],
        gap="Missing-item credit is a convention (LoCoMo graders usually accept it), but it is applied unevenly: the same run rejects other partial lists (stage d:lst has 16 such rows).",
        hypothesis="Reporting item recall as a second column and a strict-list headline shows the multi-hop figure is overstated by about 5 points (66.7% to 61.1% central); a strict all-items rule would drop a further 12-17 q.",
        solutions=[
            sol(
                "Item-recall column + strict list headline",
                "per-item coverage using the same containment rule as conventions",
                "ours: list_recall (A3) extended to strict",
                "0",
                "choice of convention; publish both",
            ),
            sol(
                "Two-judge adjudication",
                "second judge only on list golds with >=2 items",
                "Verga et al. 2024 (PoLL)",
                "~350 calls",
                "cost",
            ),
            sol(
                "Calibrate the 'missing item' side",
                "add 15 hand-labelled partial-list cases to judge_calibration_dev.jsonl",
                "ours",
                "0",
                "label noise",
            ),
        ],
        test="Freeze the 34 FP ids + 23 FN ids as a 57-row calibration extension; measure every judge change on it.",
        priority="H",
    )
    add(
        id="R14-JUDGE-3",
        layer="judge",
        problem="How far off is the 81.4% headline: net effect of judge errors in both directions and of gold errors.",
        evidence={
            "count": 0,
            "question_ids": [],
            "method": "hand estimate with explicit weights (see tables.judge.central_estimate): FN central 16.6 (range 10-23), FP central about 30 (range 17-45 incl. 3 unseen); headline 1253/1540 = 81.4% -> central about 80.5%, range 79.1 to 81.7; errata-excluded raw 82.6%; category: cat1 66.7 -> 61.1, cat3 42.7 -> 43.5, cat4 90.0 -> 90.7, cat2 83.2 -> 82.9",
            "extra": T["judge"]["by_category_adjusted"],
        },
        existing=[
            "GAP_REGISTER A1 (second judge deferred), A5, A10 (bootstrap CI +-2.2)",
            "locomo_errata.json (52 entries)",
        ],
        gap="The judge-adjusted shift (-0.9 central) is inside the bootstrap CI (+-2.2) but is concentrated in multi-hop, which carries most of the claimed improvements.",
        hypothesis="Reporting raw, convention-adjusted and strict-list numbers per category with CIs makes multi-hop deltas under 5 points uninterpretable; per-category claims should use the strict column.",
        solutions=[
            sol(
                "Triple reporting",
                "raw / conventions v3 / strict list, all with cluster-bootstrap CI",
                "ours: forensics_report two-column layout",
                "0",
                "none",
            )
        ],
        test="Add the adjusted columns to the screen report.",
        priority="H",
    )
    add(
        id="R14-JUDGE-4",
        layer="judge",
        problem="Is the judge noisy or systematically wrong? Repeat verdicts on identical answers.",
        evidence={
            "count": 4,
            "question_ids": [],
            "method": "measured: 1,696 groups of identical normalised answers judged by the guarded LLM judge across 6 full runs (clean-jina-full, full-persp-loc, r7-multprot/post/protect-full, qa-full-qs-eq06-fix); 4 groups (0.24%) contain both verdicts. 89 more rows are credited by the deterministic date guard without an LLM call",
        },
        existing=[
            "GAP_REGISTER A16 noise floor, READER_GAPS_FORENSIC 4.1 (3 of 256 near-identical flips)"
        ],
        gap="The judge is nearly deterministic at temperature 0; its errors are systematic (conventions), not random, so averaging runs will not remove them.",
        hypothesis="Fixes must be convention changes or a second judge, not repeats.",
        solutions=[sol("Convention changes + second judge", "see R14-JUDGE-1/2", "ours", "-", "-")],
        test="n/a (confirmed).",
        priority="L",
    )
    rel = d[(d.gold_type == "rel_other") & (d.correct == 0)]
    add(
        id="R14-JUDGE-5",
        layer="judge",
        problem="Relative-phrase golds that the date guard does not parse ('a year ago', 'few days before <month>', 'the last week of <month>', 'since <year>'): answered in absolute or annotated form.",
        evidence={
            "count": len(rel),
            "question_ids": ids(rel),
            "method": "heuristic: gold_type rel_other = relative words (ago, last, next, since, week, month, year) and no digit and not parsed as a day/week/weekend/month/year. 40 questions at 50.0% accuracy; 20 wrong; 1 hand-FN (annotation-equivalent year), others real reader failures or gold limits",
        },
        existing=["SOL-DATEGUARD", "I77 relative_year convention (proposed)"],
        gap="Small (one FN found) but unguarded; the relative gold carries no machine-readable anchor.",
        hypothesis="relative_year convention ('N years ago [= ~YYYY]') credits the one case; expected effect <=2 q.",
        solutions=[
            sol(
                "relative_year convention",
                "session year minus N equals the answer year",
                "ours: I77",
                "0",
                "none",
            )
        ],
        test="calibration rows.",
        priority="L",
    )
    # ---------------------------------------------------------------- gold
    add(
        id="R14-GOLD-1",
        layer="data",
        problem="Gold and question defects not yet in the errata: gold not entailed by its evidence turn, question premise that differs from the transcript, and speaker-attribution mismatches.",
        evidence={
            "count": 3 + len(T["gold"]["speaker_mismatch_wrong_not_in_errata"]),
            "question_ids": T["gold"]["new_candidates_from_hand_read"]
            + T["gold"]["speaker_mismatch_wrong_not_in_errata"],
            "method": "hand (3: gold date not entailed; question month differs from the evidence's resolved month; gold year needs an unresolved computation) + heuristic (5: question names exactly one speaker, every evidence turn is spoken by the other, wrong, not in errata; unverified). The errata file has 52 entries; excluding gold_error + needs_image rows gives 82.6% (26 rows, 3 of them credited)",
        },
        existing=[
            "SOL-ERRATA, locomo_errata.json tags gold_error/needs_image/evidence_label_error/premise_error",
            "GAP_REGISTER A5, I62",
        ],
        gap="The estimated 16-24 unread gold errors were not found: reading all 287 wrong rows added only about 3 certain and 5 candidate items; the errata is close to complete for the wrong side.",
        hypothesis="Adding these 8 entries changes the excluded-accuracy by <0.2 points; the larger distortion is on the credited side (R14-GOLD-2).",
        solutions=[
            sol(
                "Errata extension with adjudication",
                "two-person review of the 8",
                "ours: errata.py",
                "0",
                "n/a",
            )
        ],
        test="Verify the 5 speaker candidates against the transcript.",
        priority="L",
    )
    sp = d[(d.speaker_mismatch_gold == 1)]
    add(
        id="R14-GOLD-2",
        layer="data",
        problem="Speaker-swapped questions reward ignoring the person the question names: when the gold turn is spoken by the other person, a system that attributes by speaker loses the point and one that ignores speaker gains it.",
        evidence={
            "count": len(sp),
            "question_ids": ids(sp),
            "method": "heuristic: the question names exactly one of the two conversation speakers and every valid evidence turn is spoken by the other. 39 rows: 24 credited (1 in errata, 23 not), 15 wrong (10 in errata, 5 not)",
        },
        existing=["SOL-PERSPECTIVE (subject_weight), I59 owner_check, I39 perspective layer"],
        gap="Perspective improvements are scored against labels that sometimes contradict them: 23 credited, un-errata'd rows depend on ignoring speaker attribution.",
        hypothesis="Reporting the 23 credited swapped-speaker rows separately explains any perspective-feature loss up to 1.5 points and prevents rejecting a correct attribution feature.",
        solutions=[
            sol(
                "Report speaker-swap rows as their own stratum",
                "tag in errata as speaker_swap, keep in headline, show delta with and without",
                "ours: errata.py",
                "0",
                "none",
            )
        ],
        test="Run any perspective screen with the stratum split.",
        priority="M",
    )
    return it


if __name__ == "__main__":
    main()
