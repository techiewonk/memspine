"""R14 DEEP-READ: reader / distractor / judge features for clean-jina-full (CPU only, no model call).

Reads the run files and the LoCoMo data (local, licensed text), computes deterministic features
and heuristic judge-suspect flags, writes
  * evals/analysis/r14_reader_judge_features.csv   (ids, labels and numbers only, NO text)
  * evals/runs/_analysis/r14/features_full.jsonl    (local only, with text for hand review)
  * evals/runs/_analysis/r14/summary.json           (aggregate counts used by the report)

Run:  PYTHONPATH=<worktree>/src;<worktree>/evals python evals/analysis/r14_reader_judge.py
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WT = HERE.parents[2]
MAIN = Path("C:/Users/hembad/Documents/GitHub/memspine")
RUN = MAIN / "evals/runs/clean-jina-full"
DATA = MAIN / "data/locomo10.json"
OUT_LOCAL = WT / "evals/runs/_analysis/r14"
OUT_LOCAL.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(WT / "evals"))
sys.path.insert(0, str(WT / "src"))
from memspine_evals.judge_conventions import (
    _extended_conventions,
    check_conventions,
    gold_items,
)
from memspine_evals.refusal import is_refusal

STOP = set(
    [
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "at",
        "and",
        "or",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "were",
        "that",
        "this",
        "it",
        "its",
        "his",
        "her",
        "their",
        "my",
        "our",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "he",
        "she",
        "they",
        "them",
        "we",
        "you",
        "i",
        "me",
        "do",
        "does",
        "did",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "not",
        "no",
        "yes",
        "about",
        "after",
        "before",
        "into",
        "over",
        "than",
        "then",
        "there",
        "so",
        "but",
        "if",
        "also",
        "very",
        "just",
        "would",
        "could",
        "should",
        "will",
        "can",
        "may",
        "might",
        "up",
        "out",
        "one",
        "s",
        "t",
    ]
)
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
WD = {d.lower(): i for i, d in enumerate(calendar.day_name)}
NUMW = {
    w: i
    for i, w in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
        ]
    )
}
NUMW.update({"once": 1, "twice": 2, "thrice": 3})


def toks(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def content(s: str) -> list[str]:
    return [stem(w) for w in toks(s) if w not in STOP and not w.isdigit()]


def norm(s: str) -> str:
    return " ".join(w for w in toks(s) if w not in {"the", "a", "an"})


# ---------------------------------------------------------------- dates
MON_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (
        re.compile(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MON_RE})\b\.?,?\s*(\d{{4}})?", re.I
        ),
        lambda m: (int(m[3]) if m[3] else None, MONTHS[m[2].lower()], int(m[1])),
    ),
    (
        re.compile(
            rf"\b({MON_RE})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?:years?|months?|weeks?|days?))(?:,?\s*(\d{{4}}))?",
            re.I,
        ),
        lambda m: (int(m[3]) if m[3] else None, MONTHS[m[1].lower()], int(m[2])),
    ),
]


def days_in(text: str) -> list[tuple[int | None, int, int]]:
    out: list[tuple[int, tuple]] = []
    for pat, fn in DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                y, mo, d = fn(m)
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    out.append((m.start(), (y, mo, d)))
            except Exception:
                pass
    out.sort()
    seen, res = set(), []
    for _, v in out:
        if v not in seen:
            seen.add(v)
            res.append(v)
    return res


def to_date(t) -> dt.date | None:
    y, mo, d = t
    if y is None:
        return None
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def month_year(text: str) -> list[tuple[int, int]]:
    out = []
    for m in re.finditer(rf"\b({MON_RE})\b\.?,?\s+(\d{{4}})\b", text, re.I):
        out.append((int(m[2]), MONTHS[m[1].lower()]))
    return out


def gold_dayset(gold: str) -> tuple[str, set[dt.date] | None]:
    """Classify a date gold and resolve it to a set of acceptable days (None = not a date gold)."""
    g = gold.strip()
    low = g.lower()
    ds = [to_date(t) for t in days_in(g)]
    ds = [d for d in ds if d]
    m = re.search(r"\b(?:the\s+)?(" + "|".join(WD) + r")\s+(before|after)\b", low)
    if m and ds:
        wd, rel = WD[m[1]], m[2]
        d0 = ds[0]
        step = -1 if rel == "before" else 1
        d = d0 + dt.timedelta(days=step)
        while d.weekday() != wd:
            d += dt.timedelta(days=step)
        return "weekday_rel", {d}
    m = re.search(r"\b(?:the\s+)?week\s+(before|after|of)\b", low)
    if m and ds:
        d0 = ds[0]
        if m[1] == "before":
            return "week_rel", {d0 - dt.timedelta(days=k) for k in range(1, 8)}
        if m[1] == "after":
            return "week_rel", {d0 + dt.timedelta(days=k) for k in range(1, 8)}
        return "week_rel", {d0 + dt.timedelta(days=k) for k in range(-1, 8)}
    m = re.search(r"\bweekend\s+(before|after|of)\b", low)
    if m and ds:
        d0 = ds[0]
        s = set()
        for k in range(1, 9):
            d = d0 - dt.timedelta(days=k) if m[1] == "before" else d0 + dt.timedelta(days=k)
            if d.weekday() in (5, 6):
                s.add(d)
        return "weekend_rel", s
    if ds and len(ds) == 1 and re.search(r"\d", g):
        return "abs_day", {ds[0]}
    my = month_year(g)
    if my and len(my) == 1 and not ds:
        y, mo = my[0]
        n = calendar.monthrange(y, mo)[1]
        return "month", {dt.date(y, mo, k) for k in range(1, n + 1)}
    if re.fullmatch(r"\s*(in\s+)?(\d{4})\s*", low):
        y = int(re.search(r"\d{4}", low)[0])
        return "year", {dt.date(y, 1, 1) + dt.timedelta(days=k) for k in range(365)}
    if re.search(
        r"\b(ago|last|next|since|yesterday|tomorrow|week|month|year)s?\b", low
    ) and not re.search(r"\d", low):
        return "rel_other", None
    if re.search(rf"\b({MON_RE})\b", low) and re.search(r"\d", low):
        return "date_other", None
    return "none", None


def answer_dates(ans: str) -> list[dt.date]:
    return [d for d in (to_date(t) for t in days_in(ans)) if d]


NEG = re.compile(
    r"\b(not|never|no|none|cannot|can't|didn't|did not|doesn't|isn't|wasn't|unknown|unclear)\b",
    re.I,
)
HEDGE = re.compile(
    r"\b(or|either|possibly|perhaps|maybe|might|may have|probably|likely|approximately|around|about|roughly|at least)\b",
    re.I,
)
Q_SHAPES = [
    (
        "how_long",
        re.compile(r"\bhow long\b|\bhow many (days|weeks|months|years|hours|minutes)\b", re.I),
    ),
    ("how_many", re.compile(r"\bhow many\b|\bhow often\b|\bhow much\b", re.I)),
    (
        "when",
        re.compile(
            r"\bwhen\b|\bwhat (year|month|day|date)\b|\bwhich (year|month|day)\b|\bwhat time\b",
            re.I,
        ),
    ),
    (
        "yesno",
        re.compile(r"^\s*(would|is|are|was|were|did|does|do|can|could|will|has|have|had)\b", re.I),
    ),
    (
        "list",
        re.compile(
            r"\bwhat (are|were)\b.*\b(s)\b|\b(which|what) .* (activities|books|places|things|items|hobbies|games|pets|countries|events|foods|movies)\b|\blist\b|\ball\b",
            re.I,
        ),
    ),
    ("who", re.compile(r"^\s*who\b", re.I)),
    ("where", re.compile(r"^\s*where\b|\bwhich (city|country|state|place)\b", re.I)),
    ("why", re.compile(r"^\s*why\b|\bhow (did|does|do|has|have|would)\b", re.I)),
    ("what_which", re.compile(r"^\s*(what|which)\b", re.I)),
]


def q_shape(q: str) -> str:
    for name, rx in Q_SHAPES:
        if rx.search(q):
            return name
    return "other"


def numerals(text: str) -> list[int]:
    out = []
    for w in re.findall(r"\b\w+\b", text.lower()):
        if w.isdigit() and len(w) <= 3:
            out.append(int(w))
        elif w in NUMW:
            out.append(NUMW[w])
    return out


def gold_type(gold: str) -> str:
    low = gold.lower().strip()
    kind, _ = gold_dayset(gold)
    if kind != "none":
        return kind
    if low in {"yes", "no"} or re.match(r"^(yes|no)\b[,.]?", low):
        return "yesno"
    if re.fullmatch(
        r"(about |around |approximately )?(\d+|"
        + "|".join(NUMW)
        + r")( times| days| weeks| months| years| hours)?",
        low,
    ):
        return "number"
    if re.search(r"\b\d+\s*(days?|weeks?|months?|years?)\b", low):
        return "duration"
    if len(gold_items(gold)) >= 2 or ("," in gold and len(gold.split(",")) >= 2):
        return "list"
    if len(low.split()) <= 3:
        return "short_entity"
    return "phrase"


def bins(x, edges):
    for i, e in enumerate(edges):
        if x < e:
            return i
    return len(edges)


# ---------------------------------------------------------------- hand labels (ids only)
# One annotator (the R14 DEEP-READ pass) read all 287 WRONG verdicts and, for CORRECT verdicts, every row
# the deterministic rules flagged (193 rows) plus a random 70 unflagged ones. Tiers: clear = any grader
# accepts/rejects; likely = most graders; borderline = a coin flip. Keys are "<conv>/<qid>".
def _k(spec: str) -> set[str]:
    return {
        f"conv-{c}/{q}" for c, qs in (x.split(":") for x in spec.split(";")) for q in qs.split(",")
    }


HAND_FN = {
    "clear": _k("26:0-70,0-135;42:3-183;43:4-30,4-174;48:7-101,7-145;50:9-60,9-64,9-127"),
    "likely": _k("42:3-173;47:6-26,6-30,6-35"),
    "borderline": set(),  # filled below (conversation ids differ per entry)
}
HAND_FN["borderline"] = {
    "conv-30/1-17",
    "conv-42/3-134",
    "conv-42/3-148",
    "conv-48/7-8",
    "conv-49/8-154",
    "conv-49/8-18",
    "conv-49/8-37",
    "conv-49/8-54",
    "conv-50/9-1",
}
HAND_FP = {
    "clear": _k(
        "26:0-23,0-60,0-78;41:2-30,2-32,2-36,2-6;42:3-46,3-59,3-63,3-75;43:4-65,4-52;47:6-40;49:8-7,8-50;50:9-30"
    ),
    "likely": _k("43:4-4,4-9,4-62;50:9-5;48:7-59;44:5-9;42:3-83,3-11,3-88;26:0-2;41:2-91;47:6-38"),
    "borderline": _k("48:7-74,7-99,7-85;42:3-8;26:0-147"),
}
# gold / question defects found while reading the 287 wrong rows that are NOT in locomo_errata.json
HAND_GOLD_CANDIDATE = {
    "conv-43/4-55": "gold date is not entailed by the evidence turn (a relative duration on a later line points elsewhere)",
    "conv-26/0-135": "question premise month differs from the month the evidence line resolves to",
    "conv-49/8-25": "gold year needs a for-N-years start-date computation; annotator leaves it unresolved",
}
# 18-row hand check of the 'distractor' source heuristic (True = the answer really draws on a non-gold line)
HAND_DISTRACTOR_CHECK = {
    "conv-49/8-104": True,
    "conv-42/3-169": True,
    "conv-50/9-115": True,
    "conv-42/3-19": False,
    "conv-48/7-44": False,
    "conv-43/4-76": True,
    "conv-30/1-17": True,
    "conv-43/4-128": True,
    "conv-41/2-85": True,
    "conv-47/6-64": False,
    "conv-43/4-106": True,
    "conv-47/6-17": False,
    "conv-48/7-178": True,
    "conv-26/0-151": True,
    "conv-47/6-147": False,
    "conv-30/1-57": True,
    "conv-50/9-56": True,
    "conv-50/9-109": True,
}
HAND_SIBLING_EVIDENCE = {
    "conv-30/1-17",
    "conv-30/1-57",
    "conv-48/7-178",
}  # answer is valid text from another turn; gold is one of several


def hand_tier(key: str, table: dict) -> str:
    for tier in ("clear", "likely", "borderline"):
        if key in table[tier]:
            return tier
    return ""


# ---------------------------------------------------------------- load
def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf8"))
    conv = {}
    for k, it in enumerate(data):
        cid = f"conv-{k + 0}"
    # item id mapping from results: conv-26 etc come from sample_id
    by_id = {it["sample_id"]: it for it in data}
    turns: dict[str, dict[str, dict]] = {}
    for sid, it in by_id.items():
        c = it["conversation"]
        tt = {}
        for sk, sv in c.items():
            if sk.startswith("session_") and isinstance(sv, list):
                for t in sv:
                    tt[t["dia_id"]] = t
        turns[sid] = tt
    qa = {}
    for sid, it in by_id.items():
        for i, q in enumerate(it["qa"]):
            qa[(sid, f"{list(by_id).index(sid)}-{i}")] = q

    res = [
        json.loads(line)
        for line in (RUN.parent / "clean-jina-full--memspine/results.jsonl").open(encoding="utf8")
    ]
    res = [r for r in res if r.get("kind") == "result"]
    forn = {}
    for line in (RUN.parent / "clean-jina-full--forensics/forensics.jsonl").open(encoding="utf8"):
        d = json.loads(line)
        forn[d["query_id"]] = d
    labels = {}
    for line in (
        (WT / "evals/analysis/locomo_problem_labels_clean_jina_full.txt")
        .read_text(encoding="utf8")
        .splitlines()
    ):
        if line.startswith("#") or not line.strip() or line.startswith("qid"):
            continue
        p = line.split()
        labels[(p[0], p[1])] = (p[2], p[3] if len(p) > 3 else "")
    errata = {}
    for e in json.loads((WT / "evals/analysis/locomo_errata.json").read_text(encoding="utf8"))[
        "entries"
    ]:
        errata.setdefault((e["item"], e["qid"]), []).append(e["tag"])

    # other runs for judge-consistency and stable-failure
    other = {}
    for rn in [
        "full-persp-loc",
        "r7-multprot-full",
        "r7-post-full",
        "r7-protect-full",
        "qa-full-qs-eq06-fix",
        "qa-full-qs-eq06-rjina",
        "qa-full-qs-ejina-rjina",
    ]:
        p = RUN.parent / f"{rn}--memspine/results.jsonl"
        if not p.exists():
            continue
        for line in p.open(encoding="utf8"):
            d = json.loads(line)
            if d.get("kind") == "result":
                other.setdefault((d["item_id"], d["query_id"]), []).append(
                    (rn, d["score"], d["answer"], (d.get("meta") or {}).get("judge_prompt_id"))
                )

    rows = []
    for r in res:
        sid, qid = r["item_id"], r["query_id"]
        q = qa[(sid, qid)]
        T = turns[sid]
        f = forn[qid]
        gold, ans, question = r["gold"], r["answer"] or "", r["question"]
        correct = r["score"] == 1.0
        # evidence ids
        ev_raw = q.get("evidence", [])
        ev = []
        for e in ev_raw:
            ev += re.findall(r"D\d+:\d+", e)
        ev = list(dict.fromkeys(ev))
        ev_valid = [e for e in ev if e in T]
        ctx = [c["turn"] for c in f["context_records"]]
        ctx_text = {c["turn"]: c["text"] for c in f["context_records"]}
        ctx_set = set(ctx)
        n_ctx = len(ctx)
        gin = [e for e in ev_valid if e in ctx_set]
        all_in = len(ev_valid) > 0 and len(gin) == len(ev_valid)
        any_in = len(gin) > 0
        pos_first = pos_last = from_end = None
        if gin:
            idxs = [ctx.index(e) for e in gin]
            pos_first = min(idxs) / max(n_ctx - 1, 1)
            pos_last = max(idxs) / max(n_ctx - 1, 1)
            from_end = n_ctx - 1 - max(idxs)
        fr = {c["turn"]: c["r"] for c in f["final"]}
        gold_best_rank = min([fr[e] for e in ev_valid if e in fr], default=None)
        lab = labels.get((sid, qid), ("", ""))
        stage = lab[0] if not correct else ""
        stage_d = (not correct) and lab[0].startswith("d")
        # --- tokens
        gt = set(content(gold))
        at = set(content(ans))
        qt = set(content(question))
        gold_recall = (len(gt & at) / len(gt)) if gt else None
        ans_prec = (len(gt & at) / len(at)) if at else None
        gold_turn_tokens = set()
        for e in ev_valid:
            gold_turn_tokens |= set(content(T[e]["text"] + " " + (T[e].get("blip_caption") or "")))
        nongold_ctx_tokens = set()
        for t in ctx:
            if t not in ev_valid:
                nongold_ctx_tokens |= set(content(ctx_text[t]))
        # annotation text inside context e.g. [= ...] counts as context, not as turn text
        novel = [w for w in at if w not in qt and w not in gt]
        n_nov = len(novel)
        nov_gold = sum(1 for w in novel if w in gold_turn_tokens)
        nov_dist = sum(1 for w in novel if w not in gold_turn_tokens and w in nongold_ctx_tokens)
        nov_none = sum(
            1 for w in novel if w not in gold_turn_tokens and w not in nongold_ctx_tokens
        )
        refusal = bool(is_refusal(ans))
        if refusal:
            src = "refusal"
        elif n_nov == 0:
            src = "no_novel"
        elif nov_dist >= max(2, nov_gold + 1) and nov_dist >= 0.4 * n_nov:
            src = "distractor"
        elif nov_none >= 0.5 * n_nov:
            src = "ungrounded"
        elif nov_gold >= 0.5 * n_nov:
            src = "from_gold_turn"
        else:
            src = "mixed"
        gtype = gold_type(gold)
        shape = q_shape(question)
        # speakers
        spk = {by_id[sid]["conversation"]["speaker_a"], by_id[sid]["conversation"]["speaker_b"]}
        named = [s for s in spk if re.search(r"\b" + re.escape(s) + r"\b", question)]
        ev_spk = {T[e]["speaker"] for e in ev_valid}
        spk_mismatch = bool(len(named) == 1 and ev_spk and named[0] not in ev_spk)
        ans_named = [s for s in spk if re.search(r"\b" + re.escape(s) + r"\b", ans)]
        wrong_spk_answer = bool(
            (not correct) and len(named) == 1 and ans_named and named[0] not in ans_named
        )
        # captions
        cap_gold = [e for e in ev_valid if T[e].get("blip_caption")]
        cap_only = False
        if cap_gold and gt:
            txt = set(content(" ".join(T[e]["text"] for e in ev_valid)))
            cap = set(content(" ".join(T[e].get("blip_caption", "") for e in cap_gold)))
            cap_only = bool((gt - qt) & cap and not ((gt - qt) & txt))
        # sessions of evidence
        n_sess = len({e.split(":")[0] for e in ev_valid})
        a_words = len(ans.split())
        g_words = len(gold.split())
        # ---------------- judge heuristics
        fn = []
        fp = []
        kind, dset = gold_dayset(gold)
        adates = answer_dates(ans)
        a_refusal = refusal
        a_neg = (
            bool(NEG.search(ans[:80]))
            and not re.search(r"\bno\b", gold.lower())
            and gtype != "yesno"
        )
        if not correct:
            if dset and adates and not a_refusal:
                if adates[0] in dset and kind in ("abs_day", "weekday_rel"):
                    fn.append("date_exact_first")
                elif any(d in dset for d in adates) and kind in ("abs_day", "weekday_rel"):
                    fn.append("date_exact_any")
                elif kind in ("week_rel", "weekend_rel", "month", "year") and adates[0] in dset:
                    fn.append("date_in_range")
            if dset and kind == "month" and not adates and not a_refusal:
                my = month_year(ans)
                if my and (my[0][0], my[0][1]) == (min(dset).year, min(dset).month):
                    fn.append("month_year_match")
            ng = norm(gold)
            na = norm(ans)
            if (
                ng
                and len(ng.split()) >= 1
                and re.search(r"\b" + re.escape(ng) + r"\b", na)
                and not a_refusal
                and not a_neg
                and a_words <= 45
                and kind == "none"
            ):
                fn.append("gold_contained")
            if (
                gold_recall is not None
                and gold_recall >= 0.8
                and len(gt) >= 2
                and not a_refusal
                and not a_neg
                and a_words <= 45
                and kind == "none"
            ):
                fn.append("token_recall_ge08")
            if (
                at
                and gt
                and at <= gt
                and len(at) >= 1
                and gtype in ("phrase", "short_entity")
                and not a_refusal
                and not a_neg
            ):
                fn.append("answer_subset_of_gold")
            try:
                if check_conventions(question, ans, gold):
                    fn.append("conv_v1")
                if _extended_conventions(question, ans, gold):
                    fn.append("conv_v2")
            except Exception:
                pass
            gn, an = numerals(gold), numerals(ans)
            if (
                gtype == "number"
                and gn
                and an
                and gn[0] in an[:3]
                and not re.search(r"at least|more than|over|fewer|less than", ans.lower())
            ):
                fn.append("number_in_answer")
        else:
            gi = gold_items(gold)
            if len(gi) >= 2 or gtype == "list":
                items = (
                    gi
                    if len(gi) >= 2
                    else [x.strip() for x in re.split(r",|\band\b", gold) if x.strip()]
                )
                have = 0
                for it in items:
                    ct = set(content(it))
                    if ct and len(ct & at) / len(ct) >= 0.6:
                        have += 1
                if items and have < len(items):
                    fp.append("list_item_missing")
                    if have / len(items) <= 0.5:
                        fp.append("list_half_or_less")
            if dset and kind in ("abs_day", "weekday_rel"):
                if adates and not any(d in dset for d in adates):
                    fp.append("date_mismatch")
                elif not adates and not month_year(ans) and not a_refusal:
                    fp.append("date_absent_in_answer")
            if kind in ("month",) and adates and not any(d in dset for d in adates):
                fp.append("month_mismatch")
            if kind == "year" and adates and not any(d in dset for d in adates):
                fp.append("year_mismatch")
            gn, an = numerals(gold), numerals(ans)
            if gtype == "number" and gn and an and gn[0] not in an:
                fp.append("number_mismatch")
            if gtype == "number" and gn and an and gn[0] in an and len(set(an)) >= 3:
                fp.append("number_among_many")
            if (
                gold_recall is not None
                and gold_recall < 0.34
                and len(gt) >= 2
                and kind == "none"
                and not (an and gn and gn[0] in an)
            ):
                fp.append("low_gold_recall")
            if a_refusal:
                fp.append("refusal_credited")
            if (
                HEDGE.search(ans)
                and re.search(r"\b(or|either)\b", ans.lower())
                and gold_recall is not None
                and gtype in ("short_entity", "phrase", "list")
            ):
                fp.append("hedged_or")
            if a_words >= 60:
                fp.append("long_answer")
            if gtype == "yesno":
                gy = gold.lower().strip().startswith("yes")
                ay = (
                    ans.lower().strip().startswith("no")
                    if gy
                    else ans.lower().strip().startswith("yes")
                )
                if ay:
                    fp.append("polarity_mismatch")
        # judge noise across runs: same normalised answer, different verdicts
        oth = other.get((sid, qid), [])
        same_ans_diff = [
            (rn, s) for rn, s, a, jp in oth if norm(a) == norm(ans) and (s == 1.0) != correct
        ]
        runs_wrong = sum(1 for _, s, _, _ in oth if s != 1.0) + (0 if correct else 1)
        runs_n = len(oth) + 1
        rows.append(
            dict(
                item=sid,
                qid=qid,
                cat=r["type_label"],
                correct=int(correct),
                stage=lab[0] if not correct else "",
                rclass=lab[1] if not correct else "",
                stage_d=int(stage_d),
                n_ev=len(ev),
                n_ev_valid=len(ev_valid),
                n_ev_sessions=n_sess,
                bad_ev_ids=len(ev) - len(ev_valid),
                n_ctx=n_ctx,
                ctx_tokens=r["context_tokens"],
                gold_in_ctx_n=len(gin),
                all_gold_in_ctx=int(all_in),
                any_gold_in_ctx=int(any_in),
                gold_pos_first=None if pos_first is None else round(pos_first, 3),
                gold_pos_last=None if pos_last is None else round(pos_last, 3),
                gold_from_end=from_end,
                gold_best_final_rank=gold_best_rank,
                gold_type=gtype,
                q_shape=shape,
                ans_words=a_words,
                gold_words=g_words,
                len_ratio=round(a_words / max(g_words, 1), 2),
                refusal=int(refusal),
                gold_recall=None if gold_recall is None else round(gold_recall, 3),
                ans_precision=None if ans_prec is None else round(ans_prec, 3),
                novel_n=n_nov,
                novel_in_gold_turn=nov_gold,
                novel_in_distractor=nov_dist,
                novel_ungrounded=nov_none,
                answer_source=src,
                speaker_mismatch_gold=int(spk_mismatch),
                wrong_speaker_answer=int(wrong_spk_answer),
                caption_gold=int(bool(cap_gold)),
                caption_only_gold=int(cap_only),
                errata_tags=";".join(errata.get((sid, qid), [])),
                fn_flags=";".join(fn),
                fp_flags=";".join(fp),
                runs_n=runs_n,
                runs_wrong=runs_wrong,
                same_answer_other_verdict=len(same_ans_diff),
                retried=int("retry_answer" in (r.get("meta") or {})),
                hand_fn=hand_tier(f"{sid}/{qid}", HAND_FN),
                hand_fp=hand_tier(f"{sid}/{qid}", HAND_FP),
                hand_gold_candidate=int(f"{sid}/{qid}" in HAND_GOLD_CANDIDATE),
                hand_distractor=(
                    ""
                    if f"{sid}/{qid}" not in HAND_DISTRACTOR_CHECK
                    else int(HAND_DISTRACTOR_CHECK[f"{sid}/{qid}"])
                ),
                hand_sibling_evidence=int(f"{sid}/{qid}" in HAND_SIBLING_EVIDENCE),
                _question=question,
                _gold=gold,
                _answer=ans,
            )
        )

    # ---------------- write
    cols = [c for c in rows[0] if not c.startswith("_")]
    with (WT / "evals/analysis/r14_reader_judge_features.csv").open(
        "w", newline="", encoding="utf8"
    ) as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r[c] for c in cols})
    with (OUT_LOCAL / "features_full.jsonl").open("w", encoding="utf8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("rows", len(rows), "wrong", sum(1 - r["correct"] for r in rows))


if __name__ == "__main__":
    main()
