"""N50 (plan v3.2 / S6d): no benchmark gold answer or speaker name in any prompt.

Prompt examples once quoted LoCoMo gold answers ("Charlotte's Web", "Lake Tahoe", "the
week before 9 June 2023") and speaker names. An example that is a test answer leaks
the test into every arm that uses the prompt. Examples must be invented.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCOMO = ROOT / "evals" / "data" / "locomo10.json"
PROMPTS = sorted((ROOT / "src" / "memspine" / "prompts" / "defaults").glob("*.yaml"))
#: Answer formats a prompt may name: an instruction, not a fact of the benchmark.
ALLOWED = {"likely yes", "likely no"}
HARNESS = [ROOT / "evals" / "memspine_evals" / name for name in ("readers.py", "judge_prompts.py")]


def _distinctive_answers_and_names() -> tuple[set[str], set[str]]:
    data = json.loads(LOCOMO.read_text(encoding="utf-8"))
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


@pytest.mark.skipif(not LOCOMO.exists(), reason="LoCoMo data not present")
def test_no_locomo_gold_answer_or_speaker_in_any_prompt() -> None:
    answers, names = _distinctive_answers_and_names()
    leaks: list[str] = []
    for path in [*PROMPTS, *(p for p in HARNESS if p.exists())]:
        text = " ".join(path.read_text(encoding="utf-8").split())
        low = text.lower()
        leaks += [
            f"{path.name}: answer {a!r}"
            for a in answers
            if a.lower() in low and a.lower() not in ALLOWED
        ]
        leaks += [f"{path.name}: speaker {n!r}" for n in names if re.search(rf"\b{n}\b", text)]
    assert not leaks, leaks
