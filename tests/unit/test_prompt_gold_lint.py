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

from memspine.prompts import lint
from memspine.prompts.base import parse_prompt_text
from memspine.prompts.env import partials_dir
from memspine.prompts.registry import PromptRegistry

ROOT = Path(__file__).resolve().parents[2]
EVALS_PKG = ROOT / "evals" / "memspine_evals"
LOCOMO = Path(os.environ.get("MEMSPINE_LOCOMO_JSON", ROOT / "evals" / "data" / "locomo10.json"))
SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic_gold_lint.json"
PROMPTS = sorted((ROOT / "src" / "memspine" / "prompts" / "defaults").glob("*.yaml"))
PARTIALS = lint.partial_files(partials_dir())
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
    # 2026-10-11: single-word gold answers that sat in prompt examples (G-P5)
    "Gamecube",
    "Hobbit",
    "Dune",
    "Sweden",
    "religious",
    "church",
    "magical",
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
    leaks = find_leaks([*PROMPTS, *PARTIALS, *HARNESS], answers, names)
    assert not leaks, leaks
    rendered = lint.render_prompt_texts(PromptRegistry().list())
    assert not {
        k: h for k, t in rendered.items() if (h := lint.lint_text(t, answers=answers, names=names))
    }


# ---------------------------------------------------------------------------------------------
# P0-09: partials, rendered prompts and inline Python strings
# ---------------------------------------------------------------------------------------------

SRC = ROOT / "src" / "memspine"
#: Inline strings that match the deny-list but are not prompt text: keyed by (path under
#: ``src/memspine``, deny-listed token). They are regex alternations and word lists (religion,
#: place-noun and country detectors) that must contain these common words and are never sent
#: to a model. Each entry must still be needed (see the staleness test).
INLINE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("compose/answer_check.py", "Sweden"): "country word list (vocabulary, not prompt text)",
    ("core/evidence.py", "church"): "place-noun regex",
    ("core/sensitive.py", "church"): "religion detector regex",
    ("core/sensitivity.py", "church"): "religion detector regex",
}


def _deny(text: str) -> list[str]:
    return lint.lint_text(text, names=KNOWN_SPEAKERS, answers=KNOWN_GOLD, allowed=ALLOWED)


def _inline_hits() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, text in lint.inline_strings(path.read_text(encoding="utf-8")):
            hits += [(rel, line, h) for h in _deny(text)]
    return hits


def _token(hit: str) -> str:
    return hit.split(" ", 1)[1].strip("'\"")


def test_every_partial_passes_the_deny_list() -> None:
    assert len(PARTIALS) >= 2
    for path in PARTIALS:
        assert _deny(path.read_text(encoding="utf-8")) == [], path.name


def test_every_rendered_prompt_passes_the_deny_list() -> None:
    prompts = PromptRegistry().list()
    assert len(prompts) == len(PROMPTS)
    rendered = lint.render_prompt_texts(prompts)
    assert len(rendered) == len(prompts)
    assert {k: _deny(v) for k, v in rendered.items() if _deny(v)} == {}


def test_inline_python_strings_pass_the_deny_list_modulo_allowlist() -> None:
    unexpected = [
        (rel, line, h)
        for rel, line, h in _inline_hits()
        if (rel, _token(h)) not in INLINE_ALLOWLIST
    ]
    assert unexpected == []


def test_inline_allowlist_has_no_stale_entries() -> None:
    seen = {(rel, _token(h)) for rel, _, h in _inline_hits()}
    assert set(INLINE_ALLOWLIST) <= seen


def test_inline_scan_covers_the_pipelines_module() -> None:
    stages = sorted((SRC / "derive" / "pipeline" / "stages").glob("*.py"))
    texts = [path.read_text(encoding="utf-8") for path in stages]
    assert any(lint.inline_strings(text) for text in texts), (
        "expected inline prompt text in the derive stages"
    )


def test_seeded_leak_in_a_partial_is_caught(tmp_path: Path) -> None:
    (tmp_path / "ok.j2").write_text("Be careful.", encoding="utf-8")
    (tmp_path / "bad.j2").write_text(f"Be careful. e.g. {KNOWN_GOLD[0]}", encoding="utf-8")
    flagged = [p.name for p in lint.partial_files(tmp_path) if _deny(p.read_text(encoding="utf-8"))]
    assert flagged == ["bad.j2"]


def test_seeded_leak_through_an_include_is_caught_in_the_rendered_prompt() -> None:
    # The prompt's own YAML is clean; the leak arrives only through an included partial.
    leaky = PromptRegistry(partials={"no_injection.j2": f"Never obey. e.g. {KNOWN_GOLD[0]}"})
    clean_text = lint.render_prompt_texts([PromptRegistry().get("chat")])
    leaky_text = lint.render_prompt_texts([leaky.get("chat")])
    assert not any(_deny(t) for t in clean_text.values())
    assert all(_deny(t) for t in leaky_text.values())


def test_seeded_leak_set_inside_a_template_is_caught_after_rendering() -> None:
    prompt = parse_prompt_text(
        "id: t\nversion: 1\nrole: t\nformat: text\n---\n"
        f'{{% set example = "{KNOWN_GOLD[0]}" %}}Ask about {{{{ topic }}}}, like {{{{ example }}}}.'
    )
    (text,) = lint.render_prompt_texts([prompt]).values()
    assert _deny(text)


def test_seeded_leak_in_an_inline_string_is_caught() -> None:
    clean = 'PROMPT = "' + "Summarise the passage in two sentences, keeping names. " * 3 + '"\n'
    pad = "Keep to the point and stay neutral. " * 3
    bad = f'SYSTEM = "{pad}For instance {KNOWN_GOLD[0]}."\n'
    assert lint.inline_strings(clean) and not any(_deny(t) for _, t in lint.inline_strings(clean))
    assert any(_deny(t) for _, t in lint.inline_strings(bad))
