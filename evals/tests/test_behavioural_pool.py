"""INV-FILLER on the ``read.rerank`` path (R3-07 / S16).

The behavioural gate must give the reranker the arm's pool. Model-free: a lexical-overlap stub replaces
the reranker chain, the hash embedder stands in for the encoder.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import screen_behavioural as vb
from memspine_evals.behavioural import Status, run_behavioural

import memspine.read.pipeline.rerank as rerank_mod
from memspine import Engine

ARMS = Path(vb.__file__).resolve().parent / "arms"


def _arm(name: str) -> dict[str, Any]:
    return json.loads((ARMS / f"{name}.json").read_text(encoding="utf-8"))


def _overlay_for_s16() -> tuple[dict[str, Any], dict[str, Any]]:
    arm = _arm("R3-07-rerank")
    return vb.overlay(arm, _arm("scr-pool-protect-jina")), arm


def test_pool_shape_carried_for_the_current_reranker_chain() -> None:
    ov, arm = _overlay_for_s16()
    assert ov["read"] == {
        "lift_before_rerank": True,
    }  # the diff alone has no pool keys
    carried = vb.with_pool_shape(ov, arm)
    assert carried["read"]["pool_protect_per_leg"] == 3 and carried["read"]["candidate_pool"] == 2


def test_flag_off_overlay_is_unchanged() -> None:
    arm = _arm("R3-08-lift_before_rerank")
    ov = vb.overlay(arm, _arm("scr-pool-protect-jina"))
    assert vb.with_pool_shape(ov, arm) == ov


def _filler_status(config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Status:
    async def lexical(eng: Any, query: str, documents: list[str], cfg: Any) -> Any:
        q = set(query.lower().replace("?", "").split())
        words = [set(d.lower().replace(".", "").replace(",", "").split()) for d in documents]
        scores = [float(len(q & w)) for w in words]
        return scores, "stub:lexical", None, []

    monkeypatch.setattr(rerank_mod, "chain_score", lexical)
    monkeypatch.setattr("memspine.read.pipeline.router.chain_score", lexical, raising=False)

    def factory(d: Path) -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            storage={"records": {"provider": "sqlite", "settings": {"path": str(d / "b.db")}}},
            model={"embedding": {"provider": "hash"}, **config.get("model", {})},
            read={"record_access": False, **config.get("read", {})},
            memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        )

    logging.disable(logging.CRITICAL)
    try:
        return run_behavioural(factory, only={"INV-FILLER"}).results[0].status
    finally:
        logging.disable(logging.NOTSET)


def test_inv_filler_on_the_base_path(monkeypatch: pytest.MonkeyPatch) -> None:
    ov, arm = _overlay_for_s16()
    config = vb.with_pool_shape(ov, arm)
    assert _filler_status(config, monkeypatch) is Status.PASS
