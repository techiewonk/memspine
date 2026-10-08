"""SpineTune (G-23): guarded search on dev, confirmation on held-out, no answer leaks.

A synthetic evaluator stands in for the retrieval-only runner: 10 items x 30
questions; the knob ``read.good`` = True covers 6 more questions per item (a real
gain), ``read.noise`` flips one question per item at random (no real gain)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.spinetune import (
    Knob,
    SearchSpace,
    Tuner,
    TuneSettings,
    apply_knobs,
    main,
    sign_test,
    split_items,
)

ITEMS = [f"conv-{i}" for i in range(10)]
SPACE = SearchSpace((Knob("read.good", (False, True)), Knob("read.noise", (False, True))))


def synthetic(calls: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def evaluate(config: dict[str, Any], items: list[str]) -> dict[str, bool]:
        calls.append({"config": config, "items": list(items)})
        read = config.get("read", {})
        out: dict[str, bool] = {}
        for item in items:
            rng = random.Random(f"{item}|{read.get('noise')}")
            for q in range(30):
                covered = q < 15 or (read.get("good") and q < 21)
                if read.get("noise") and q == rng.randrange(30):
                    covered = not covered
                out[f"{item}|{q}"] = bool(covered)
        return out

    return evaluate


def test_split_is_deterministic_and_disjoint() -> None:
    dev, held = split_items(ITEMS, 0.5, seed=3)
    assert (dev, held) == split_items(ITEMS, 0.5, seed=3)
    assert not set(dev) & set(held) and sorted(dev + held) == sorted(ITEMS)


def test_sign_test_counts_and_p() -> None:
    base = {str(i): i < 5 for i in range(20)}
    arm = {str(i): i < 15 for i in range(20)}
    t = sign_test(base, arm)
    assert (t["won"], t["lost"], t["delta"]) == (10, 0, 50.0)
    assert t["p"] < 0.01
    assert sign_test(base, base)["p"] == 1.0


def test_apply_knobs_writes_dotted_paths() -> None:
    out = apply_knobs({"read": {"a": 1}}, {"read.b": 2, "x.y.z": 3})
    assert out == {"read": {"a": 1, "b": 2}, "x": {"y": {"z": 3}}}


@pytest.mark.parametrize("algo", ["coordinate", "random", "halving"])
def test_each_algorithm_finds_the_real_gain_and_confirms_it(algo: str) -> None:
    calls: list[dict[str, Any]] = []
    tuner = Tuner(
        SPACE,
        {"read": {"good": False, "noise": False}},
        synthetic(calls),
        ITEMS,
        TuneSettings(algo=algo, max_trials=8, seed=5),
        log=lambda _m: None,
    )
    result = tuner.run()
    assert result.best_settings.get("read.good") is True
    assert result.verdict == "confirmed (auto-tuned)"
    assert result.heldout and result.heldout["won"] > 0
    # Search never touched held-out items; only the final confirmation did.
    search_calls = calls[:-2]
    assert all(not set(c["items"]) & set(result.heldout_items) for c in search_calls)


def test_noise_alone_is_never_accepted() -> None:
    space = SearchSpace((Knob("read.noise", (False, True)),))
    result = Tuner(
        space,
        {"read": {"noise": False}},
        synthetic([]),
        ITEMS,
        TuneSettings(algo="coordinate", max_trials=4),
        log=lambda _m: None,
    ).run()
    assert result.best_settings == {}
    assert result.verdict == "no change"


def test_guard_dataset_can_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    def hurts(config: dict[str, Any], items: list[str]) -> dict[str, bool]:
        good = config.get("read", {}).get("good")
        return {f"{i}|{q}": (q < 20 and not good) or (q < 5) for i in items for q in range(30)}

    result = Tuner(
        SPACE,
        {"read": {"good": False, "noise": False}},
        synthetic([]),
        ITEMS,
        TuneSettings(algo="coordinate", max_trials=4),
        guard=(hurts, ["g1", "g2"]),
        log=lambda _m: None,
    ).run()
    assert result.best_settings.get("read.good") is True
    assert result.guard and result.guard["lost"] > result.guard["won"]
    assert result.verdict == "not confirmed on held-out"


def test_budget_caps_the_trials() -> None:
    result = Tuner(
        SPACE,
        {"read": {"good": False, "noise": False}},
        synthetic([]),
        ITEMS,
        TuneSettings(algo="random", max_trials=1),
        log=lambda _m: None,
    ).run()
    assert len(result.trials) <= 1


def test_dry_run_runs_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    space = tmp_path / "space.json"
    space.write_text(json.dumps({"knobs": [{"path": "read.good", "values": [False, True]}]}))
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"read": {}}))
    code = main(
        [
            "--space",
            str(space),
            "--base",
            str(base),
            "--path",
            "unused",
            "--items",
            ",".join(ITEMS),
            "--dry-run",
        ]
    )
    assert code == 0
    assert "read.good" in capsys.readouterr().out


def test_declared_default_is_not_re_run_as_a_trial() -> None:
    calls: list[dict[str, Any]] = []
    space = SearchSpace(
        (
            Knob("read.noise", (False, True), default=False),
            Knob("read.good", (False, True), default=False),
        )
    )
    result = Tuner(
        space,
        {"read": {}},
        synthetic(calls),
        ITEMS,
        TuneSettings(algo="coordinate", max_trials=2),
        log=lambda _m: None,
    ).run()
    tried = [t.settings for t in result.trials]
    assert {"read.noise": False} not in tried
    assert any(t.get("read.good") is True for t in tried)
