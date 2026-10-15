"""OBS-GAPS: CPU-only measurements behind docs/v2/OBSERVATIONS_GAP_ANALYSIS.md.

Reads the LoCoMo file and the ``clean-jina-full`` problem table. Prints COUNTS only. Anything that
carries dataset text (per-question evidence) goes to ``--detail-dir`` (default
``evals/runs/_analysis/obs_gaps``, gitignored). No model call, no GPU, no network.

Every predicate is a heuristic (a regex or token rule) unless the docstring says "measured":
measured = read straight from the logged ranks / classifier labels of the run.

    python evals/obs_gaps_measure.py --data <locomo10.json> --table <locomo_problem_table.jsonl> \
        --out docs/v2/data/obs_measurements.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

PRON3 = re.compile(r"\b(he|she|they|them|him|his|her|hers|their|theirs|it|its)\b", re.I)
PLURAL = re.compile(r"\b(they|them|their|theirs|those|these)\b", re.I)
WE = re.compile(r"\b(we|us|our|ours)\b", re.I)
DEICTIC = re.compile(
    r"\b(there|then|that (?:time|day|place|one)|those days|back then|over there)\b", re.I
)
REL_TIME = re.compile(
    r"\b(yesterday|tomorrow|tonight|today|last (?:week|weekend|month|year|night|friday|saturday|sunday|monday|"
    r"tuesday|wednesday|thursday|summer|winter|spring|fall|autumn)|next (?:week|weekend|month|year)|"
    r"this (?:week|weekend|morning|month|year|summer)|(?:a|one|two|three|four|five|several|few) "
    r"(?:days?|weeks?|months?|years?) (?:ago|from now)|a while ago|recently|the other day|earlier this)\b",
    re.I,
)
BACKREF = re.compile(
    r"\b(you (?:recommended|mentioned|suggested|told me|said|showed|sent|shared|talked about)|"
    r"(?:we|you and i) (?:talked|discussed|spoke)|last time|as i (?:said|mentioned)|"
    r"the (?:one|book|movie|show|place|song|class|trip) you|that (?:book|movie|show|place|song|"
    r"class|trip|thing) (?:you|we))\b",
    re.I,
)
REACTION = re.compile(
    r"^(wow|oh|ah|yeah|yes|no|that'?s|sounds|cool|great|nice|awesome|thanks|thank you|"
    r"congrats|haha|lol|hmm|really|amazing|wonderful|sorry)\b",
    re.I,
)
WH_FOLLOWUP = re.compile(
    r"^(what|how|why|where|who|when|any|did|do|does|was|were|is|are|can)\b", re.I
)
GREET = re.compile(
    r"(?i:hey|hi|hello|thanks|thank you|wow|congrats|good luck|bye|see you|dear)[, ]+([A-Z][a-z]{1,})"
)
ATTR_Q = re.compile(
    r"\b(job|career|profession|occupation|works? as|work(?:s|ed)? (?:at|for)|studied?|degree|major|field|"
    r"education|pets?|dogs?|cats?|kids?|children|family|married|spouse|partner|boyfriend|girlfriend|"
    r"gender|where (?:does|did|is|was) \w+ (?:live|from|move|born|grow)|hometown|lives?|moved|"
    r"hobbies|hobby|favou?rite|religio\w+|politic\w+|identity|relationship status|single|age|how old|"
    r"allergic|diet|vegetarian|car|owns?)\b",
    re.I,
)
STATE_Q = re.compile(
    r"(?:^|\W)(used to|no longer|still|currently|now|anymore|changed?|switch(?:ed)?|new job|started|stopped|quit|latest|recent|current)(?:\W|$)",
    re.I,
)
INFER_Q = re.compile(
    r"\b(would|could|might|likely|probably|prefer|enjoy|interested in|open to|consider|"
    r"be considered|suitable|guess)\b",
    re.I,
)
SETOP_Q = re.compile(
    r"\b(both|in common|share[sd]?|same|either|neither|together|each other)\b", re.I
)
COMPARE_Q = re.compile(
    r"\b(more than|less than|older|younger|earlier|later|first|longer|compared|most|least)\b", re.I
)
COUNT_Q = re.compile(r"\bhow many\b", re.I)
LIST_Q = re.compile(
    r"\b(what (?:are|were|activities|books|places|hobbies|kinds?|types?)|which .* (?:has|have|did)|list)\b",
    re.I,
)
REFUSAL = re.compile(
    r"(not mentioned|do not know|don't know|no information|not stated|not specified|cannot (?:be )?"
    r"(?:determin|answer)|not (?:clear|available|provided)|unknown|isn't mentioned|is not mentioned)",
    re.I,
)
STOP = set(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "with",
        "by",
        "from",
        "as",
        "about",
        "into",
        "up",
        "out",
        "over",
        "after",
        "before",
        "when",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "how",
        "why",
        "where",
        "i",
        "you",
        "he",
        "she",
        "they",
        "we",
        "me",
        "my",
        "your",
        "his",
        "her",
        "their",
        "our",
        "not",
        "no",
        "yes",
        "can",
        "could",
        "would",
        "should",
        "will",
        "just",
        "so",
        "than",
        "then",
        "there",
        "here",
        "also",
        "very",
        "more",
        "most",
        "some",
        "any",
        "much",
        "many",
    ]
)


def stem(w: str) -> str:
    w = w.lower()
    for suf in ("ing", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def content(text: str, names: set[str] | None = None) -> set[str]:
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", text):
        lw = w.lower()
        if lw in STOP or (names and lw in names):
            continue
        out.add(stem(lw))
    return out


def turn_body(text: str) -> str:
    return text.strip()


def has_named_antecedent(body: str, speakers: set[str]) -> bool:
    """A capitalised word that is not sentence-initial and not a speaker name."""
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", body)
    for i, w in enumerate(toks):
        if i == 0:
            continue
        if (
            w[0].isupper()
            and w.lower() not in {s.lower() for s in speakers}
            and w != "I"
            and not w.startswith("I'")
        ):
            return True
    return False


def flags_for_turn(t: dict, speakers: set[str]) -> dict[str, bool]:
    body = turn_body(t["text"])
    words = body.split()
    named = has_named_antecedent(body, speakers)
    f = {}
    f["pron3_no_antecedent"] = bool(PRON3.search(body)) and not named
    f["deictic"] = bool(DEICTIC.search(body))
    f["very_short"] = len(words) <= 6
    f["reaction_open"] = bool(REACTION.match(body)) and len(words) <= 25
    f["followup_question"] = (
        body.rstrip().endswith("?") and len(words) <= 14 and bool(WH_FOLLOWUP.match(body))
    )
    f["relative_time"] = bool(REL_TIME.search(body))
    f["backref"] = bool(BACKREF.search(body))
    f["plural_pron"] = bool(PLURAL.search(body)) and not named
    f["we_pron"] = bool(WE.search(body))
    f["caption"] = bool(t.get("blip_caption"))
    # decision rule for "rephrase" (observation 3), heuristic: the turn cannot be read alone.
    dep = f["pron3_no_antecedent"] or f["deictic"] or f["backref"]
    f["incomplete"] = (
        (dep and len(words) <= 20)
        or f["followup_question"]
        or f["very_short"]
        or (f["reaction_open"] and len(words) <= 8)
    )
    f["incomplete_strict"] = (dep and len(words) <= 20) or f["followup_question"] or f["very_short"]
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--table", required=True)
    ap.add_argument("--forensics", default=None)
    ap.add_argument("--out", default="docs/v2/data/obs_measurements.json")
    ap.add_argument("--detail-dir", default="evals/runs/_analysis/obs_gaps")
    a = ap.parse_args()

    data = json.load(open(a.data, encoding="utf-8"))
    table = [json.loads(l) for l in open(a.table, encoding="utf-8")]
    tmap = {(x["item"], x["qid"]): x for x in table}

    res: dict = {}
    # ---------- turn-level ----------
    turn_flags: dict[tuple[str, str], dict] = {}
    turn_text: dict[tuple[str, str], str] = {}
    cap_text: dict[tuple[str, str], tuple[str, str]] = {}
    sess_nick: dict[tuple[str, int], int] = defaultdict(
        int
    )  # (conv, session) -> n short-form vocatives
    nick_turns = 0
    spk_map = {}
    for si, conv in enumerate(data):
        item = f"conv-{conv['sample_id'].split('-')[-1]}" if "sample_id" in conv else f"conv-{si}"
        c = conv["conversation"]
        sp = {c["speaker_a"], c["speaker_b"]}
        spk_map[item] = sp
        for k, v in c.items():
            if k.startswith("session_") and isinstance(v, list):
                sn = int(k.split("_")[1])
                for t in v:
                    fl = flags_for_turn(t, sp)
                    # short form of a speaker name used as a vocative
                    for m in GREET.finditer(t["text"]):
                        w = m.group(1)
                        if w not in sp and any(s.startswith(w) and len(w) >= 3 for s in sp):
                            fl["short_form"] = True
                    if fl.get("short_form"):
                        nick_turns += 1
                        sess_nick[(item, sn)] += 1
                    if t.get("blip_caption"):
                        cap_text[(item, t["dia_id"])] = (t["text"], t["blip_caption"])
                    turn_flags[(item, t["dia_id"])] = fl
                    turn_text[(item, t["dia_id"])] = t["text"]
    # previous turn of the same session (for carry-over measurements)
    prev_text: dict[tuple[str, str], str] = {}
    for si, conv in enumerate(data):
        item = f"conv-{conv['sample_id'].split('-')[-1]}"
        for k, v in conv["conversation"].items():
            if k.startswith("session_") and isinstance(v, list):
                for i in range(1, len(v)):
                    prev_text[(item, v[i]["dia_id"])] = v[i - 1]["text"]
    n_turns = len(turn_flags)
    cnt = Counter()
    for fl in turn_flags.values():
        for k, v in fl.items():
            if v:
                cnt[k] += 1
    res["turns"] = {"total": n_turns, **{k: cnt[k] for k in sorted(cnt)}}
    res["sessions_with_short_form"] = len({k for k, v in sess_nick.items() if v})
    res["sessions_total"] = sum(
        1
        for conv in data
        for k, v in conv["conversation"].items()
        if k.startswith("session_") and isinstance(v, list)
    )

    # window arithmetic (model-call counts of the proposals): windows of w turns with stride st
    win: dict = {}
    for w, st in ((6, 6), (4, 3), (4, 4)):
        n_win = n_flag = 0
        for conv in data:
            item = f"conv-{conv['sample_id'].split('-')[-1]}"
            for k, v in conv["conversation"].items():
                if k.startswith("session_") and isinstance(v, list):
                    i = 0
                    while i < len(v):
                        chunk = v[i : i + w]
                        n_win += 1
                        if any(
                            turn_flags[(item, t["dia_id"])].get("incomplete_strict") for t in chunk
                        ):
                            n_flag += 1
                        if i + w >= len(v):
                            break
                        i += st
        win[f"w{w}_stride{st}"] = {"windows": n_win, "windows_with_incomplete_turn": n_flag}
    res["windows"] = win
    res["turns_per_session_mean"] = round(n_turns / res["sessions_total"], 1)
    res["turn_words_mean"] = round(sum(len(t.split()) for t in turn_text.values()) / n_turns, 1)

    # ---------- question-level over the problem table ----------
    def gold_keys(x):
        return [(x["item"], g["turn"]) for g in x["gold_turns"]]

    def sess_of(g):
        return g.get("session")

    def pred_q(x):
        q = x["question"]
        sp = spk_map.get(x["item"], set())
        names_in_q = [s for s in sp if re.search(rf"\b{re.escape(s)}\b", q)]
        gts = x["gold_turns"]
        flg = [turn_flags.get((x["item"], g["turn"]), {}) for g in gts]
        p = {}
        p["both_names"] = len(names_in_q) >= 2
        p["setop"] = bool(SETOP_Q.search(q))
        p["multi_entity"] = p["both_names"] or p["setop"]
        p["compare"] = bool(COMPARE_Q.search(q))
        p["count"] = bool(COUNT_Q.search(q))
        p["attr"] = bool(ATTR_Q.search(q))
        p["infer_stem"] = bool(INFER_Q.search(q))
        p["ans_refusal"] = bool(REFUSAL.search(str(x.get("answer") or "")))
        p["gold_caption"] = any(f.get("caption") for f in flg)
        capdec = False
        for g in gts:
            tt = cap_text.get((x["item"], g["turn"]))
            if tt:
                cap_c = content(tt[1], {s.lower() for s in sp})
                spk_c = content(tt[0], {s.lower() for s in sp})
                qq = content(q, {s.lower() for s in sp})
                if (qq & cap_c) and not (qq & spk_c):
                    capdec = True
        p["caption_decisive"] = capdec
        carry = False
        for g in gts:
            own = g.get("text") or turn_text.get((x["item"], g["turn"]), "") or ""
            prev = prev_text.get((x["item"], g["turn"]), "")
            qq2 = content(q, {s.lower() for s in sp})
            if (
                len(qq2 & content(prev, {s.lower() for s in sp})) >= 2
                and len(qq2 & content(own, {s.lower() for s in sp})) <= 1
            ):
                carry = True
        p["prev_turn_carries_terms"] = carry
        p["state_change_q"] = bool(STATE_Q.search(q))
        p["gold_incomplete"] = any(f.get("incomplete") for f in flg)
        p["gold_incomplete_strict"] = any(f.get("incomplete_strict") for f in flg)
        p["gold_followup"] = any(f.get("followup_question") or f.get("reaction_open") for f in flg)
        p["gold_pron3"] = any(f.get("pron3_no_antecedent") for f in flg)
        p["gold_plural"] = any(f.get("plural_pron") for f in flg)
        p["gold_reltime"] = any(f.get("relative_time") for f in flg)
        p["gold_backref"] = any(f.get("backref") for f in flg)
        p["gold_short_form_session"] = any(
            sess_nick.get((x["item"], sess_of(g)), 0) > 0 for g in gts if sess_of(g)
        )
        p["gold_short_form_turn"] = any(f.get("short_form") for f in flg)
        # lexical mismatch: question has zero stem overlap with every gold turn (heuristic)
        qc = content(q, {s.lower() for s in sp})
        ov = [
            len(
                qc
                & content(
                    g.get("text") or turn_text.get((x["item"], g["turn"]), "") or "",
                    {s.lower() for s in sp},
                )
            )
            for g in gts
        ]
        p["zero_overlap_gold"] = bool(gts) and max(ov, default=0) == 0
        p["low_overlap_gold"] = bool(gts) and max(ov, default=0) <= 1

        # measured: BM25 missed (rank None or >10) while vector found within 10; or neither within 60
        def r(v):
            return v if isinstance(v, int) else None

        vs = [r(g.get("vector")) for g in gts]
        ls = [r(g.get("lexical")) for g in gts]
        p["bm25_miss_vec_hit"] = any(
            (v is not None and v <= 10) and (l is None or l > 10) for v, l in zip(vs, ls)
        )
        p["vec_miss_bm25_hit"] = any(
            (l is not None and l <= 10) and (v is None or v > 10) for v, l in zip(vs, ls)
        )
        p["both_miss"] = bool(gts) and any(v is None and l is None for v, l in zip(vs, ls))
        p["spans_3_sessions"] = len({sess_of(g) for g in gts}) >= 3
        p["n_gold_ge3"] = len(gts) >= 3
        return p

    rows = []
    for x in table:
        p = pred_q(x)
        rows.append((x, p))
    wrong = [(x, p) for x, p in rows if str(x["correct"]).lower() != "true"]
    res["questions"] = {"total": len(rows), "wrong": len(wrong)}

    def tally(sel):
        allq = [(x, p) for x, p in rows if sel(x, p)]
        wq = [(x, p) for x, p in allq if str(x["correct"]).lower() != "true"]
        return {
            "all": len(allq),
            "wrong": len(wq),
            "wrong_by_class": dict(Counter(x["rclass"] for x, _ in wq).most_common()),
            "wrong_by_category": dict(Counter(x["category"] for x, _ in wq).most_common()),
        }

    preds = {
        "multi_entity_both_names": lambda x, p: p["both_names"],
        "multi_entity_setop_words": lambda x, p: p["setop"],
        "multi_entity_any": lambda x, p: p["multi_entity"],
        "comparison_words": lambda x, p: p["compare"],
        "count_question": lambda x, p: p["count"],
        "attribute_lookup_question": lambda x, p: p["attr"],
        "inference_stem_question": lambda x, p: p["infer_stem"],
        "open_domain_category": lambda x, p: x["category"] == "open-domain",
        "open_domain_refusal_answer": lambda x, p: (
            x["category"] == "open-domain" and p["ans_refusal"]
        ),
        "any_category_refusal_answer": lambda x, p: p["ans_refusal"],
        "gold_has_caption_turn": lambda x, p: p["gold_caption"],
        "prev_turn_carries_question_terms": lambda x, p: p["prev_turn_carries_terms"],
        "state_change_wording": lambda x, p: p["state_change_q"],
        "gold_caption_decisive_for_question": lambda x, p: p["caption_decisive"],
        "gold_turn_incomplete_strict": lambda x, p: p["gold_incomplete_strict"],
        "gold_turn_incomplete_by_rule": lambda x, p: p["gold_incomplete"],
        "gold_turn_followup_or_reaction": lambda x, p: p["gold_followup"],
        "gold_turn_pronoun_no_antecedent": lambda x, p: p["gold_pron3"],
        "gold_turn_plural_pronoun": lambda x, p: p["gold_plural"],
        "gold_turn_relative_time": lambda x, p: p["gold_reltime"],
        "gold_turn_backreference": lambda x, p: p["gold_backref"],
        "gold_in_session_with_short_form": lambda x, p: p["gold_short_form_session"],
        "gold_turn_with_short_form": lambda x, p: p["gold_short_form_turn"],
        "zero_content_overlap_question_gold": lambda x, p: p["zero_overlap_gold"],
        "le1_content_overlap_question_gold": lambda x, p: p["low_overlap_gold"],
        "bm25_miss_vector_hit_MEASURED": lambda x, p: p["bm25_miss_vec_hit"],
        "vector_miss_bm25_hit_MEASURED": lambda x, p: p["vec_miss_bm25_hit"],
        "neither_leg_in_top60_MEASURED": lambda x, p: p["both_miss"],
        "gold_spans_3plus_sessions": lambda x, p: p["spans_3_sessions"],
        "gold_3plus_turns": lambda x, p: p["n_gold_ge3"],
        "temporal_category": lambda x, p: x["category"] == "temporal",
        "multi_hop_category": lambda x, p: x["category"] == "multi-hop",
    }
    res["predicates"] = {k: tally(f) for k, f in preds.items()}

    # class sizes (measured)
    res["wrong_by_class"] = dict(Counter(x["rclass"] for x, _ in wrong).most_common())
    res["wrong_by_stage"] = dict(Counter(x["stage"] for x, _ in wrong).most_common())

    # combined predicates used in the document
    def cnt_wrong(sel):
        return sum(1 for x, p in wrong if sel(x, p))

    res["combined_wrong"] = {
        "incomplete_gold AND retrieval_stage(b,cf,cr)": cnt_wrong(
            lambda x, p: p["gold_incomplete"] and x["stage"] in ("b", "cf", "cr")
        ),
        "short_form_session AND retrieval_stage": cnt_wrong(
            lambda x, p: p["gold_short_form_session"] and x["stage"] in ("b", "cf", "cr")
        ),
        "multi_entity AND (multi-hop OR single-hop) AND not ok": cnt_wrong(
            lambda x, p: p["multi_entity"] and x["category"] in ("multi-hop", "single-hop")
        ),
        "open_domain AND refusal_answer": cnt_wrong(
            lambda x, p: x["category"] == "open-domain" and p["ans_refusal"]
        ),
        "open_domain AND not_refusal": cnt_wrong(
            lambda x, p: x["category"] == "open-domain" and not p["ans_refusal"]
        ),
        "attr AND (single-hop OR multi-hop)": cnt_wrong(
            lambda x, p: p["attr"] and x["category"] in ("single-hop", "multi-hop")
        ),
        "temporal wrong with relative-time gold": cnt_wrong(
            lambda x, p: x["category"] == "temporal" and p["gold_reltime"]
        ),
        "lexical: (zero_overlap OR bm25_miss_vec_hit) AND retrieval_stage": cnt_wrong(
            lambda x, p: (
                (p["zero_overlap_gold"] or p["bm25_miss_vec_hit"])
                and x["stage"] in ("b", "cf", "cr")
            )
        ),
    }

    # detail file (text-bearing, local only)
    out_dir = Path(a.detail_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "wrong_predicates.jsonl", "w", encoding="utf-8") as fh:
        for x, p in wrong:
            fh.write(
                json.dumps(
                    {
                        "item": x["item"],
                        "qid": x["qid"],
                        "category": x["category"],
                        "rclass": x["rclass"],
                        "stage": x["stage"],
                        **p,
                    }
                )
                + "\n"
            )
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(
        json.dumps(
            {
                k: res[k]
                for k in (
                    "turns",
                    "questions",
                    "sessions_with_short_form",
                    "sessions_total",
                    "combined_wrong",
                )
            },
            indent=1,
        )
    )
    for k, v in res["predicates"].items():
        print(f"{k:42s} all={v['all']:5d} wrong={v['wrong']:4d}")


if __name__ == "__main__":
    main()
