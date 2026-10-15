"""R14-INDEXVIEW offline check (CPU, no model call): how many lexical-zero gold turns gain
question-word overlap once the turn is seen through the index view (``write.index_view``)?

Reads the local LoCoMo file (licensed text, never copied) and ``docs/v2/data/r14_master.json`` for the
R14-M-4 (lexical-zero gold, 81) and R14-M-5 (elliptical, 32) question ids; writes COUNTS ONLY to
``evals/analysis/r14_indexview_overlap.json``. Overlap = shared stemmed content words (the same rule as
``r14_reader_judge.py``). A gold turn is lexical-zero when it shares none with the question.

Run:  PYTHONPATH=<worktree>/src python evals/analysis/r14_indexview_overlap.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
DATA = Path("C:/Users/hembad/Documents/GitHub/memspine/data/locomo10.json")
sys.path.insert(0, str(WT / "src"))
from memspine.ingest.index_view import build_index_prefix

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


def toks(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def content(s: str) -> set[str]:
    return {stem(w) for w in toks(s) if w not in STOP and not w.isdigit()}


def session_date(raw: str) -> datetime | None:
    m = re.search(r"(\d{1,2}) (\w+),? (\d{4})", raw)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m[1]} {m[2][:3]} {m[3]}", "%d %b %Y")
    except ValueError:
        return None


def main() -> None:
    master = json.loads((WT / "docs/v2/data/r14_master.json").read_text(encoding="utf-8"))
    sets = {
        it["id"]: set(it["evidence"]["question_ids"])
        for it in master["items"]
        if it["id"] in ("R14-M-4", "R14-M-5")
    }
    convs = json.loads(DATA.read_text(encoding="utf-8"))
    variants = (
        "full",
        "no_prev_turn",
        "no_date_words",
        "date_words_only",
        "prev_turn_only",
        "full_elliptical",
        "prev_turn_only_elliptical",
    )  # the last two use the R14-INDEXVIEW carry rule (R14-IVFIX: the others use "anaphoric")
    tally: dict[str, dict[str, int]] = {}

    def bump(group: str, key: str, n: int = 1) -> None:
        tally.setdefault(group, {})
        tally[group][key] = tally[group].get(key, 0) + n

    for ci, conv in enumerate(convs):
        c = conv["conversation"]
        names = [c["speaker_a"], c["speaker_b"]]
        name_toks = {
            stem(w) for n in names for w in toks(n)
        }  # in every question: not evidence of a match
        turns: dict[str, tuple[str, str, datetime | None, str | None, str | None]] = {}
        for key in c:
            if not re.fullmatch(r"session_\d+", key):
                continue
            when = session_date(c.get(f"{key}_date_time", ""))
            prev_text: str | None = None
            prev_spk: str | None = None
            for t in c[key]:
                turns[t["dia_id"]] = (t["speaker"], t["text"], when, prev_text, prev_spk)
                prev_text, prev_spk = t["text"], t["speaker"]
        for qi, qa in enumerate(conv["qa"]):
            if qa.get("category") == 5:
                continue
            qid = f"{ci}-{qi}"
            q = content(qa["question"]) - name_toks
            groups = ["all"] + [name for name, ids in sets.items() if qid in ids]
            for ev in qa.get("evidence", []):
                for dia in re.findall(r"D\d+:\d+", ev):
                    if dia not in turns:
                        continue
                    spk, text, when, prev_text, prev_spk = turns[dia]
                    if q & (content(text) - name_toks):
                        continue  # not lexical-zero
                    for g in groups:
                        bump(g, "lexical_zero_gold_turns")
                    for v in variants:
                        prefix = build_index_prefix(
                            speaker=""
                            if v
                            in ("date_words_only", "prev_turn_only", "prev_turn_only_elliptical")
                            else spk,
                            stamp=None
                            if v in ("no_date_words", "prev_turn_only", "prev_turn_only_elliptical")
                            else when,
                            content=text,
                            prev_content=None
                            if v in ("no_prev_turn", "date_words_only")
                            else prev_text,
                            prev_speaker=None
                            if v in ("date_words_only", "prev_turn_only")
                            else prev_spk,
                            participants=[]
                            if v
                            in ("date_words_only", "prev_turn_only", "prev_turn_only_elliptical")
                            else names,
                            carry="elliptical" if v.endswith("_elliptical") else "anaphoric",
                        )
                        gained = bool(q & (content(f"{prefix} {text}") - name_toks))
                        for g in groups:
                            if gained:
                                bump(g, f"gain_{v}")
            # question-level: lexical-zero questions whose every zero gold turn is not rescued are not tracked
    out = {
        "note": "counts only; LoCoMo cat 1-4 gold turns with zero stemmed content-word overlap with the question, speaker first names excluded from both sides (they are in every question)",
        "variants": {
            "full": "speaker + session date/month/weekday + resolved relative dates + participants + previous-turn words if anaphoric (write.index_view_carry)",
            "no_prev_turn": "full without the previous-turn words",
            "no_date_words": "full without any date words (undated)",
            "date_words_only": "session date, month, weekday and resolved relative dates only",
            "prev_turn_only": "previous-turn words of anaphoric turns only (R14-IVFIX carry rule)",
            "full_elliptical": "full with the R14-INDEXVIEW elliptical carry rule (screen S69)",
            "prev_turn_only_elliptical": "previous-turn words under the R14-INDEXVIEW elliptical rule only",
        },
        "counts": tally,
    }
    path = WT / "evals/analysis/r14_indexview_overlap.json"
    path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(out["counts"], indent=1))


if __name__ == "__main__":
    main()
