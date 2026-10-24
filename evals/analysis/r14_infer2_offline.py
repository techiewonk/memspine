"""R14-INFER2 offline CPU check: firing rate of the gate shapes over the local LoCoMo questions.

No model calls. Reads the dataset through ``local_dataset`` (gitignored, never committed) and
writes COUNTS only (no question text, no ids beyond the M-9 id list already in r14_master.json)
to ``r14_infer2_offline.json``.

    python evals/analysis/r14_infer2_offline.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from memspine_evals.local_dataset import load_locomo
from memspine_evals.readers import inference_shape, is_inference_question

M9 = "R14-M-9"


def main() -> None:
    data = load_locomo()
    if data is None:
        raise SystemExit("locomo10.json not on disk (set MEMSPINE_LOCOMO_PATH)")
    master = json.loads(
        (HERE.parent.parent / "docs/v2/data/r14_master.json").read_text(encoding="utf-8")
    )
    m9_ids = next(i["evidence"]["question_ids"] for i in master["items"] if i["id"] == M9)
    per_cat: dict[int, Counter] = {}
    reasons: Counter = Counter()
    m9 = Counter()
    m9_cat: dict[int, Counter] = {}
    for ci, conv in enumerate(data):
        for qi, qa in enumerate(conv["qa"]):
            cat = int(qa["category"])
            q = qa["question"]
            v1 = is_inference_question(q)
            v2, why = inference_shape(q)
            c = per_cat.setdefault(cat, Counter())
            c["n"] += 1
            c["gated_v1"] += v1
            c["gated"] += v2
            c["v2_only"] += v2 and not v1
            c["v1_only"] += v1 and not v2
            if v2:
                reasons[why] += 1
            if f"{ci}-{qi}" in m9_ids:
                m9["n"] += 1
                m9["v1"] += v1
                m9["v2"] += v2
                mc = m9_cat.setdefault(cat, Counter())
                mc["n"] += 1
                mc["v1"] += v1
                mc["v2"] += v2
    out = {
        "item": M9,
        "note": "counts only; firing = the gate would add the commit clause / use the inference retry",
        "per_category": {
            str(k): {
                **dict(v),
                "v1_rate": round(v["gated_v1"] / v["n"], 4),
                "v2_rate": round(v["gated"] / v["n"], 4),
            }
            for k, v in sorted(per_cat.items())
        },
        "m9": {
            "n": m9["n"],
            "v1_fires": m9["v1"],
            "v2_fires": m9["v2"],
            "v1_recall": round(m9["v1"] / m9["n"], 4),
            "v2_recall": round(m9["v2"] / m9["n"], 4),
            "by_category": {str(k): dict(v) for k, v in sorted(m9_cat.items())},
        },
        "v2_fire_reasons": dict(reasons),
    }
    path = HERE / "r14_infer2_offline.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
