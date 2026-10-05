"""G24 (#89): offline agreement of the GLiNER2 read planner with rule-derived mode labels.

Pre-registration: ``evals/prereg/G24_gliner2_planner.md``. Read it before changing anything
here; the labelling rules, the split, the metric and the decision rule are fixed there.

Two subcommands:

``build``
    Labels every LoCoMo-10 question with a read mode by the deterministic rules in
    :func:`label` (the engine's own ``core/query_shape`` predicates plus a replay cue taken
    from the documented intent of the ``plan`` prompt), draws a stratified sample and splits
    it into a ``tune`` and a ``heldout`` half. The result is the frozen fixture
    ``evals/prereg/G24_gliner2_planner_set.json``. Run once; never re-run after results.

``run``
    Classifies the fixture's questions with every setup in :data:`SETUPS` (a GLiNER2
    checkpoint, local CPU) and the rule baselines, and writes accuracy, per-mode recall and
    confusion matrices per split. ``--split tune`` is the tuning loop; the held-out half is
    read once, for the final table.

Usage::

    python evals/gliner2_planner_eval.py build --locomo evals/data/locomo10.json
    python evals/gliner2_planner_eval.py run --split tune
    python evals/gliner2_planner_eval.py run --split both \
        --out evals/prereg/G24_gliner2_planner_results.md

No paid or remote call is made; the only network use is a Hugging Face model download if
the checkpoint is not cached (set ``HF_HOME``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "prereg" / "G24_gliner2_planner_set.json"

#: Fixed by the pre-registration.
MODES: tuple[str, ...] = ("compose", "replay", "retrieve")
PER_MODE = {"compose": 34, "replay": 33, "retrieve": 33}
SEED = 2026_10_06

#: Replay cue from ``prompts/defaults/plan.yaml`` ("why something happened, how someone felt,
#: what was said around an event") and the engine's replay option ("exact wording or what was
#: said around an event"). Not an engine rule: it exists only to label this set.
REPLAY_CUE = re.compile(
    r"\bwhy\b|\breasons?\b|\bmotivat\w*|\binspir\w*|\bfe(?:el|els|lt|eling)\b|\breact\w*"
    r"|\b(?:say|said|says|tell|told|mention\w*)\b",
    re.I,
)


def label(question: str) -> tuple[str, str]:
    """The pre-registered target mode of ``question`` and the rule that set it.

    1. ``compose`` when ``is_count`` fires, or ``is_aggregation`` fires on a question that
       is not a date/duration question (``is_temporal``): counts, lists, sets.
    2. ``replay`` when ``is_ordering`` fires (evidence must be shown in time order) or the
       replay cue fires (why / feelings / what was said).
    3. ``retrieve`` otherwise: one specific fact, including plain date questions.
    """
    from memspine.core.query_shape import is_aggregation, is_count, is_ordering, is_temporal

    if is_count(question):
        return "compose", "is_count"
    if is_aggregation(question) and not is_temporal(question):
        return "compose", "is_aggregation"
    if is_ordering(question):
        return "replay", "is_ordering"
    if REPLAY_CUE.search(question):
        return "replay", "replay_cue"
    return "retrieve", "temporal_fact" if is_temporal(question) else "default"


def build(locomo: Path, out: Path = FIXTURE) -> dict[str, Any]:
    """Label all LoCoMo questions, sample :data:`PER_MODE` per mode, split 50/50."""
    raw = locomo.read_bytes()
    data = json.loads(raw)
    pool: dict[str, list[dict[str, Any]]] = {m: [] for m in MODES}
    seen: set[str] = set()
    for conv in data:
        for index, qa in enumerate(conv["qa"]):
            question = str(qa["question"]).strip()
            if question.lower() in seen:
                continue  # a few questions repeat across categories
            seen.add(question.lower())
            mode, rule = label(question)
            pool[mode].append(
                {
                    "id": f"{conv['sample_id']}#{index}",
                    "question": question,
                    "category": qa.get("category"),
                    "mode": mode,
                    "rule": rule,
                }
            )
    rng = random.Random(SEED)
    items: list[dict[str, Any]] = []
    for mode in MODES:
        chosen = rng.sample(pool[mode], PER_MODE[mode])
        for position, item in enumerate(chosen):
            item["split"] = "tune" if position % 2 == 0 else "heldout"
        items.extend(chosen)
    items.sort(key=lambda it: it["id"])
    fixture = {
        "description": "G24 GLiNER2 planner set; labels by evals/gliner2_planner_eval.py:label",
        "source": "LoCoMo-10 (locomo10.json)",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "seed": SEED,
        "pool_sizes": {m: len(pool[m]) for m in MODES},
        "items": items,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return fixture


def load_fixture(path: Path = FIXTURE) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["items"]
    return items


# ---------------------------------------------------------------------------------------
# Classifiers


Classifier = Callable[[str], tuple[str, float | None]]


@dataclass(frozen=True)
class Setup:
    """One GLiNER2 classification setup.

    ``labels`` maps the surface label shown to the model to the read mode it stands for;
    ``descriptions`` holds one description per surface label. ``rules_first`` runs the
    ``query_shape`` rules before the model (the hybrid); ``hints`` appends the rule
    predicates to the text the model sees.
    """

    name: str
    labels: Mapping[str, str]
    descriptions: Mapping[str, str]
    task: str = "choice"
    prompt: str | None = None
    examples: tuple[tuple[str, str], ...] = ()
    rules_first: bool = False
    hints: bool = False


#: The engine's options before this change (``Engine._READ_MODES`` at ``f21576b``).
CURRENT = Setup(
    "current",
    {m: m for m in MODES},
    {
        "compose": "the question asks for a count, a list, or several things over time",
        "replay": "the question needs exact wording or what was said around an event",
        "retrieve": "the question asks for one specific fact",
    },
)


def rule_mode(question: str) -> str | None:
    """The engine-side rules of the hybrid: compose for counts and sets (not durations),
    replay for ordering questions; None leaves the question to the model."""
    from memspine.core.query_shape import is_aggregation, is_count, is_ordering, is_temporal

    if is_count(question) or (is_aggregation(question) and not is_temporal(question)):
        return "compose"
    if is_ordering(question):
        return "replay"
    return None


def hint_text(question: str) -> str:
    from memspine.core.query_shape import is_aggregation, is_count, is_ordering, is_temporal

    tags = [
        name
        for name, hit in (
            ("count", is_count(question)),
            ("several items", is_aggregation(question)),
            ("order in time", is_ordering(question)),
            ("date", is_temporal(question)),
        )
        if hit
    ]
    return f"{question} (asks for: {', '.join(tags) or 'one fact'})"


def gliner2_classifier(setup: Setup, extractor: Any) -> Classifier:
    """A classifier calling ``extractor`` (a loaded GLiNER2 model) with ``setup``."""

    def classify(question: str) -> tuple[str, float | None]:
        if setup.rules_first:
            ruled = rule_mode(question)
            if ruled is not None:
                return ruled, None
        text = hint_text(question) if setup.hints else question
        kwargs: dict[str, Any] = {}
        if setup.prompt:
            kwargs["prompt"] = setup.prompt
        if setup.examples:
            kwargs["examples"] = list(setup.examples)
        schema = extractor.create_schema().classification(
            setup.task, dict(setup.descriptions), **kwargs
        )
        result = extractor.extract(text, schema, include_confidence=True)
        value = result[setup.task]
        surface, confidence = (
            (value["label"], value["confidence"]) if isinstance(value, Mapping) else (value, None)
        )
        return setup.labels[surface], None if confidence is None else float(confidence)

    return classify


def rules_baseline(question: str) -> tuple[str, float | None]:
    """``read(mode="auto")`` with no planner: compose on ``is_aggregation``, else replay."""
    from memspine.core.query_shape import is_aggregation

    return ("compose" if is_aggregation(question) else "replay"), None


# ---------------------------------------------------------------------------------------
# Metrics


@dataclass
class Score:
    name: str
    split: str
    n: int = 0
    correct: int = 0
    confusion: dict[str, Counter[str]] = field(default_factory=dict)
    confidences: list[float] = field(default_factory=list)
    ms: list[float] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    def recall(self, mode: str) -> float:
        row = self.confusion.get(mode, Counter())
        total = sum(row.values())
        return row[mode] / total if total else 0.0

    @property
    def macro_recall(self) -> float:
        return sum(self.recall(m) for m in MODES) / len(MODES)


def evaluate(
    name: str, classify: Classifier, items: Sequence[Mapping[str, Any]], split: str
) -> Score:
    score = Score(name, split, confusion={m: Counter() for m in MODES})
    for item in items:
        if split != "all" and item["split"] != split:
            continue
        started = time.perf_counter()
        got, confidence = classify(str(item["question"]))
        score.ms.append((time.perf_counter() - started) * 1000)
        score.n += 1
        score.correct += got == item["mode"]
        score.confusion[str(item["mode"])][got] += 1
        if confidence is not None:
            score.confidences.append(confidence)
    return score


def render_scores(scores: Sequence[Score]) -> str:
    lines = [
        "| setup | split | n | accuracy | recall compose | recall replay | recall retrieve "
        "| macro recall | median conf |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        conf = f"{statistics.median(s.confidences):.3f}" if s.confidences else "-"
        lines.append(
            f"| {s.name} | {s.split} | {s.n} | {s.accuracy:.3f} | {s.recall('compose'):.2f} "
            f"| {s.recall('replay'):.2f} | {s.recall('retrieve'):.2f} "
            f"| {s.macro_recall:.3f} | {conf} |"
        )
    return "\n".join(lines)


def render_confusion(score: Score) -> str:
    lines = [
        f"{score.name} ({score.split}); rows = label, columns = predicted",
        "",
        "| label \\ predicted | " + " | ".join(MODES) + " |",
        "|---|" + "---|" * len(MODES),
    ]
    for mode in MODES:
        row = score.confusion[mode]
        lines.append(f"| {mode} | " + " | ".join(str(row[m]) for m in MODES) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Candidate setups (tuned on the tune half only)

#: Setups tried on the tune half; see the results file for the tune table.
SETUPS: tuple[Setup, ...] = (CURRENT,)


def select(tune_scores: Sequence[Score]) -> str:
    """The pre-registered selection: best tune accuracy, ties by macro recall, then by the
    earlier entry in :data:`SETUPS`."""
    best = max(
        enumerate(tune_scores), key=lambda p: (p[1].accuracy, p[1].macro_recall, -p[0])
    )
    return best[1].name


def load_model(model: str) -> Any:
    from memspine.services.decision.gliner2_decision import gliner2_class

    return gliner2_class().from_pretrained(model)


def run(
    split: str,
    model: str,
    setups: Sequence[Setup],
    items: Sequence[Mapping[str, Any]],
    extractor: Any = None,
) -> list[Score]:
    extractor = extractor if extractor is not None else load_model(model)
    splits = ("tune", "heldout") if split == "both" else (split,)
    scores: list[Score] = []
    for which in splits:
        scores.append(evaluate("rules (no planner)", rules_baseline, items, which))
        scores.append(evaluate("constant retrieve", lambda _q: ("retrieve", None), items, which))
        for setup in setups:
            scores.append(evaluate(setup.name, gliner2_classifier(setup, extractor), items, which))
    return scores


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--locomo", type=Path, required=True)
    b.add_argument("--out", type=Path, default=FIXTURE)
    r = sub.add_parser("run")
    r.add_argument("--split", choices=("tune", "heldout", "both"), default="tune")
    r.add_argument("--model", default="fastino/gliner2-base-v1")
    r.add_argument("--setup", action="append", help="only these setup names")
    r.add_argument("--confusion", action="store_true")
    r.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "build":
        fixture = build(args.locomo, args.out)
        print(json.dumps({k: v for k, v in fixture.items() if k != "items"}, indent=1))
        print(Counter((it["mode"], it["split"]) for it in fixture["items"]))
        return 0
    setups = [s for s in SETUPS if not args.setup or s.name in args.setup]
    scores = run(args.split, args.model, setups, load_fixture())
    table = render_scores(scores)
    print(table)
    if args.confusion:
        for s in scores:
            print()
            print(render_confusion(s))
    if args.out:
        args.out.write_text(table + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
