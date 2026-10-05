"""G24: the GLiNER2 planner eval's labelling, frozen set, procedure and shipped setup.

No model is loaded: the extractor is a fake with gliner2's ``create_schema`` / ``extract``
shape.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gliner2_planner_eval.py"


def _module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("gliner2_planner_eval", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("question", "mode", "rule"),
    [
        ("How many children does Melanie have?", "compose", "is_count"),
        ("What books has Melanie read?", "compose", "is_aggregation"),
        ("How many months passed between the trips?", "retrieve", "temporal_fact"),
        ("What was the first concert Caroline went to?", "replay", "is_ordering"),
        ("Why did Jon open a dance studio?", "replay", "replay_cue"),
        ("How did Melanie feel after the race?", "replay", "replay_cue"),
        ("When did Caroline go to the support group?", "retrieve", "temporal_fact"),
        ("Where did Melanie move from?", "retrieve", "default"),
    ],
)
def test_label_rules(question: str, mode: str, rule: str) -> None:
    assert _module().label(question) == (mode, rule)


def test_frozen_set_matches_its_registration() -> None:
    mod = _module()
    items = mod.load_fixture()
    assert len(items) == 100 and len({it["id"] for it in items}) == 100
    assert Counter(it["mode"] for it in items) == Counter(mod.PER_MODE)
    assert Counter(it["split"] for it in items) == {"tune": 51, "heldout": 49}
    # No relabelling: every frozen label is still what the registered rules give.
    for it in items:
        assert mod.label(it["question"]) == (it["mode"], it["rule"])


class _Schema:
    def __init__(self) -> None:
        self.task = ""
        self.labels: dict[str, str] = {}
        self.kwargs: dict[str, Any] = {}

    def classification(self, task: str, labels: dict[str, str], **kwargs: Any) -> _Schema:
        self.task, self.labels, self.kwargs = task, labels, kwargs
        return self


class _KeywordModel:
    """Picks the first label whose description shares a word with the question."""

    def __init__(self) -> None:
        self.calls = 0

    def create_schema(self) -> _Schema:
        return _Schema()

    def extract(self, text: str, schema: _Schema, include_confidence: bool) -> dict[str, Any]:
        self.calls += 1
        words = set(text.lower().replace("?", "").split())
        for label, desc in schema.labels.items():
            if words & set(desc.lower().replace(",", " ").replace(";", " ").split()):
                return {schema.task: {"label": label, "confidence": 0.7}}
        return {schema.task: {"label": list(schema.labels)[-1], "confidence": 0.5}}


def test_classifier_maps_surface_labels_and_runs_rules_first() -> None:
    mod = _module()
    model = _KeywordModel()
    hybrid = next(s for s in mod.SETUPS if s.name == "D + rules first")
    classify = mod.gliner2_classifier(hybrid, model)
    assert classify("How many dogs does Ana have?") == ("compose", None)
    assert model.calls == 0
    assert classify("Why did Ana move?") == ("replay", 0.7)
    assert model.calls == 1


def test_examples_are_passed_to_the_schema() -> None:
    mod = _module()
    seen: list[_Schema] = []

    class _Spy(_KeywordModel):
        def create_schema(self) -> _Schema:
            seen.append(_Schema())
            return seen[-1]

    setup = next(s for s in mod.SETUPS if s.name == "A task=question type + examples")
    mod.gliner2_classifier(setup, _Spy())("Where does Ana live?")
    assert seen[0].task == "question type"
    assert seen[0].kwargs["examples"] and "LoCoMo" not in str(seen[0].kwargs["examples"])


def test_procedure_selects_on_tune_and_applies_the_rule() -> None:
    mod = _module()
    setups = [mod.CURRENT, *(s for s in mod.SETUPS if s.name in ("D", "D + rules first"))]
    outcome = mod.procedure(_KeywordModel(), mod.load_fixture(), setups)
    assert [s.split for s in outcome.tune] == ["tune"] * (2 + len(setups))
    assert {s.split for s in outcome.heldout} == {"heldout"}
    tune = {s.name: s for s in outcome.tune}
    assert outcome.selected == max(
        (s.name for s in setups), key=lambda n: (tune[n].accuracy, tune[n].macro_recall)
    )
    heldout = {s.name: s for s in outcome.heldout}
    assert outcome.ship == (heldout[outcome.selected].accuracy > heldout["current"].accuracy)
    text = mod.report(outcome, "fake", "test")
    assert "Held-out half" in text and "Decision rule" in text


def test_shipped_setup_is_the_engines_planner() -> None:
    """The engine's decision planner options are exactly the shipped setup's."""
    from memspine import Engine

    mod = _module()
    shipped = next(s for s in mod.SETUPS if s.name == mod.SHIPPED)
    assert shipped.rules_first and shipped.task == "choice" and not shipped.hints
    assert not shipped.examples
    assert {
        label: (shipped.labels[label], desc) for label, desc in shipped.descriptions.items()
    } == Engine._READ_OPTIONS
