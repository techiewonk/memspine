# ruff: noqa: E501
"""Deterministic root-cause classes for wrong LoCoMo answers (LOCOMO-ATLAS).

    from locomo_problem_classes import classify_row, CLASSES

Pure functions over one ``forensic_question/v1`` row (``forensics_report.py`` output, one question) plus a few
optional side inputs (errata tag, judge-convention credit, non-gold context text). No model call, no network, no
file access: the caller supplies everything, so the rules are unit-testable on synthetic rows.

Two coded fields per wrong question:

* ``stage``  the weakest link, same alphabet as ``analysis/full_persp_loc_stage_labels.txt``:
  ``a`` write path, ``b`` recall (gold in no leg), ``cf`` cut at fusion/pool, ``cr`` cut at rerank_keep,
  ``ca`` cut at assembly, ``d:det`` ``d:lst`` ``d:cnt`` ``d:dar`` ``d:ref`` ``d:inf`` ``d:dis`` ``d:spk`` reader,
  ``e`` judge, ``f`` gold/dataset error. ``ok`` for a correct answer, ``x`` when no gold turn is resolvable.
* ``rclass`` one finer root-cause class from ``CLASSES`` (first matching rule, order in ``classify_row``).

Every class carries ``heuristic``: ``False`` when the rule is a direct test of logged ranks, tags or flags;
``True`` when it reads question/answer wording or token overlap (the rule text is in ``CLASSES[...]['rule']``).
The classes reuse the vocabulary of RECALL_GAPS_FORENSIC.md (causes H N I T O M P), READER_GAPS_FORENSIC.md
(JFN INF GOLD DIST DET DARITH LIST IMG REF HOP DANC) and FULL_RUN_FORENSICS_2026-10-10.md section 2.1 (stage codes).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------------------------------------------------
# Class registry. id -> label, stage group, source in the forensic docs, deterministic rule, heuristic flag.
# ---------------------------------------------------------------------------------------------------------------------
CLASSES: dict[str, dict] = {
    "ok": {"label": "correct", "group": "ok", "doc": "-", "rule": "judge_score == 1", "heuristic": False},
    "gold_error": {
        "label": "Gold or dataset error (errata)",
        "group": "metric",
        "doc": "READER_GAPS_FORENSIC.md s5; analysis/locomo_errata.json",
        "rule": "(item, qid) carries a default-excluded errata tag: gold_error, needs_image or premise_error",
        "heuristic": False,
    },
    "judge_false_negative": {
        "label": "Judge false negative (answer states the gold fact)",
        "group": "metric",
        "doc": "READER_GAPS_FORENSIC.md s4.1 (JFN); FULL_RUN_FORENSICS s2.3(e)",
        "rule": "wrong verdict but judge conventions credit it (list_superset, typo) or every content word of the gold sits in the answer (answer_contains_gold)",
        "heuristic": True,
    },
    "relative_date_gold_vs_judge": {
        "label": "Relative-date or range gold against an absolute-date answer (judge/format)",
        "group": "metric",
        "doc": "FULL_RUN_FORENSICS s2.3(e), s4.1; GAP_REGISTER I77",
        "rule": "wrong verdict, date question, and the answer date overlaps the gold interval at another granularity, or the date guard says equivalent, or the gold is a relative phrase ('the week before D', 'N years ago') that the answer resolves",
        "heuristic": True,
    },
    "write_path_loss": {
        "label": "Gold turn not stored or altered at write",
        "group": "write",
        "doc": "FULL_RUN_FORENSICS s2.3(a)",
        "rule": "primary_gap == ingest",
        "heuristic": False,
    },
    "open_domain_inference_retrieval": {
        "label": "Open-domain: evidence is an implication, recall/cut",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC cause I",
        "rule": "category open-domain and stage in b/cf/cr",
        "heuristic": False,
    },
    "temporal_evidence_missing": {
        "label": "Temporal: date-dependent gold turn not retrieved",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC cause T; FULL_RUN_FORENSICS s4.1",
        "rule": "category temporal and stage in b/cf/cr",
        "heuristic": False,
    },
    "rerank_keep_cut": {
        "label": "Gold in the reranker pool, cut by rerank_keep",
        "group": "retrieval",
        "doc": "FULL_RUN_FORENSICS s2.3(c)",
        "rule": "stage cr (gold in the reranker pool, not in the final list)",
        "heuristic": False,
    },
    "list_aggregation_generic": {
        "label": "List / aggregation question with a generic category (category-to-instance gap)",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC cause H; B1_LIST_QUESTIONS_DESIGN.md s1",
        "rule": "stage b/cf, >= 2 gold turns, and the question is list/aggregation shaped (what/which/who + plural set noun, 'what kind of', 'how many', or a comma/and-list gold)",
        "heuristic": True,
    },
    "implicit_or_paraphrased_gold": {
        "label": "Answer not literally in the gold turn (implicit, paraphrased, noisy label)",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC cause N",
        "rule": "stage b/cf, non-temporal non-open, and no content word of the gold answer occurs in any gold turn or its caption",
        "heuristic": True,
    },
    "fusion_loss_vector_only": {
        "label": "Fusion loss: gold found by the vector leg only",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC s2.5, s3 (195 of 237 fusion-lost turns vector-only); FULL_RUN_FORENSICS s2.3(c)",
        "rule": "stage cf and every lost gold turn is ranked in the vector leg only (no lexical, no extra leg)",
        "heuristic": False,
    },
    "fusion_loss_lexical_only": {
        "label": "Fusion loss: gold found by BM25 / an extra leg only",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC s3; FULL_RUN_FORENSICS s3.3 (perspective leg crowds BM25-only gold)",
        "rule": "stage cf and every lost gold turn is ranked in the lexical or an extra leg and not in the vector leg",
        "heuristic": False,
    },
    "fusion_loss_two_leg_crowded": {
        "label": "Fusion loss: gold in both legs but outranked at the fused pool",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC cause O, s2.5",
        "rule": "stage cf, a lost gold turn sits in the vector and the lexical leg",
        "heuristic": False,
    },
    "multi_evidence_recall": {
        "label": "Recall: many gold turns across sessions, at least one in no leg",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC s2.2 tags (4+ gold turns lift 2.24); s4",
        "rule": "stage b and >= 3 gold turns (or gold spans >= 3 sessions)",
        "heuristic": False,
    },
    "vocabulary_mismatch_recall": {
        "label": "Recall: no stemmed content-word overlap between question and gold turn",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC s2.2 (lift 2.02), causes H/P",
        "rule": "stage b and the lost gold turn shares no stemmed content word with the question",
        "heuristic": True,
    },
    "lexical_outranked_recall": {
        "label": "Recall/cut: overlap exists but the turn is outranked (BM25 noise, morphology)",
        "group": "retrieval",
        "doc": "RECALL_GAPS_FORENSIC causes O, M, P",
        "rule": "remaining b/cf rows",
        "heuristic": True,
    },
    "date_duration_arithmetic": {
        "label": "Reader: duration / elapsed-time / two-event arithmetic",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC DARITH; FULL_RUN_FORENSICS s4.1; GAP_REGISTER I76",
        "rule": "reader stage and the question asks how long / how many days|weeks|months|years (or bucket date_arithmetic)",
        "heuristic": True,
    },
    "date_offset_small": {
        "label": "Reader: right event, date off by 1 to 7 days (weekday / 'last week' arithmetic)",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC DANC/DARITH; GAP_REGISTER I79",
        "rule": "reader stage, bucket wrong_absolute_date with a gap of at most 7 days",
        "heuristic": False,
    },
    "date_wrong_event": {
        "label": "Reader: date of a different event, or a day invented in a coarse range",
        "group": "reader",
        "doc": "FULL_RUN_FORENSICS s4.1 (wrong event matched, session date as event date); READER_GAPS_FORENSIC DIST",
        "rule": "reader stage, bucket wrong_absolute_date with a gap above 7 days, or no date in the answer",
        "heuristic": False,
    },
    "refusal_premise_denial": {
        "label": "Reader: refusal or premise denial despite evidence in context",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC REF; FULL_RUN_FORENSICS s2.3(d)",
        "rule": "reader stage and bucket refusal",
        "heuristic": False,
    },
    "inference_open_domain": {
        "label": "Reader: open-domain inference or world-knowledge step",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC INF; FULL_RUN_FORENSICS s2.3(d)",
        "rule": "reader stage and category open-domain",
        "heuristic": False,
    },
    "list_incomplete": {
        "label": "Reader: incomplete or over-inclusive list",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC LIST; B1_LIST_QUESTIONS_DESIGN.md",
        "rule": "reader stage and bucket list_missing_item / list_extra_item",
        "heuristic": False,
    },
    "count_wrong": {
        "label": "Reader: wrong count",
        "group": "reader",
        "doc": "FULL_RUN_FORENSICS s2.1 (d:cnt); GAP_REGISTER I56",
        "rule": "reader stage and the question starts 'how many' (not a time-unit duration)",
        "heuristic": True,
    },
    "image_caption_misuse": {
        "label": "Reader: answer sits in an image caption the reader ignored or mixed",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC IMG",
        "rule": "reader stage and the gold answer tokens occur only inside '[image: ...]' captions of the gold turns",
        "heuristic": True,
    },
    "speaker_confusion": {
        "label": "Reader: answer taken from the other speaker's turn",
        "group": "reader",
        "doc": "FULL_RUN_FORENSICS s2.1 (d:spk); perspective policy",
        "rule": "reader stage, the question names exactly one of the two speakers and the answer names only the other one",
        "heuristic": True,
    },
    "distractor_line": {
        "label": "Reader: answer follows a non-gold line in the context",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC DIST",
        "rule": "reader stage and >= 60% of the answer content words occur in a non-gold context line and in no gold turn",
        "heuristic": True,
    },
    "wrong_detail": {
        "label": "Reader: right line, wrong or vague detail",
        "group": "reader",
        "doc": "READER_GAPS_FORENSIC DET; FULL_RUN_FORENSICS s2.1 (d:det)",
        "rule": "remaining reader rows",
        "heuristic": True,
    },
    "no_gold_resolvable": {
        "label": "No gold turn resolvable (malformed evidence id)",
        "group": "metric",
        "doc": "analysis/locomo_errata.json bad_evidence_id",
        "rule": "wrong, no usable gold turn, no primary gap",
        "heuristic": False,
    },
}

STAGE_NAME = {
    "ok": "correct",
    "a": "(a) write path",
    "b": "(b) recall: gold in no leg",
    "cf": "(c) cut at fusion / pool",
    "cr": "(c) cut at rerank_keep",
    "ca": "(c) cut at assembly",
    "d:det": "(d) reader: wrong detail",
    "d:lst": "(d) reader: incomplete list",
    "d:cnt": "(d) reader: wrong count",
    "d:dar": "(d) reader: date / duration",
    "d:ref": "(d) reader: refusal",
    "d:inf": "(d) reader: inference",
    "d:dis": "(d) reader: distractor",
    "d:spk": "(d) reader: wrong speaker",
    "e": "(e) judge / metric",
    "f": "(f) gold error",
    "x": "unresolved",
}

# class -> hand-code alphabet of full_persp_loc_stage_labels.txt (used by the validation against the hand labels)
STAGE_OF_CLASS = {
    "date_duration_arithmetic": "d:dar",
    "date_offset_small": "d:dar",
    "date_wrong_event": "d:dar",
    "refusal_premise_denial": "d:ref",
    "inference_open_domain": "d:inf",
    "list_incomplete": "d:lst",
    "count_wrong": "d:cnt",
    "image_caption_misuse": "d:det",
    "speaker_confusion": "d:spk",
    "distractor_line": "d:dis",
    "wrong_detail": "d:det",
}

CAT_NAME = {"cat1": "multi-hop", "cat2": "temporal", "cat3": "open-domain", "cat4": "single-hop", "cat5": "adversarial"}

# ---------------------------------------------------------------------------------------------------------------------
# Text features
# ---------------------------------------------------------------------------------------------------------------------
_STOP = frozenset(
    ["a", "an", "the", "of", "to", "in", "on", "at", "for", "and", "or", "but", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "has", "have", "had", "will", "would", "can", "could", "should", "may", "might", "must", "i", "you", "he", "she", "it", "we", "they", "them", "his", "her", "its", "their", "my", "your", "our", "me", "him", "us", "this", "that", "these", "those", "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "with", "from", "by", "as", "about", "into", "than", "then", "so", "not", "no", "yes", "if", "just", "also", "very", "there", "here", "up", "out", "over", "after", "before", "again", "more", "most", "some", "any", "each", "both", "other", "such", "only", "own", "same", "too", "s", "t"]
)
_WORD = re.compile(r"[a-z0-9]+")
_CAPTION = re.compile(r"\[image:[^\]]*\]", re.I)
_DUR = re.compile(r"\bhow (long|many (days?|weeks?|months?|years?|hours?|minutes?))\b|\bhow much time\b|\bhow old\b", re.I)
_HOWMANY = re.compile(r"^\s*how many\b", re.I)
_LIST_NOUN = re.compile(
    r"\b(activities|events|hobbies|books|items|places|things|kinds|types|games|movies|films|songs|bands|artists|pets|"
    r"foods|dishes|sports|instruments|countries|cities|states|friends|members|projects|ways|reasons|goals|"
    r"plans|jobs|classes|topics|shows|trips|gifts|dances|styles|colors|skills|issues|traits|qualities)\b",
    re.I,
)
_LIST_Q = re.compile(r"^\s*(what|which|who)\b", re.I)
_KIND_OF = re.compile(r"\bwhat (kind|kinds|type|types|sort|sorts) of\b", re.I)
_WHAT_DO = re.compile(r"^\s*what (do|does|did|has|have|is|are)\b.*\b(do|like|enjoy|attend|play|read|watch|visit|collect|own|use|make|paint|buy|eat|participate|partake|practice|study)\b", re.I)
_REL_GOLD = re.compile(
    r"\b(week|weekend|weekday|month|year|years|months|days|day)\b.*\b(before|after|ago|earlier|later|prior|following)\b|"
    r"\b(before|after|around|since|last|next|early|late|mid)\b.*\b(\d{4}|january|february|march|april|may|june|july|august|"
    r"september|october|november|december|week|weekend|month|year)\b|\b\d+ (years?|months?|weeks?|days?) ago\b",
    re.I,
)
_PURE_AGO = re.compile(r"^\s*(about\s+|around\s+)?(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(year|month|week|day)s?\s+ago\s*\.?\s*$", re.I)
_ABS_DATE = re.compile(r"\b(19|20)\d{2}\b|\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I)
_NEG = re.compile(r"\b(not|never|no|none|cannot|can't|didn't|doesn't|wasn't|isn't|neither)\b|n't", re.I)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")


def stem(w: str) -> str:
    """Tiny suffix stripper (no dependency): enough to tell vocabulary overlap from morphology-only overlap."""
    for suf in ("ingly", "ing", "edly", "ed", "es", "ly", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def content_words(text: str, drop: frozenset = frozenset()) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and w not in drop and len(w) > 1]


def asks_duration(q: str) -> bool:
    return bool(_DUR.search(q or ""))


def asks_count(q: str) -> bool:
    return bool(_HOWMANY.search(q or "")) and not asks_duration(q)


def gold_is_list(gold: str) -> bool:
    g = (gold or "").strip()
    if not g:
        return False
    parts = [p for p in re.split(r",|;|\band\b", g) if p.strip()]
    return len(parts) >= 2 and all(len(p.split()) <= 5 for p in parts)


def is_list_shaped(question: str, gold: str) -> bool:
    q = question or ""
    if asks_duration(q):
        return False
    if asks_count(q):
        return True
    if _KIND_OF.search(q):
        return True
    if _LIST_Q.search(q) and _LIST_NOUN.search(q):
        return True
    if _WHAT_DO.search(q) and gold_is_list(gold):
        return True
    return gold_is_list(gold) and bool(_LIST_Q.search(q))


def relative_gold(gold: str) -> bool:
    """True when the gold is a relative or range date phrase rather than one calendar day."""
    g = (gold or "").lower()
    if not g:
        return False
    if re.search(r"\b\d{1,2}\s+(" + "|".join(_MONTHS) + r")\s+\d{4}\b|\b(" + "|".join(_MONTHS) + r")\s+\d{1,2},?\s+\d{4}\b", g) and not _REL_GOLD.search(g.split(",")[0]):
        return False
    return bool(_REL_GOLD.search(g)) or bool(re.fullmatch(r"\s*(in\s+)?(" + "|".join(_MONTHS) + r")?\s*\d{4}\s*", g)) or bool(re.search(r"\bago\b", g))


def named_speakers(question: str, names: tuple[str, ...]) -> list[str]:
    q = (question or "").lower()
    return [n for n in names if n and re.search(r"\b" + re.escape(n.lower()) + r"(?:'s)?\b", q)]


def _parse_gap_days(detail: str | None) -> int | None:
    """``reader_bucket['detail']`` looks like 'off by 1 d' / 'off by 3 d' / 'gap 400 d'; None when absent."""
    if not detail:
        return None
    m = re.search(r"(\d+)\s*d\b", detail)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------------------------------------------------
# Row classifier
# ---------------------------------------------------------------------------------------------------------------------
def _lost_turns(row: dict) -> list[dict]:
    return [g for g in row.get("gold_turns") or [] if g.get("lost_at") in ("recall", "fusion", "rerank", "assembly", "ingest")]


def leg_set(ranks: dict) -> set[str]:
    out = set()
    if ranks.get("vector") is not None:
        out.add("vector")
    if ranks.get("lexical") is not None:
        out.add("lexical")
    for k, v in (ranks.get("extra") or {}).items():
        if v is not None:
            out.add("extra:" + k)
    return out


def best_leg_rank(ranks: dict) -> int | None:
    vals = [ranks.get("vector"), ranks.get("lexical"), *(ranks.get("extra") or {}).values()]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def sessions_of(row: dict) -> list[int]:
    out = []
    for g in row.get("gold_turns") or []:
        m = re.match(r"D(\d+):", g.get("turn") or "")
        if m and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return sorted(out)


def classify_row(
    row: dict,
    *,
    errata_tag: str | None = None,
    convention_credit: str | None = None,
    date_guard_equivalent: bool = False,
    non_gold_context: list[str] | None = None,
    speakers: tuple[str, ...] = (),
) -> dict:
    """Return ``{'stage', 'rclass', 'tags', 'heuristic', 'rule'}`` for one forensic row.

    ``errata_tag``: tag from locomo_errata.json for this (item, qid), or None.
    ``convention_credit``: 'list_superset' / 'typo' when ``check_conventions`` credits the answer, else None.
    ``date_guard_equivalent``: the deterministic date guard (``date_equivalent``) says answer == gold.
    ``non_gold_context``: texts of context lines that are not gold turns (distractor test), may be empty.
    ``speakers``: the two speaker names of the conversation (speaker-confusion test).
    """
    tags: list[str] = []
    if row.get("correct"):
        return _out("ok", "ok", tags)
    cat = row.get("category") or ""
    question, gold, answer = row.get("question") or "", row.get("gold_answer") or "", row.get("answer") or ""
    gts = row.get("gold_turns") or []
    gap = row.get("primary_gap")
    bucket = (row.get("reader_bucket") or {}).get("bucket")
    gap_days = _parse_gap_days((row.get("reader_bucket") or {}).get("detail"))
    nsess = len(sessions_of(row))
    lost = _lost_turns(row)
    if errata_tag:
        tags.append("errata:" + errata_tag)

    # 1. metric-side classes first: they say the system may not be at fault
    if errata_tag in ("gold_error", "needs_image", "premise_error"):
        return _out("f", "gold_error", tags)
    if not gts and gap is None:
        return _out("x", "no_gold_resolvable", tags)

    if convention_credit:
        tags.append("convention:" + convention_credit)
        return _out("e", "judge_false_negative", tags)
    reader_side = gap in (None, "reader") and not lost
    if reader_side or gap == "reader":
        date_q = bucket in ("wrong_granularity", "date_matches_gold") or date_guard_equivalent
        if date_q:
            return _out("e", "relative_date_gold_vs_judge", tags)
        if _PURE_AGO.match(gold) and _ABS_DATE.search(answer) and not _NEG.search(answer):
            tags.append("relative_year_gold")
            return _out("e", "relative_date_gold_vs_judge", tags)
        gw = set(stem(w) for w in content_words(gold))
        aw = set(stem(w) for w in content_words(answer))
        if (
            gw
            and gw <= aw
            and bucket not in ("refusal", "date_arithmetic")
            and not _NEG.search(answer)
            and (len(gw) >= 2 or len(answer.split()) <= 25)
        ):
            tags.append("answer_contains_gold")
            return _out("e", "judge_false_negative", tags)

    # 2. retrieval-side stages
    if gap == "ingest":
        return _out("a", "write_path_loss", tags)
    if gap in ("recall", "fusion", "rerank", "assembly"):
        stage = {"recall": "b", "fusion": "cf", "rerank": "cr", "assembly": "ca"}[gap]
        if any(g.get("lost_at") == "fusion" for g in lost) and gap == "recall":
            tags.append("also_fusion_lost")
        if any((best_leg_rank(g.get("ranks") or {}) or 999) <= 10 and g.get("lost_at") == "fusion" for g in lost):
            tags.append("leg_top10_cut")
        if nsess >= 3:
            tags.append("spans_3plus_sessions")
        if len(gts) >= 3:
            tags.append("3plus_gold_turns")
        if cat == "open-domain":
            return _out(stage, "open_domain_inference_retrieval", tags)
        if cat == "temporal":
            return _out(stage, "temporal_evidence_missing", tags)
        if stage == "cr":
            return _out(stage, "rerank_keep_cut", tags)
        if len(gts) >= 2 and is_list_shaped(question, gold):
            return _out(stage, "list_aggregation_generic", tags)
        # implicit: gold answer words absent from every gold turn (caption included)
        aw = set(stem(w) for w in content_words(gold))
        gtext = " ".join((g.get("text") or "") for g in gts)
        gset = set(stem(w) for w in content_words(gtext))
        if aw and not (aw & gset) and not asks_duration(question):
            return _out(stage, "implicit_or_paraphrased_gold", tags)
        if stage == "cf":
            legsets = [leg_set(g.get("ranks") or {}) for g in lost if g.get("lost_at") == "fusion"]
            if legsets and all(s == {"vector"} for s in legsets):
                return _out(stage, "fusion_loss_vector_only", tags)
            if legsets and all("vector" not in s for s in legsets):
                return _out(stage, "fusion_loss_lexical_only", tags)
            if any({"vector", "lexical"} <= s for s in legsets):
                return _out(stage, "fusion_loss_two_leg_crowded", tags)
            return _out(stage, "fusion_loss_vector_only" if any("vector" in s for s in legsets) else "fusion_loss_lexical_only", tags)
        # recall
        if len(gts) >= 3 or nsess >= 3:
            return _out(stage, "multi_evidence_recall", tags)
        qset = set(stem(w) for w in content_words(question, frozenset(n.lower() for n in speakers)))
        miss = [g for g in lost if g.get("lost_at") == "recall"] or lost
        if miss and all(not (qset & set(stem(w) for w in content_words(g.get("text") or ""))) for g in miss):
            return _out(stage, "vocabulary_mismatch_recall", tags)
        return _out(stage, "lexical_outranked_recall", tags)

    # 3. reader stage (every gold turn reached the context)
    if cat == "open-domain":
        return _out("d:inf", "inference_open_domain", tags)
    if bucket == "refusal":
        return _out("d:ref", "refusal_premise_denial", tags)
    if asks_duration(question) or bucket == "date_arithmetic":
        return _out("d:dar", "date_duration_arithmetic", tags)
    if bucket in ("wrong_absolute_date", "no_date_in_answer"):
        if bucket == "wrong_absolute_date" and gap_days is not None and gap_days <= 7:
            return _out("d:dar", "date_offset_small", tags)
        return _out("d:dar", "date_wrong_event", tags)
    if bucket in ("list_missing_item", "list_extra_item"):
        return _out("d:lst", "list_incomplete", tags)
    if asks_count(question):
        return _out("d:cnt", "count_wrong", tags)
    # caption-only gold
    ans_tok = set(stem(w) for w in content_words(gold))
    caps = " ".join(" ".join(_CAPTION.findall(g.get("text") or "")) for g in gts)
    plain = " ".join(_CAPTION.sub(" ", g.get("text") or "") for g in gts)
    if ans_tok and (ans_tok & set(stem(w) for w in content_words(caps))) and not (ans_tok & set(stem(w) for w in content_words(plain))):
        return _out("d:det", "image_caption_misuse", tags)
    names = [n for n in speakers if n]
    if len(names) == 2:
        qn = named_speakers(question, tuple(names))
        an = named_speakers(answer, tuple(names))
        if len(qn) == 1 and an and qn[0] not in an:
            return _out("d:spk", "speaker_confusion", tags)
    if non_gold_context and answer:
        atoks = set(stem(w) for w in content_words(answer))
        gtoks = set(stem(w) for w in content_words(" ".join((g.get("text") or "") for g in gts)))
        only = atoks - gtoks - set(stem(w) for w in content_words(question))
        if len(atoks) >= 3 and only:
            for t in non_gold_context:
                ttoks = set(stem(w) for w in content_words(t))
                if len(only & ttoks) / max(1, len(atoks)) >= 0.6:
                    return _out("d:dis", "distractor_line", tags)
    return _out("d:det", "wrong_detail", tags)


def _out(stage: str, rclass: str, tags: list[str]) -> dict:
    c = CLASSES[rclass]
    return {"stage": stage, "rclass": rclass, "tags": tags, "heuristic": c["heuristic"], "rule": c["rule"]}
