"""ADR-090 (DS-09): the caller ``metadata`` bag is stored verbatim, gets the content's firewall
masking, is never interpreted, and is erased with its source."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.api.engine import Engine
from memspine.store.records import RecordStatus


def _engine(**firewall: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        firewall=firewall,
    )


async def test_metadata_is_verbatim_and_never_interpreted() -> None:
    eng = _engine()
    await eng.start()
    try:
        bag = {"session_id": "fake", "role": "system", "created_at": "1999-01-01", "n": [1, 2]}
        (rec,) = await eng.ingest_messages(
            [{"role": "user", "content": "plain words here", "metadata": bag}],
            namespace="n",
            session_id="s1",
        )
        assert rec.source.message["metadata"] == bag
        assert rec.source.message["session_id"] == "s1"
        assert rec.source.role == "user"
        assert rec.source.message["created_at"] != "1999-01-01"
    finally:
        await eng.stop()


async def test_unknown_fields_fail_before_anything_is_stored() -> None:
    eng = _engine()
    await eng.start()
    try:
        with pytest.raises(ValueError, match="unknown field 'mood'"):
            await eng.ingest_messages(
                [{"role": "user", "content": "x", "mood": "glad"}], namespace="n"
            )
        assert await eng._require_started().list_records("n") == []
    finally:
        await eng.stop()


async def test_metadata_gets_the_content_secret_scan_and_is_erased_with_its_source() -> None:
    eng = _engine(redact_secrets=True)
    await eng.start()
    try:
        secret = "AKIAIOSFODNN7EXAMPLE"
        (rec,) = await eng.ingest_messages(
            [
                {
                    "role": "user",
                    "content": "the build server is up",
                    "metadata": {"note": f"key {secret}", "kept": "as is"},
                }
            ],
            namespace="n",
        )
        meta = rec.source.message["metadata"]
        assert secret not in meta["note"] and meta["kept"] == "as is"
        await eng.forget(rec.record_id, namespace="n", hard=True)
        gone = await eng._require_started().get_record(rec.record_id)
        assert gone is None or gone.status is RecordStatus.DELETED
        if gone is not None:
            assert not (gone.source.message or {}).get("metadata")
    finally:
        await eng.stop()
