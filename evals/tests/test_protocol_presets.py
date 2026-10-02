"""H25: protocol presets set reader/judge/endpoint and are recorded."""

from __future__ import annotations

import pytest
from memspine_evals.experiments import PROTOCOL_PRESETS, C01Config, apply_protocol_preset


def test_omnimemeval_preset() -> None:
    cfg = apply_protocol_preset(C01Config(mode="qa"), "omnimemeval")
    assert cfg.reader_model == "gpt-4.1-mini" and cfg.judge_model == "gpt-4o-mini"
    assert "UNVERIFIED" in cfg.protocol_notes
    assert "omnimemeval" in PROTOCOL_PRESETS


def test_unknown_preset_rejected_and_none_is_identity() -> None:
    base = C01Config()
    assert apply_protocol_preset(base, None) is base
    with pytest.raises(ValueError):
        apply_protocol_preset(base, "nope")
