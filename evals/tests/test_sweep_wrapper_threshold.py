"""G8a offline sweep: plumbing on the synthetic dataset, and the pre-registered rule."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "sweep_wrapper_threshold.py"


def _module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("sweep_wrapper_threshold", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(mod, t: float, pt_raw: int, ua: tuple[int, int, int]):  # type: ignore[no-untyped-def]
    row = mod.Row(t)
    row.fixture_states = {
        f"f{i}": ("raw" if i < pt_raw else "untrusted_wrapped") for i in range(10)
    }
    row.u = {"U_a": ua, "U_b": ua}
    return row


def test_decision_rule_picks_lowest_threshold_with_minimal_pass_through() -> None:
    mod = _module()
    rows = [
        _row(mod, 0.0, 5, (10, 10, 10)),
        _row(mod, 0.3, 3, (10, 10, 10)),
        _row(mod, 0.4, 1, (10, 10, 10)),
        _row(mod, 0.5, 1, (10, 10, 10)),
        _row(mod, 0.6, 0, (9, 10, 10)),  # PT 0 but utility 0.9 < 0.95: not eligible
    ]
    chosen, _why = mod.decide(rows)
    assert chosen == 0.4


def test_decision_rule_refuses_when_nothing_is_eligible() -> None:
    mod = _module()
    chosen, why = mod.decide([_row(mod, 0.0, 5, (1, 1, 1)), _row(mod, 0.4, 0, (0, 0, 1))])
    assert chosen is None and "escalate" in why


def test_offline_sweep_runs_on_the_fixture_with_no_model_calls() -> None:
    pytest.importorskip("memspine")
    from memspine_evals.stub_llm import install_stub_litellm

    mod = _module()
    with install_stub_litellm() as stub:
        rows, meta = asyncio.run(mod.sweep(None, grid=(0.0, 0.4)))
    assert sum(stub.calls.values()) == 0
    assert "FIXTURE" in meta["utility_source"]
    off, on = rows
    assert set(off.fixture_states) == {f.fixture_id for f in mod._fixtures()}
    # the ingest-channel claim (trust 0.3) is raw with the wrapper off and wrapped at 0.4
    assert off.fixture_states["mfa-claim-ingest"] == "raw"
    assert on.fixture_states["mfa-claim-ingest"] == "untrusted_wrapped"
    assert on.pass_through <= off.pass_through
    assert off.utility("U_a") == 1.0  # the reference is measured against itself
    assert on.utility("U_b") < 1.0  # ingested turns (0.3) are wrapped at 0.4
    table = mod.render(rows, meta)
    assert "Decision rule outcome" in table and "| t=0.4 |" in table


def test_isolated_cell_matches_in_process() -> None:
    pytest.importorskip("memspine")
    mod = _module()
    item, _source = mod._load_item(None)
    child = mod._run_cell(0.4, "ingest", None)
    here = asyncio.run(mod._delivered(item, 0.4, "ingest"))
    assert child == here and child
