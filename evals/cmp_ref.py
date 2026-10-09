"""Paired comparison of screen runs against the dev reference (date-checked): python cmp_ref.py <run-id> [...]"""
import json
import sys


def res(r):
    out = {}
    for line in open(f"runs/{r}--memspine/results.jsonl", encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") == "result":
            sc = d.get("score_date_checked", d.get("score"))
            ok = float(sc or 0) >= 1 and d["status"] == "completed" and bool((d.get("answer") or "").strip())
            out[(d["item_id"], d["query_id"])] = (ok, d["type_label"])
    return out


def acc(x, c=None):
    sel = [v[0] for v in x.values() if c is None or v[1] == c]
    return 100 * sum(sel) / max(1, len(sel))


ref = res("rs-r0-ref-i2")
for r in sys.argv[1:]:
    b = res(r)
    w = [q[1] for q in ref if b[q][0] and not ref[q][0]]
    lo = [q[1] for q in ref if ref[q][0] and not b[q][0]]
    cats = " ".join(f"{n} {acc(b, c):.1f}/{acc(ref, c):.1f}" for c, n in (("cat4", "single"), ("cat1", "multi"), ("cat2", "temp"), ("cat3", "open")))
    print(f"{r}: {acc(b):.1f} vs {acc(ref):.1f} | {cats} | +{len(w)}/-{len(lo)} gained {w} lost {lo}")
