"""BI-05: complete annotation batches survive in the canonical effect ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from memspine.clients.sqlite.sync import sqlite_sync_engine
from memspine.runtime.outbox import EffectFence, SqlJobStore
from memspine.runtime.runner import InlineRunner
from memspine.schemas.durable import AnnotationBatch, IngestionSourceSnapshot, OwnerScope

SCOPE = OwnerScope(tenant="t", owner="tenant/alice")
NOW = datetime(2026, 10, 24, tzinfo=UTC)
SOURCE = {
    "namespace": SCOPE.owner,
    "record_id": "record-1",
    "source_id": "message-1",
    "source_version": 1,
    "content_hash": "a" * 16,
    "sender": "alice",
    "role": "user",
    "source_at": NOW.isoformat(),
    "erasure_generation": 0,
}


def _engine(path: Path) -> Any:
    return sqlite_sync_engine(str(path), busy_timeout_s=5.0)


def _effect(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "effect_id": "annotation000001",
        "layer": "annotations",
        "identity": {
            "effect_id": "annotation000001",
            "sources": [SOURCE],
        },
        "count": 0,
        "artifact": {"annotation_batches": [batch]},
    }


async def _authorized_store(path: Path) -> tuple[SqlJobStore, str, int, dict[str, Any]]:
    store = SqlJobStore(_engine(path))
    runner = InlineRunner(store)
    await runner.enqueue(SCOPE, "derive", {"record_ids": ["record-1"]})
    store.mark_sources(SCOPE, "derive", [SOURCE], NOW)
    claim = runner.book.claim("worker", timedelta(minutes=1))
    assert claim is not None
    batch = AnnotationBatch(
        source=IngestionSourceSnapshot.model_validate(SOURCE),
        mentions=(),
        producer_revision="gliner:test",
    ).model_dump(mode="json")
    effect = _effect(batch)
    assert (
        store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[SOURCE],
            at=NOW,
        )
        == "authorized"
    )
    return store, claim.job.job_id, claim.lease.epoch, effect


async def test_empty_annotation_success_survives_restart_and_replay(tmp_path: Path) -> None:
    db = tmp_path / "annotation.db"
    store, job_id, epoch, effect = await _authorized_store(db)
    assert store.finalize_effects(
        job_id=job_id, epoch=epoch, effects=[effect], failure=None, at=NOW
    )
    # Re-finalization is an idempotent overwrite of the same immutable effect.
    assert store.finalize_effects(
        job_id=job_id, epoch=epoch, effects=[effect], failure=None, at=NOW
    )
    different = _effect(
        {
            **effect["artifact"]["annotation_batches"][0],
            "producer_revision": "gliner:different",
        }
    )
    # A differing replay result is a typed fenced outcome, never a ValueError.
    assert (
        store.finalize_effects(
            job_id=job_id, epoch=epoch, effects=[different], failure=None, at=NOW
        )
        == EffectFence.REPLAY_DIVERGED
    )
    store.close()

    reopened = SqlJobStore(_engine(db))
    [row] = reopened.effect_status(SCOPE)
    assert row["state"] == "complete" and row["count"] == 0
    [restored] = row["artifact"]["annotation_batches"]
    assert AnnotationBatch.model_validate(restored).mentions == ()

    # A replacement source version clears the prior assessment instead of
    # allowing its artifact/count to survive beside the new empty result.
    changed_source = {**SOURCE, "source_version": 2, "content_hash": "c" * 16}
    reopened.mark_sources(SCOPE, "derive", [changed_source], NOW)
    assert reopened.effect_status(SCOPE) == []
    changed_batch = AnnotationBatch(
        source=IngestionSourceSnapshot.model_validate(changed_source),
        mentions=(),
        producer_revision="gliner:test",
    ).model_dump(mode="json")
    replacement = _effect(changed_batch)
    replacement["effect_id"] = "annotation000002"
    replacement["identity"] = {
        "effect_id": "annotation000002",
        "sources": [changed_source],
    }
    assert (
        reopened.authorize_effects(
            job_id=job_id,
            epoch=epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[replacement],
            sources=[changed_source],
            at=NOW,
        )
        == "authorized"
    )
    assert reopened.finalize_effects(
        job_id=job_id, epoch=epoch, effects=[replacement], failure=None, at=NOW
    )
    [cleared] = reopened.effect_status(SCOPE)
    assert cleared["count"] == 0
    assert cleared["artifact"]["annotation_batches"][0]["mentions"] == []
    reopened.close()


async def test_annotation_artifact_revalidates_binding_and_erasure_removes_it(
    tmp_path: Path,
) -> None:
    store, job_id, epoch, effect = await _authorized_store(tmp_path / "erasure.db")
    forged = dict(effect)
    forged_batch = dict(effect["artifact"]["annotation_batches"][0])
    forged_batch["mentions"] = [
        {
            "text": "Mira",
            "label": "person",
            "start": 0,
            "end": 4,
            "asserted": False,
            "source_namespace": SCOPE.owner,
            "source_record_id": "record-1",
            "source_version": 1,
            "source_content_fingerprint": "a" * 16,
            "source_binding_hash": "b" * 16,
            "source_role": "user",
            "source_channel": "messages",
            "sender_id": "mallory",
        }
    ]
    forged["artifact"] = {"annotation_batches": [forged_batch]}
    with pytest.raises(ValidationError, match="sender differs"):
        store.finalize_effects(job_id=job_id, epoch=epoch, effects=[forged], failure=None, at=NOW)
    assert store.effect_status(SCOPE)[0]["state"] == "authorized"

    assert store.finalize_effects(
        job_id=job_id, epoch=epoch, effects=[effect], failure=None, at=NOW
    )
    assert store.erase_source(SCOPE, "derive", "record-1")
    assert store.effect_status(SCOPE) == []
    store.close()


async def test_annotation_artifact_source_must_belong_to_effect_identity(
    tmp_path: Path,
) -> None:
    store, job_id, epoch, effect = await _authorized_store(tmp_path / "foreign.db")
    foreign_source = {
        **SOURCE,
        "namespace": "tenant/mallory",
        "record_id": "record-2",
        "source_id": "message-2",
    }
    foreign_batch = AnnotationBatch(
        source=IngestionSourceSnapshot.model_validate(foreign_source),
        mentions=(),
        producer_revision="gliner:test",
    ).model_dump(mode="json")
    forged = {**effect, "artifact": {"annotation_batches": [foreign_batch]}}
    with pytest.raises(ValueError, match="sources differ from effect identity"):
        store.finalize_effects(job_id=job_id, epoch=epoch, effects=[forged], failure=None, at=NOW)
    assert store.effect_status(SCOPE)[0]["state"] == "authorized"
    store.close()


async def test_annotation_artifact_cannot_omit_an_authorized_source(tmp_path: Path) -> None:
    store, job_id, epoch, original = await _authorized_store(tmp_path / "missing.db")
    second = {
        **SOURCE,
        "record_id": "record-2",
        "source_id": "message-2",
        "content_hash": "c" * 16,
    }
    store.mark_sources(SCOPE, "derive", [second], NOW)
    effect = {
        **original,
        "effect_id": "annotation000002",
        "identity": {
            "effect_id": "annotation000002",
            "sources": [SOURCE, second],
        },
    }
    assert (
        store.authorize_effects(
            job_id=job_id,
            epoch=epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[SOURCE, second],
            at=NOW,
        )
        == "authorized"
    )
    with pytest.raises(ValueError, match="sources differ from effect identity"):
        store.finalize_effects(job_id=job_id, epoch=epoch, effects=[effect], failure=None, at=NOW)
    store.close()
