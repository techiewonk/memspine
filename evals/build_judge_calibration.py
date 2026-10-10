"""I13: build the offline judge-calibration set from an existing finished run.

    python evals/build_judge_calibration.py [--run qa-full-qs-eq06-fix] [--runs-dir evals/runs]
        [--out evals/analysis/judge_calibration_dev.jsonl]

The set holds 45 development-conversation items (conv-26 / 30 / 41 / 42 only; never a held-out
conversation) whose answers were read by hand against the gold: correct paraphrases, wrong but
plausible answers, refusals and partial lists. Each row stores the question, gold, the run's answer
text, the HUMAN verdict (CORRECT / PARTIAL / WRONG) with the reason, and the judge verdict the
source run recorded. No model call. Re-running regenerates the same file; the labels live in
``LABELS`` below, so a change to a label is a visible diff.

Verdicts. CORRECT: the answer states the gold fact(s) (a list: every gold item, extras allowed; a
relative date: the date the gold phrase resolves to). PARTIAL: some gold items or an adjacent
fact (a list missing one or more gold items; a coarser answer). WRONG: states something else,
contradicts the gold, or refuses/denies although the gold is a fact. ``borderline`` marks a call a
second reader could make the other way; the agreement script reports it with and without them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEV_ITEMS = ("conv-26", "conv-30", "conv-41", "conv-42")

# key: "item/qid" -> (kind, human verdict, reason, borderline)
LABELS: dict[str, tuple[str, str, str, bool]] = {
    # --- correct paraphrases and equivalent dates -------------------------------------------------
    "conv-26/0-88": ("paraphrase", "CORRECT", "'make a family for kids who need one' restates the gold", False),
    "conv-26/0-105": ("paraphrase", "CORRECT", "self-acceptance, finding support: both gold ideas, plus an extra true one", False),
    "conv-42/3-167": ("paraphrase", "CORRECT", "couch for several people, fluffy, weighted blanket, dimmable lights: all three gold items reworded", False),
    "conv-30/1-29": ("short_list", "CORRECT", "both cities named", False),
    "conv-41/2-7": ("short_list", "CORRECT", "doll and film camera: both items", False),
    "conv-41/2-46": ("date_equivalent", "CORRECT", "'weekend before 22 July' resolved to 15-16 July: the same date", False),
    "conv-42/3-48": ("date_equivalent", "CORRECT", "9 Oct 2022 is a Sunday; the Friday before is 7 Oct: the answer's date", False),
    "conv-42/3-64": ("date_equivalent", "CORRECT", "7 Nov 2022 is a Monday; the Saturday before is 5 Nov: the answer's date", False),
    "conv-30/1-33": ("date_equivalent", "CORRECT", "20 June 2023: same day", False),
    "conv-41/2-142": ("paraphrase", "CORRECT", "a medal is the recognition asked; 'from the volunteers' vs 'for volunteering' is a small qualifier slip", True),
    "conv-42/3-148": ("paraphrase", "CORRECT", "'fun and rewarding' is a positive feeling close to 'happy to share'", True),
    "conv-42/3-82": ("complete_list", "CORRECT", "both gold recipes present among extra true ones", False),
    "conv-26/0-32": ("complete_list", "CORRECT", "support group, school event, pride parade: all three gold items", False),
    # --- partial lists ------------------------------------------------------------------------------
    "conv-42/3-5": ("partial_list", "PARTIAL", "writing, movies, nature present; 'hanging with friends' missing (3 of 4)", False),
    "conv-26/0-15": ("partial_list", "PARTIAL", "pottery, painting, camping present; swimming missing (3 of 4)", False),
    "conv-42/3-67": ("partial_list", "PARTIAL", "dog and turtles named, the count of three turtles is not", False),
    "conv-42/3-75": ("partial_list", "PARTIAL", "2 of 4 desserts", False),
    "conv-41/2-36": ("partial_list", "PARTIAL", "live music event only; the violin concert is missing (1 of 2)", False),
    "conv-41/2-6": ("partial_list", "PARTIAL", "shelter and volunteers named; gym and church missing (1 of 3)", False),
    "conv-42/3-1": ("partial_list", "PARTIAL", "movies shared; desserts missing, video games is not a gold item (1 of 2)", False),
    "conv-42/3-83": ("partial_list", "PARTIAL", "high-score reset only (1 of 3)", False),
    "conv-26/0-2": ("partial_list", "PARTIAL", "counseling present, psychology missing; the answer is the right field", True),
    "conv-41/2-17": ("partial_list", "PARTIAL", "'education or public policy' is adjacent to public administration, not the gold fields", True),
    # --- refusals -------------------------------------------------------------------------------------
    "conv-42/3-62": ("refusal", "WRONG", "refuses; gold is 'Two'", False),
    "conv-42/3-27": ("refusal", "WRONG", "refuses; gold names film contest and festival", False),
    "conv-26/0-22": ("refusal", "WRONG", "'No ... not mentioned'; the gold is Yes (likely, she collects classics)", False),
    "conv-41/2-10": ("refusal", "WRONG", "bare 'Not mentioned.'; gold is a date", False),
    "conv-42/3-92": ("refusal", "WRONG", "refuses; gold is red and purple lighting", False),
    "conv-41/2-45": ("refusal", "WRONG", "refuses a would-question whose gold is No", False),
    "conv-42/3-14": ("refusal", "WRONG", "denies; gold nickname is Jo", False),
    "conv-26/0-71": ("refusal", "WRONG", "says the title is not named; gold is the title", False),
    "conv-26/0-94": ("hedged_refusal", "WRONG", "denies the premise, then states the gold content as Caroline's; as an answer to the question it refuses", True),
    # --- wrong but plausible -------------------------------------------------------------------------
    "conv-41/2-84": ("wrong_plausible", "WRONG", "meal for residents, not the 5K charity run", False),
    "conv-30/1-60": ("wrong_plausible", "WRONG", "'feel most alive' is not 'happy'", False),
    "conv-41/2-63": ("wrong_plausible", "WRONG", "about 3 weeks; the gold is two", False),
    "conv-42/3-68": ("wrong_plausible", "WRONG", "'at least three'; the gold is four", False),
    "conv-42/3-122": ("wrong_plausible", "WRONG", "says he did nothing for her; gold is a stuffed animal", False),
    "conv-26/0-151": ("wrong_plausible", "WRONG", "camping, not a nature walk or hike", False),
    "conv-42/3-113": ("wrong_plausible", "WRONG", "action and sci-fi vs fantasy and sci-fi: one of two items wrong", False),
    "conv-30/1-44": ("wrong_plausible", "WRONG", "'look great and passionate' is not 'graceful'", True),
    "conv-42/3-143": ("wrong_plausible", "WRONG", "'awesome ... power of her words' is not 'touched'", True),
    "conv-26/0-59": ("wrong_plausible", "WRONG", "a flat No; the gold is somewhat religious", False),
    "conv-41/2-64": ("wrong_plausible", "WRONG", "'volunteer at shelters' is not a future job", False),
}


def build(run_dir: Path) -> list[dict]:
    rows: dict[str, dict] = {}
    with (run_dir / "report" / "per_question.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["item"] in DEV_ITEMS:
                rows[f"{r['item']}/{r['qid']}"] = r
    out = []
    for key, (kind, verdict, reason, borderline) in LABELS.items():
        r = rows[key]
        out.append(
            {
                "id": key,
                "item": r["item"],
                "qid": r["qid"],
                "category": r["category"],
                "question": r["question"],
                "gold": r["gold_answer"],
                "answer": r["answer"],
                "kind": kind,
                "human_verdict": verdict,
                "borderline": borderline,
                "reason": reason,
                "source_run": r["run_id"],
                "source_judge_label": "CORRECT" if r["correct"] else "WRONG",
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="qa-full-qs-eq06-fix")
    ap.add_argument("--runs-dir", default=str(Path(__file__).parent / "runs"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "analysis" / "judge_calibration_dev.jsonl"))
    args = ap.parse_args(argv)
    rows = build(Path(args.runs_dir) / f"{args.run}--memspine")
    Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"{len(rows)} items -> {args.out}  {kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
