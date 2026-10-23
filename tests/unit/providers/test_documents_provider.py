"""ADR-081 ``storage.documents``: config, descriptor, Engine wiring and the erasure hook."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.registry import get_registry

_RUNTIME = {
    "provider_services": {
        "health_timeout_s": 5.0,
        "close_timeout_s": 10.0,
        "startup_timeout_s": None,
    }
}


def _engine(tmp_path: Path, documents: dict[str, Any] | None, **overrides: Any) -> Engine:
    storage: dict[str, Any] = {
        "records": {
            "provider": "sqlite",
            "settings": {"path": str(tmp_path / "m.db"), "busy_timeout_s": 5.0},
        },
    }
    if documents is not None:
        storage["documents"] = documents
    return Engine(
        template="core",
        dotenv_path=None,
        storage=storage,
        model={"embedding": {"provider": "hash", "settings": {}}},
        **overrides,
    )


@pytest.mark.parametrize("name", ["mongodb", "s3", "weaviate"])
def test_planned_providers_are_rejected_naming_rm_112(name: str) -> None:
    with pytest.raises(ConfigError, match=f"provider {name!r} is planned \\(RM-112\\)"):
        MemspineConfig.model_validate(
            {"runtime": _RUNTIME, "storage": {"documents": {"provider": name}}}
        )


def test_filesystem_slot_is_accepted_and_descriptor_declares_host_single_writer() -> None:
    config = MemspineConfig.model_validate(
        {"runtime": _RUNTIME, "storage": {"documents": {"provider": "filesystem"}}}
    )
    assert config.storage.documents is not None
    descriptor = get_registry("document_stores").get("filesystem")
    assert (descriptor.locality, descriptor.single_writer) == ("host", True)


async def test_unknown_provider_fails_at_start(tmp_path: Path) -> None:
    eng = _engine(tmp_path, {"provider": "nope"})
    with pytest.raises(ConfigError, match="storage.documents.provider"):
        await eng.start()
    await eng.stop()


async def test_default_root_sits_beside_the_records_store(tmp_path: Path) -> None:
    eng = _engine(tmp_path, {"provider": "filesystem"})
    await eng.start()
    try:
        assert eng.documents is not None
        head = await eng.documents.put("default", b"attachment")
        assert await eng.documents.get("default", head.digest) == b"attachment"
    finally:
        await eng.stop()
    assert (tmp_path / "m.db.documents").is_dir()


async def test_without_the_slot_there_is_no_document_store(tmp_path: Path) -> None:
    eng = _engine(tmp_path, None)
    await eng.start()
    try:
        assert eng.documents is None
    finally:
        await eng.stop()


async def test_hard_forget_and_namespace_erasure_remove_referenced_documents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "docs"
    eng = _engine(tmp_path, {"provider": "filesystem", "settings": {"root": str(root)}})
    await eng.start()
    try:
        docs = eng.documents
        assert docs is not None
        first = await eng.write("the invoice is attached", namespace="ns")
        second = await eng.write("the photo is attached", namespace="ns")
        invoice = await docs.put("ns", b"%PDF invoice", record_ids=[first.record_id])
        photo = await docs.put("ns", b"\x89PNG photo", record_ids=[second.record_id])
        both = await docs.put("ns", b"shared", record_ids=[first.record_id, second.record_id])
        loose = await docs.put("ns", b"no reference")

        await eng.forget(second.record_id, namespace="ns")  # soft: nothing is erased
        assert await docs.get("ns", photo.digest) == b"\x89PNG photo"

        await eng.forget(first.record_id, namespace="ns", hard=True)
        assert await docs.get("ns", invoice.digest) is None  # its only reference was erased
        shared_head = await docs.head("ns", both.digest)
        assert shared_head is not None and shared_head.record_ids == (second.record_id,)
        assert await docs.get("ns", loose.digest) == b"no reference"

        await eng.erase_namespace("ns")
        assert await docs.list("ns") == []
    finally:
        await eng.stop()
