"""RM-112: the S3 connector (``clients/s3``): spec validation, credential-free identity,
locality, key confinement, error redaction, and (live, with ``MEMSPINE_TEST_S3_URL``) open,
health, close and the multipart upload path."""

from __future__ import annotations

import os
import uuid

import pytest

from memspine.clients._common.secrets import Secret
from memspine.clients.base import Connector
from memspine.clients.s3 import ObjectLimitError, S3Connector, S3Credentials, S3Spec
from memspine.exceptions import ConfigError, StorageError

S3_URL = os.environ.get("MEMSPINE_TEST_S3_URL")
live = pytest.mark.skipif(not S3_URL, reason="set MEMSPINE_TEST_S3_URL (MinIO)")
MiB = 1024 * 1024


def _creds(key: str = "AKIAEXAMPLE", secret: str = "s3cr3t-value") -> S3Credentials:
    return S3Credentials(Secret(key), Secret(secret))


def _spec(**changes: object) -> S3Spec:
    base: dict[str, object] = {
        "bucket": "memspine-docs",
        "connect_timeout_s": 2.0,
        "request_timeout_s": 5.0,
        "prefix": "app/",
        "endpoint_url": "https://s3.example.com",
        "credentials": _creds(),
    }
    base.update(changes)
    return S3Spec(**base)  # type: ignore[arg-type]


def _live_spec(bucket: str, **changes: object) -> S3Spec:
    creds = None
    if os.environ.get("MEMSPINE_TEST_S3_ACCESS_KEY"):
        creds = S3Credentials(
            Secret(os.environ["MEMSPINE_TEST_S3_ACCESS_KEY"]),
            Secret(os.environ.get("MEMSPINE_TEST_S3_SECRET_KEY", "")),
        )
    base: dict[str, object] = {
        "bucket": bucket,
        "connect_timeout_s": 10.0,
        "request_timeout_s": 30.0,
        "prefix": f"connector/{uuid.uuid4().hex}/",
        "endpoint_url": S3_URL,
        "region": "us-east-1",
        "credentials": creds,
        "addressing_style": "path",
        "create_bucket": True,
    }
    base.update(changes)
    return S3Spec(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------ identity and redaction
def test_identity_is_credential_free_and_pinned() -> None:
    identity = S3Connector.identity(_spec())
    assert identity.target == {
        "kind": "s3",
        "endpoint": "https://s3.example.com:443",
        "region": None,
        "bucket": "memspine-docs",
        "prefix": "app/",
    }
    assert identity.canonical == {
        "kind": "s3",
        "endpoint": "https://s3.example.com:443",
        "bucket": "memspine-docs",
        "prefix": "app/",
    }
    rendered = repr(identity) + repr(_spec()) + repr(identity.descriptor_target())
    assert "s3cr3t-value" not in rendered and "AKIAEXAMPLE" not in rendered
    # Stable across calls and spellings of the same endpoint.
    assert S3Connector.identity(_spec(endpoint_url="https://S3.example.com:443/")) == identity


def test_auth_digest_separates_principals_and_the_boto3_chain() -> None:
    one = S3Connector.identity(_spec()).auth_digest
    two = S3Connector.identity(_spec(credentials=_creds(secret="other"))).auth_digest
    chain = S3Connector.identity(_spec(credentials=None)).auth_digest
    profile = S3Connector.identity(_spec(credentials=None, profile="ops")).auth_digest
    assert len({one, two, chain, profile}) == 4


def test_locality_follows_the_endpoint() -> None:
    assert S3Connector.locality(_spec()) == "cluster"
    assert S3Connector.locality(_spec(endpoint_url=None)) == "cluster"  # AWS
    assert S3Connector.locality(_spec(endpoint_url="http://127.0.0.1:9000")) == "host"


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"bucket": ""}, "bucket"),
        ({"prefix": "/abs/"}, "prefix"),
        ({"prefix": "no-slash"}, "prefix"),
        ({"connect_timeout_s": 0.0}, "connect_timeout_s"),
        ({"endpoint_url": "https://user:pw@s3.example.com"}, "without credentials"),
        ({"endpoint_url": "https://s3.example.com/path"}, "without credentials or path"),
    ],
)
def test_spec_rejects_bad_values(changes: dict[str, object], match: str) -> None:
    with pytest.raises((ConfigError, ValueError), match=match) as raised:
        _spec(**changes)
    assert "pw@" not in str(raised.value)


