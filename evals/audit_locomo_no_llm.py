"""Offline feasibility inventory and lexical retrieval probe; never answer accuracy.

Gold/evidence are inspected only AFTER question-only BM25 ranking. No engine imports.
Dataset-bearing outputs must remain in the local ignored runs directory.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

STOP = set(
    [
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "did",
        "does",
        "do",
        "has",
        "have",
        "had",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "from",
        "and",
        "or",
        "with",
        "what",
        "which",
        "who",
        "when",
        "where",
        "how",
        "would",
        "could",
        "likely",
        "be",
        "considered",
        "both",
    ]
)
CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}
OPERATORS = {
    "temporal_anchor": (
        r"\b(when|what year|what month|what date|how long|how many years|before|after)\b",
        "Source-local date parser; preserve precision; match the event before computing dates.",
    ),
    "duration": (
        r"\b(how long|how many (?:years|months|days)|time.*take)\b",
        "Join supported start/end of the same event; interval arithmetic; unresolved if an endpoint is unknown.",
    ),
    "entity_intersection": (
        r"\b(both|in common|same)\b",
        "Resolve subjects, then intersect validated predicate/value sets; never fuzzy-merge people to match gold.",
    ),
    "enumeration": (
        r"\b(what (?:activities|people|books|places|things|types|kinds)|which (?:activities|people|books|places)|list|name all)\b",
        "Collect source-bound members, deduplicate by identity/event and retain provenance; completeness requires scoped coverage.",
    ),
    "count": (
        r"\b(how many|number of)\b",
        "Count validated distinct entities/events rather than mentions; reject incomplete candidate coverage.",
    ),
    "location": (
        r"\b(where|city|country|state|move|moved|live|living|visited)\b",
        "Separate residence, origin, destination, venue and visit; require explicit place disambiguation.",
    ),
    "causal": (
        r"\b(why|reason|inspire|motivated)\b",
        "Extract explicit causal evidence only; sequence and lexical similarity cannot establish motive.",
    ),
    "preference_inference": (
        r"\b(likely|would|might|could|considered)\b",
        "Retrieve supporting preferences/traits; deterministic answer needs a validated external rule or explicit evidence, otherwise unresolved.",
    ),
    "identity_relation": (
        r"\b(who|whose|family|husband|wife|friend|sister|brother)\b",
        "Bind sender/subject and explicit aliases/kinship; preserve reported source and ambiguity.",
    ),
}


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def normalized(text: object) -> str:
    return " ".join(tokens(str(text)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw = args.dataset.read_bytes()
    data = json.loads(raw)
    errata_path = Path(__file__).parent / "analysis/locomo_errata.json"
    errata = defaultdict(list)
    if errata_path.exists():
        for e in json.loads(errata_path.read_text(encoding="utf-8"))["entries"]:
            errata[(e["item"], e["qid"])].append(e)
    rows = []
    aggregate = Counter()
    by_category = defaultdict(Counter)
    for sample_index, sample in enumerate(data):
        conversation = sample["conversation"]
        turns = {}
        for key, values in conversation.items():
            if re.fullmatch(r"session_\d+", key) and isinstance(values, list):
                for turn in values:
                    turns[turn["dia_id"]] = dict(
                        turn, source_time=conversation.get(key + "_date_time")
                    )
        postings = defaultdict(list)
        lengths = {}
        for ident, turn in turns.items():
            # Captions, summaries, observations and gold are deliberately excluded.
            counts = Counter(tokens(turn["speaker"] + " " + turn["text"]))
            lengths[ident] = sum(counts.values())
            for word, count in counts.items():
                postings[word].append((ident, count))
        n = len(turns)
        avg = sum(lengths.values()) / max(n, 1)
        for qi, qa in enumerate(sample["qa"]):
            question = qa["question"]
            scores = defaultdict(float)
            for word in set(tokens(question)) - STOP:
                hits = postings.get(word, [])
                idf = math.log(1 + (n - len(hits) + 0.5) / (len(hits) + 0.5))
                for ident, tf in hits:
                    scores[ident] += (
                        idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[ident] / avg))
                    )
            ranking = sorted(scores, key=lambda x: (-scores[x], x))
            refs = list(
                dict.fromkeys(
                    "D" + str(int(a)) + ":" + str(int(b))
                    for part in qa.get("evidence", [])
                    for a, b in re.findall(r"D:?(\d+):(\d+)", part)
                )
            )
            valid = [ident for ident in refs if ident in turns]
            support = [dict(turns[ident], source_id=ident) for ident in valid]
            tags = [
                tag for tag, (pattern, _) in OPERATORS.items() if re.search(pattern, question, re.I)
            ]
            if not tags:
                tags = ["explicit_detail_or_semantic_extraction"]
            caption = any(t.get("img_url") or t.get("blip_caption") for t in support)
            gold = qa.get("answer")
            gold_norm = normalized(gold) if gold is not None else ""
            literal = bool(gold_norm) and any(gold_norm in normalized(t["text"]) for t in support)
            # A lexical diagnostic, not answerability: long/short gold and paraphrases bias this.
            review = errata[(sample["sample_id"], f"{sample_index}-{qi}")]
            if qa["category"] == 5:
                route = "abstention_policy_review"
            elif qa["category"] == 3:
                route = "external_knowledge_or_semantic_inference_review"
            elif not valid:
                route = "missing_labelled_evidence_review"
            elif review:
                route = "existing_errata_review"
            elif literal:
                route = "literal_span_candidate_not_verified_answer"
            elif "temporal_anchor" in tags or qa["category"] == 2:
                route = "temporal_rules_candidate_needs_event_binding"
            else:
                route = "structured_extraction_or_paraphrase_review"
            metrics = {}
            for k in (5, 10, 20):
                retrieved = set(ranking[:k])
                metrics[f"evidence_recall_at_{k}"] = (
                    len(retrieved.intersection(valid)) / len(valid) if valid else None
                )
                metrics[f"all_evidence_at_{k}"] = bool(valid) and set(valid).issubset(retrieved)
            row = dict(
                sample_id=sample["sample_id"],
                qid=f"{sample_index}-{qi}",
                category=CATEGORIES[qa["category"]],
                question=question,
                gold=gold,
                adversarial_distractor=qa.get("adversarial_answer"),
                evidence_ids_raw=qa.get("evidence", []),
                evidence_ids=refs,
                missing_ids=[x for x in refs if x not in turns],
                evidence=support,
                operator_hypotheses=tags,
                proposed_steps=[
                    OPERATORS[t][1]
                    if t in OPERATORS
                    else "Retrieve source and identify an explicit subject/predicate/value; unmatched semantic forms remain unresolved."
                    for t in tags
                ],
                review_route=route,
                literal_gold_in_cited_raw=literal,
                cited_turn_has_media_or_caption=caption,
                existing_errata=review,
                bm25_top20=[dict(source_id=x, score=round(scores[x], 6)) for x in ranking[:20]],
                **metrics,
                validation="automated heuristic inventory; semantic review and solver validation pending",
            )
            rows.append(row)
            aggregate[route] += 1
            by_category[row["category"]]["questions"] += 1
            if valid and qa["category"] != 5:
                by_category[row["category"]]["evidence_evaluable"] += 1
                for k in (5, 10, 20):
                    by_category[row["category"]][f"all_at_{k}"] += metrics[f"all_evidence_at_{k}"]
                    by_category[row["category"]][f"recall_sum_at_{k}"] += metrics[
                        f"evidence_recall_at_{k}"
                    ]
    args.out.mkdir(parents=True, exist_ok=True)
    summary = dict(
        dataset_sha256=hashlib.sha256(raw).hexdigest(),
        total_questions=len(rows),
        routes=dict(aggregate),
        by_category=dict(by_category),
        limitations=[
            "No non-LLM answer solver was run; these are not accuracy or maximum-achievable estimates.",
            "Each question ranked the full matching conversation; no as-of query cutoff was available/applied.",
            "Evidence labels can be incomplete/wrong; category 5 is not automatically unanswerable.",
            "Regex operators are hypotheses and can miss or mislabel requirements.",
            "Gold used only for offline literal-span diagnostic after retrieval; never a runtime extraction rule.",
            "Raw-text BM25 only: no neighbor expansion, captions, stemming, semantic model or named-entity oracle.",
        ],
    )
    (args.out / "questions.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    def esc(value: object) -> str:
        return html.escape(str(value))

    table = []
    for row in rows:
        evidence = "\n\n".join(
            f"{t['source_id']} | {t['source_time']} | {t['speaker']}\n{t['text']}\nCaption (separate provenance): {t.get('blip_caption', '')}"
            for t in row["evidence"]
        )
        cells = [
            row["sample_id"] + "/" + row["qid"],
            row["category"],
            row["question"],
            row["gold"],
            row["review_route"],
            ", ".join(row["operator_hypotheses"]),
            "\n".join(row["proposed_steps"]),
            str(row["all_evidence_at_20"])
            + "\n"
            + ", ".join(x["source_id"] for x in row["bm25_top20"]),
            json.dumps(row["existing_errata"]),
            evidence,
        ]
        table.append(
            "<tr>"
            + "".join("<td>" + esc(c).replace("\n", "<br>") + "</td>" for c in cells)
            + "</tr>"
        )
    page = """<!doctype html><meta charset="utf-8"><title>LoCoMo non-LLM feasibility inventory</title>
