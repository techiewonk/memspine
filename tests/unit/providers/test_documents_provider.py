"""ADR-081 ``storage.documents``: config, descriptor, Engine wiring and the erasure hook."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.config.schema.remote_providers import S3DocumentsSettings
from memspine.exceptions import ConfigError
from memspine.providers.builtin.documents import s3_spec
from memspine.providers.registry import (
    ProviderRegistryError,
    get_registry,
    provider_target_identity,
    validate_settings,
)

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


@pytest.mark.parametrize("name", ["mongodb"])
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


# ------------------------------------------------------------------ RM-112: s3 and weaviate
_S3 = {"bucket": "memspine-docs", "connect_timeout_s": 2.0, "request_timeout_s": 5.0}


def test_remote_descriptors_are_cluster_and_multi_writer() -> None:
    registry = get_registry("document_stores")
    s3, weaviate = registry.get("s3"), registry.get("weaviate")
    assert (s3.locality, s3.single_writer, s3.requires_deployment_id) == ("cluster", False, False)
    assert (weaviate.locality, weaviate.single_writer) == ("cluster", False)
    assert s3.service_manifest is not None and s3.service_manifest.extra == "aws"
    assert weaviate.service_manifest is not None and weaviate.service_manifest.extra == "weaviate"
    for name in ("s3", "weaviate"):
        config = MemspineConfig.model_validate(
            {"runtime": _RUNTIME, "storage": {"documents": {"provider": name}}}
        )
        assert config.storage.documents is not None


@pytest.mark.parametrize(
    ("changes", "fields"),
    [
        ({"bucket": "Bad_Bucket"}, "bucket"),
        ({"prefix": "/abs/"}, "prefix"),
        ({"prefix": "a/../"}, "<root>"),
        ({"access_key_id_env": "K"}, "<root>"),
        ({"session_token_env": "T"}, "<root>"),
        ({"profile": "p", "access_key_id_env": "K", "secret_access_key_env": "S"}, "<root>"),
        ({"part_size_bytes": 1024}, "part_size_bytes"),
        ({"max_bytes": 6 * 1024**3}, "max_bytes"),
        ({"addressing_style": "dns"}, "addressing_style"),
        ({"endpoint_url_env": "http://not-a-name"}, "endpoint_url_env"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_s3_settings_are_validated_strictly(changes: dict[str, Any], fields: str) -> None:
    descriptor = get_registry("document_stores").get("s3")
    with pytest.raises(ProviderRegistryError, match=f"invalid settings for documents.s3.*{fields}"):
        validate_settings(descriptor, {**_S3, **changes})


def test_s3_missing_credentials_variable_is_named_never_valued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = get_registry("document_stores").get("s3")
    settings = validate_settings(
        descriptor,
        {**_S3, "access_key_id_env": "DOCS_KEY_ID", "secret_access_key_env": "DOCS_SECRET"},
    )
    assert isinstance(settings, S3DocumentsSettings)
    monkeypatch.setenv("DOCS_KEY_ID", "AKIAVISIBLE")
    monkeypatch.delenv("DOCS_SECRET", raising=False)
    with pytest.raises(ConfigError, match="DOCS_SECRET") as raised:
        s3_spec(settings)
    assert "AKIAVISIBLE" not in str(raised.value)


def test_s3_target_identity_is_stable_and_credential_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = get_registry("document_stores").get("s3")
    monkeypatch.setenv("DOCS_URL", "http://minio.internal:9000")
    monkeypatch.setenv("DOCS_KEY_ID", "AKIAVISIBLE")
    monkeypatch.setenv("DOCS_SECRET", "very-secret")
    raw = {
        **_S3,
        "endpoint_url_env": "DOCS_URL",
        "access_key_id_env": "DOCS_KEY_ID",
        "secret_access_key_env": "DOCS_SECRET",
    }
    first = provider_target_identity(descriptor, validate_settings(descriptor, raw))
    again = provider_target_identity(descriptor, validate_settings(descriptor, raw))
    assert first == again and first is not None
    assert {k: v for k, v in first.items() if k != "auth_digest"} == {
        "kind": "s3",
        "endpoint": "http://minio.internal:9000",
        "region": None,
        "bucket": "memspine-docs",
        "prefix": "memspine/",
        "canonical_target": {
            "kind": "s3",
            "endpoint": "http://minio.internal:9000",
            "bucket": "memspine-docs",
            "prefix": "memspine/",
        },
    }
    assert "very-secret" not in repr(first) and "AKIAVISIBLE" not in repr(first)
    monkeypatch.setenv("DOCS_SECRET", "rotated")
    rotated = provider_target_identity(descriptor, validate_settings(descriptor, raw))
    assert rotated is not None and rotated["auth_digest"] != first["auth_digest"]


async def test_s3_slot_wires_into_the_engine_and_erasure(tmp_path: Path) -> None:
    if not os.environ.get("MEMSPINE_TEST_S3_URL"):
        pytest.skip("set MEMSPINE_TEST_S3_URL (MinIO)")
    settings: dict[str, Any] = {
        "bucket": os.environ.get("MEMSPINE_TEST_S3_BUCKET", "memspine-conformance"),
        "prefix": f"engine/{uuid.uuid4().hex}/",
        "endpoint_url_env": "MEMSPINE_TEST_S3_URL",
        "region": "us-east-1",
        "addressing_style": "path",
        "connect_timeout_s": 10.0,
        "request_timeout_s": 30.0,
        "create_bucket": True,
    }
    if os.environ.get("MEMSPINE_TEST_S3_ACCESS_KEY"):
        settings["access_key_id_env"] = "MEMSPINE_TEST_S3_ACCESS_KEY"
        settings["secret_access_key_env"] = "MEMSPINE_TEST_S3_SECRET_KEY"
    eng = _engine(tmp_path, {"provider": "s3", "settings": settings})
    await eng.start()
    try:
        docs = eng.documents
        assert docs is not None
        record = await eng.write("the contract is attached", namespace="ns")
        attached = await docs.put("ns", b"%PDF contract", record_ids=[record.record_id])
        await eng.forget(record.record_id, namespace="ns", hard=True)
        assert await docs.get("ns", attached.digest) is None
        await docs.put("ns", b"loose")
        await eng.erase_namespace("ns")
        assert await docs.list("ns") == []
    finally:
        await eng.stop()