@pytest.mark.parametrize("key", ["", "/x", "a/../b", "./a", "a\\b", "a/\x00", "a//b", ".."])
def test_keys_are_confined_to_the_prefix(key: str) -> None:
    connector = S3Connector(_spec())
    with pytest.raises(StorageError, match="outside the documents prefix"):
        connector._key(key)


def test_protocol_members_and_closed_state() -> None:
    connector = S3Connector(_spec())
    assert isinstance(connector, Connector)
    assert S3Connector.scope == "thread"
    with pytest.raises(StorageError, match="not open"):
        _ = connector.handle
    described = connector.describe()
    assert described["credentials"] == "static" and "s3cr3t" not in repr(described)


async def test_closed_connector_health_is_false_and_close_is_idempotent() -> None:
    connector = S3Connector(_spec())
    assert not await connector.health(1.0)
    await connector.close()
    await connector.close()


async def test_unreachable_endpoint_fails_within_the_deadline_without_secrets() -> None:
    connector = S3Connector(
        _spec(endpoint_url="http://127.0.0.1:9", connect_timeout_s=3.0, request_timeout_s=1.0)
    )
    with pytest.raises(StorageError) as raised:
        await connector.open()
    assert "memspine-docs" in str(raised.value)
    assert "s3cr3t-value" not in str(raised.value) and "AKIAEXAMPLE" not in str(raised.value)
    assert not await connector.health(1.0)


# ------------------------------------------------------------------ live (MinIO)
@live
async def test_live_open_health_close_round_trip() -> None:
    connector = S3Connector(_live_spec("memspine-connector"))
    await connector.open()
    await connector.open()  # idempotent
    try:
        assert await connector.health(5.0)
        etag = await connector.put_bytes("probe/a.txt", b"hello", content_type="text/plain")
        got = await connector.get_bytes("probe/a.txt")
        assert got is not None and got == (b"hello", etag)
        assert await connector.list_keys("probe/") == ["probe/a.txt"]
        await connector.delete(["probe/a.txt", "probe/missing"])
        assert await connector.get_bytes("probe/a.txt") is None
    finally:
        await connector.close()
        await connector.close()
    assert not await connector.health(1.0)


@live
async def test_live_missing_bucket_is_refused_without_create_bucket() -> None:
    connector = S3Connector(_live_spec(f"absent-{uuid.uuid4().hex[:12]}", create_bucket=False))
    with pytest.raises(StorageError, match="create_bucket"):
        await connector.open()


@live
async def test_live_wrong_credentials_fail_without_echoing_them() -> None:
    spec = _live_spec(
        "memspine-connector", credentials=_creds("wrong-key-id", "wrong-secret-value")
    )
    connector = S3Connector(spec)
    with pytest.raises(StorageError) as raised:
        await connector.open()
    assert "wrong-secret-value" not in str(raised.value)
    assert "wrong-key-id" not in str(raised.value)


@live
async def test_live_multipart_upload_and_over_limit_abort() -> None:
    connector = S3Connector(_live_spec("memspine-connector"))
    await connector.open()
    try:
        data = os.urandom(11 * MiB + 17)
        chunks = [data[i : i + MiB] for i in range(0, len(data), MiB)]
        staged = await connector.upload_stream(
            "tmp/big", chunks, max_bytes=64 * MiB, part_size=5 * MiB
        )
        assert staged.inline is None and staged.key == "tmp/big" and staged.size == len(data)
        got = await connector.get_bytes("tmp/big")
        assert got is not None and got[0] == data
        small = await connector.upload_stream(
            "tmp/s", [b"ab", b"c"], max_bytes=10, part_size=5 * MiB
        )
        assert small.inline == b"abc" and small.key is None
        with pytest.raises(ObjectLimitError):
            await connector.upload_stream("tmp/over", chunks, max_bytes=6 * MiB, part_size=5 * MiB)
        assert await connector.get_bytes("tmp/over") is None
        uploads = await connector._call(
            "upload", "list_multipart_uploads", Prefix=connector.spec.prefix
        )
        assert not uploads.get("Uploads")  # the over-limit upload was aborted
    finally:
        await connector.delete(await connector.list_keys(""))
        await connector.close()
