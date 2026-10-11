"""N50 (plan v3.2 / S6d): no benchmark gold answer or speaker name in any prompt.

Prompt examples once quoted LoCoMo gold answers ("Charlotte's Web", "Lake Tahoe", "the
week before 9 June 2023") and speaker names. An example that is a test answer leaks
the test into every arm that uses the prompt. Examples must be invented.

The lint logic is tested on a committed synthetic fixture, so it runs in CI where no benchmark
data is on disk; the check against the real LoCoMo file is an extra that runs when the file
is present (``evals/data/locomo10.json`` or ``$MEMSPINE_LOCOMO_JSON``).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVALS_PKG = ROOT / "evals" / "memspine_evals"
LOCOMO = Path(os.environ.get("MEMSPINE_LOCOMO_JSON", ROOT / "evals" / "data" / "locomo10.json"))
SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic_gold_lint.json"
PROMPTS = sorted((ROOT / "src" / "memspine" / "prompts" / "defaults").glob("*.yaml"))
#: Answer formats a prompt may name: an instruction, not a fact of the benchmark.
ALLOWED = {"likely yes", "likely no"}
#: Reader, judge and OP-Bench prompt code. ``legacy_prompts.py`` is exempt on purpose: it holds
#: the old contaminated reader prompts byte-for-byte, reachable only as ``*_legacy`` names.
HARNESS = [
    EVALS_PKG / name
    for name in (
        "readers.py",
        "judge.py",
        "judge_prompts.py",
        "opbench.py",
        "refusal.py",
        "cli.py",
        "date_check.py",
        "date_repair.py",
        "duration_solve.py",
        "split.py",
    )
]
LEGACY_FILE = EVALS_PKG / "legacy_prompts.py"

#: Known leaks, kept as a standing deny-list so CI catches them with no data on disk: gold
#: strings and example dates once printed in prompts, and the LoCoMo speaker names.
KNOWN_GOLD = (
    "the week before 9 June 2023",
    "9 June 2023",
    "25 May 2023",
    "19 May 2023",
    "2023-05-20",
    "15 July 2023",
    "2023-07-15",
    "2023-07-14",
    "Charlotte's Web",
    "Lake Tahoe",
)
KNOWN_SPEAKERS = (
    "Caroline",
    "Melanie",
    "Jon",
    "Gina",
    "John",
    "Maria",
    "Joanna",
    "Nate",
    "Tim",
    "Audrey",
    "Andrew",
    "Deborah",
    "Jolene",
    "Evan",
    "Calvin",
    "Dave",
)


def distinctive_answers_and_names(data: list[dict]) -> tuple[set[str], set[str]]:
    answers: set[str] = set()
    names: set[str] = set()
    for conv in data:
        names.update({conv["conversation"]["speaker_a"], conv["conversation"]["speaker_b"]})
        for qa in conv["qa"]:
            answer = str(qa.get("answer", "")).strip().strip('"')
            # Distinctive: a multi-word answer holding a capitalised word (a title, a
            # place, a dated phrase), not a common word like "Single" or "Running".
            if len(answer.split()) >= 2 and re.search(r"\b[A-Z][a-z]+", answer):
                answers.add(answer)
    return answers, names


def find_leaks(
    paths: list[Path], answers: set[str], names: set[str], *, literal: tuple[str, ...] = ()
) -> list[str]:
    leaks: list[str] = []
    for path in paths:
        text = " ".join(path.read_text(encoding="utf-8").split())
        low = text.lower()
        leaks += [
            f"{path.name}: answer {a!r}"
            for a in sorted(answers | set(literal))
            if a.lower() in low and a.lower() not in ALLOWED
        ]
        leaks += [
            f"{path.name}: speaker {n!r}" for n in sorted(names) if re.search(rf"\b{n}\b", text)
        ]
    return leaks


def _load(path: Path) -> tuple[set[str], set[str]]:
    return distinctive_answers_and_names(json.loads(path.read_text(encoding="utf-8")))


def test_synthetic_fixture_yields_distinctive_gold_and_names_only() -> None:
    answers, names = _load(SYNTHETIC)
    assert answers == {"Marmalade Bridge Harbour", "The fortnight before 3 March 2099"}
    assert names == {"Zephyrine", "Quillon"}


def test_lint_flags_a_prompt_that_quotes_gold_or_a_speaker(tmp_path: Path) -> None:
    answers, names = _load(SYNTHETIC)
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        'Answer in the memories\' wording (for example "the fortnight  before 3 March 2099"),\n'
        "as Zephyrine would say it. Marmalade Bridge Harbour is a ferry stop.\n",
        encoding="utf-8",
    )
    clean = tmp_path / "clean.yaml"
    clean.write_text(
        'Answer in the wording the memories use (for example "the week before <date>").\n'
    )
    assert find_leaks([clean], answers, names) == []
    leaks = find_leaks([bad], answers, names)
    assert any("fortnight before 3 March 2099" in leak for leak in leaks)
    assert any("Marmalade Bridge Harbour" in leak for leak in leaks)
    assert any("'Zephyrine'" in leak for leak in leaks)
    assert not any("Quillon" in leak for leak in leaks)  # absent from the text, not flagged


def test_lint_allows_instruction_formats(tmp_path: Path) -> None:
    p = tmp_path / "ok.yaml"
    p.write_text("Answer 'Likely yes' or 'Likely no' with a reason.\n")
    assert find_leaks([p], {"Likely yes", "Likely no"}, set()) == []


def test_shipped_prompts_pass_the_synthetic_fixture_and_the_standing_deny_list() -> None:
    answers, names = _load(SYNTHETIC)
    files = [*PROMPTS, *HARNESS]
    assert PROMPTS and all(p.exists() for p in HARNESS)
    assert find_leaks(files, answers, names | set(KNOWN_SPEAKERS), literal=KNOWN_GOLD) == []


def test_legacy_prompts_are_reachable_only_under_legacy_names() -> None:
    from memspine_evals import readers

    legacy = {k for k in readers.QA_PROMPTS if k.endswith("_legacy")}
    assert legacy == {"grounded_legacy", "grounded_detail_legacy"}
    # nothing but readers.py imports the legacy module
    importers = [
        p.name
        for p in EVALS_PKG.rglob("*.py")
        if p != LEGACY_FILE and "legacy_prompts" in p.read_text(encoding="utf-8")
    ]
    assert importers == ["readers.py"]
    # every other registered prompt (fixed, derived or routed) is free of the known gold
    for name, text in {**readers.QA_PROMPTS, **readers.ROUTED_QA_VARIANTS}.items():
        if name in legacy:
            continue
        hits = [g for g in KNOWN_GOLD if g.lower() in text.lower()]
        assert not hits, (name, hits)
    # the default names are the clean prompts; the legacy text is the one with the gold
    assert readers.QA_PROMPTS["grounded"] == readers.QA_PROMPTS["grounded_nodate"]
    assert readers.QA_PROMPTS["grounded_detail"] == readers.QA_PROMPTS["grounded_detail_nodate"]
    assert "9 June 2023" in readers.QA_PROMPTS["grounded_legacy"]
    assert "9 June 2023" in readers.QA_PROMPTS["grounded_detail_legacy"]


@pytest.mark.skipif(not LOCOMO.exists(), reason="LoCoMo data not present (optional extra check)")
def test_no_locomo_gold_answer_or_speaker_in_any_prompt() -> None:
    answers, names = _load(LOCOMO)
    leaks = find_leaks([*PROMPTS, *HARNESS], answers, names)
    assert not leaks, leaks