<style>body{font:15px system-ui;margin:20px;background:#fafafa;color:#17212b}header{position:sticky;top:0;background:#fafafa;padding:10px;z-index:2}input{width:65%;padding:10px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd3da;padding:9px;vertical-align:top;min-width:140px}td:last-child{min-width:460px}th{background:#e8eef4}pre{white-space:pre-wrap}button{padding:10px}</style>
<header><h1>All-question non-LLM feasibility inventory</h1><p>Automated hypotheses, not verified answerability. No answer solver, LLM call or accuracy estimate.</p><input id="q" placeholder="Filter by question, operator, source ID or erratum"><button onclick="document.documentElement.requestFullscreen()">Full screen</button><span id="count"></span></header>"""
    page += (
        "<details><summary>Method and measured retrieval results</summary><pre>"
        + esc(json.dumps(summary, indent=2))
        + "</pre></details>"
    )
    page += (
        '<div style="overflow:auto"><table><thead><tr>'
        + "".join(
            "<th>" + c + "</th>"
            for c in [
                "ID",
                "Category",
                "Question",
                "Gold (offline only)",
                "Review route",
                "Operator hypotheses",
                "Proposed engineering",
                "BM25 all evidence@20 / ranked IDs",
                "Known errata",
                "Cited raw evidence / time / caption",
            ]
        )
        + "</tr></thead><tbody>"
        + "".join(table)
        + "</tbody></table></div>"
    )
    page += '<script>const rows=[...document.querySelectorAll("tbody tr")];q.oninput=()=>{let n=0;const v=q.value.toLowerCase();rows.forEach(r=>{r.hidden=!r.textContent.toLowerCase().includes(v);if(!r.hidden)n++});document.getElementById("count").textContent=` ${n} / ${rows.length}`};q.oninput();</script>'
    (args.out / "index.html").write_text(page, encoding="utf-8")
    assert len(rows) == sum(len(s["qa"]) for s in data)
    assert len({(r["sample_id"], r["qid"]) for r in rows}) == len(rows)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
