"""P03 / A04 / A06 offline coverage on traced runs (no model call, no gold at run time).

P03: on an OP-Bench trace, how many probes of each type the assertion detector fires on, against
the I32 detector (``no_record.asserts_past_event``), and the verdict split of the rules classifier.
A04/A06: on a LoCoMo trace, for how many questions an evidence table would be built (list /
count), by contract cardinality, with the size of the extraction prompt. The gold is read only to
say how many of those questions were wrong before (an upper bound on what the table can change).

    PYTHONPATH=src:evals python evals/assertion_table_offline.py \
        evals/runs/full-persp-opb--trace/reads.jsonl evals/runs/full-persp-loc--trace/reads.jsonl.gz
"""

from __future__ import annotations

import sys
from collections import Counter

from duration_offline import load
from memspine_evals.assertion_check import classify_claim, detect_claims, memory_lines
from memspine_evals.evidence_table import _BODY, needs_table, numbered_lines, render_lines
from memspine_evals.no_record import asserts_past_event, event_supported, strip_public_knowledge
from memspine_evals.tokens import HeuristicTokenCounter

from memspine.core.query_contract import build_contract


def p03(rows: list[dict]) -> dict:
    by_type: dict[str, Counter] = {}
    for r in rows:
        qid = r["query_id"]
        kind = qid.split(":")[2] if qid.count(":") >= 3 else "other"
        c = by_type.setdefault(kind, Counter())
        q = r["question"]
        c["probes"] += 1
        old = asserts_past_event(q)
        old_flag = old and not event_supported(q, strip_public_knowledge(r["context_text"]))
        claims = detect_claims(q)
        c["old_detects"] += old
        c["old_note_fires"] += old_flag
        c["new_detects"] += bool(claims)
        if claims:
            lines = memory_lines(r["context_text"])
            labels = {classify_claim(cl.text, lines).label for cl in claims}
            worst = next(x for x in ("contradicted", "unknown", "supported") if x in labels)
            c[f"verdict_{worst}"] += 1
    return {k: dict(v) for k, v in sorted(by_type.items())}


def a04(rows: list[dict]) -> dict:
    counter = HeuristicTokenCounter()
    out: Counter = Counter()
    sizes = []
    for r in rows:
        q = r["question"]
        out["questions"] += 1
        if not needs_table(q):
            continue
        card = build_contract(q).cardinality
        out["table_questions"] += 1
        out[f"cardinality_{card}"] += 1
        lines = numbered_lines(r["context_text"])
        if not lines:
            out["no_lines"] += 1
            continue
        sizes.append(counter.count(_BODY) + counter.count(render_lines(lines)) + counter.count(q))
        if r["verdict"]["score"] < 1:
            out["table_questions_wrong_before"] += 1
    sizes.sort()
    if sizes:
        out["prompt_tokens_median"] = sizes[len(sizes) // 2]
        out["prompt_tokens_max"] = sizes[-1]
        out["prompt_tokens_total"] = sum(sizes)
    return dict(out)


if __name__ == "__main__":
    opb = load(sys.argv[1])
    print(f"{len(opb)} OP-Bench traced reads")
    for kind, counts in p03(opb).items():
        print(kind, counts)
    if len(sys.argv) > 2:
        loc = load(sys.argv[2])
        print(f"{len(loc)} LoCoMo traced reads")
        for key, val in a04(loc).items():
            print(key, val)
