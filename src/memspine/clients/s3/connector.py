"""S3-compatible object-store connector (RM-112, ``[aws]``): one bucket under one key prefix.

Driver choice: **boto3** (already the ``[aws]`` extra) run in worker threads with
:func:`asyncio.to_thread`. A boto3 client is thread-safe and its connection pool is sized by
``max_pool_connections``, so blocking calls in threads give real concurrency without an event
loop binding (``scope = "thread"``: Engines on different loops share one client). The async
alternatives (aioboto3 / aiobotocore) were rejected: a second dependency that pins botocore to
an exact version and so fights the boto3 the Secrets resolver and Bedrock already use
(docs/DEPENDENCY_ANALYSIS.md, A14).

The connector owns every object operation (RM-113 standard, as ``clients/fs`` does for files):
the adapter (:mod:`memspine.services.documents.s3`) passes keys *relative to the prefix* and
never sees boto3 or botocore. Keys are confined to the prefix: an empty key, a leading ``/``,
a ``.`` or ``..`` segment, a backslash or a control character is refused. Driver errors are
mapped to :class:`~memspine.exceptions.StorageError` subclasses carrying only the operation
and the S3 error code, never the driver's message (it can carry endpoints and request ids).

Credentials come from named environment variables resolved by the factory into
:class:`~memspine.clients._common.secrets.Secret` values (``repr`` shows ``<redacted>``), or,
when none are named, from the standard boto3 chain (an optional named profile, instance or
task roles). Identity carries only an auth digest.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from memspine.clients._common.deadlines import check_deadline, probe
from memspine.clients._common.endpoints import is_loopback, parse_endpoint
from memspine.clients._common.secrets import Secret, auth_digest
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import ConfigError, MissingServiceError, StorageError

__all__ = [
    "ObjectLimitError",
    "PreconditionFailedError",
    "S3Connector",
    "S3Credentials",
    "S3Spec",
    "StagedObject",
]

AddressingStyle = Literal["auto", "path", "virtual"]
ChecksumMode = Literal["when_supported", "when_required"]

#: The minimum S3 multipart part size (every part but the last).
_MIN_PART_BYTES = 5 * 1024 * 1024
#: DeleteObjects accepts at most 1000 keys per request.
_DELETE_BATCH = 1000
_NOT_FOUND = frozenset({"404", "NoSuchKey", "NotFound"})
_NO_BUCKET = frozenset({"404", "NoSuchBucket", "NotFound"})
_PRECONDITION = frozenset({"412", "PreconditionFailed", "409", "ConditionalRequestConflict"})


class PreconditionFailedError(StorageError):
    """A conditional write (``If-Match`` / ``If-None-Match``) lost a race; re-read and retry."""


class ObjectLimitError(StorageError):
    """A streamed upload exceeded the caller's byte limit (nothing was kept)."""


@dataclass(frozen=True, slots=True)
class S3Credentials:
    """Static credentials (from named environment variables); never rendered."""

    access_key_id: Secret
    secret_access_key: Secret
    session_token: Secret | None = None


@dataclass(frozen=True, slots=True)
class S3Spec:
    """Transport spec of one bucket and key prefix.

    ``endpoint_url`` None is the AWS regional endpoint; set it for an S3-compatible store
    (MinIO, Ceph RGW, R2). ``credentials`` None uses the boto3 chain (``profile`` optional).
    ``checksum_mode`` is botocore's ``request_checksum_calculation``: ``when_required`` for
    stores that reject the default CRC checksums. ``create_bucket`` lets :meth:`open` create a
    missing bucket (development stores); otherwise a missing bucket fails the open."""

    bucket: str
    connect_timeout_s: float
    request_timeout_s: float
    prefix: str = ""
    endpoint_url: str | None = None
    region: str | None = None
    profile: str | None = None
    credentials: S3Credentials | None = None
    addressing_style: AddressingStyle = "auto"
    checksum_mode: ChecksumMode = "when_supported"
    max_pool_connections: int | None = None
    create_bucket: bool = False

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ConfigError("an S3 spec needs a bucket")
        check_deadline(self.connect_timeout_s, "connect_timeout_s")
        check_deadline(self.request_timeout_s, "request_timeout_s")
        if self.prefix and (self.prefix.startswith("/") or not self.prefix.endswith("/")):
            raise ConfigError("an S3 key prefix must not start with '/' and must end with '/'")
        if self.endpoint_url is not None:
            parse_endpoint(self.endpoint_url, allowed_schemes=("http", "https"))


@dataclass(frozen=True, slots=True)
class StagedObject:
    """The result of :meth:`S3Connector.upload_stream`.

    A payload that fit in one part is returned ``inline`` and nothing was uploaded; a larger
    one was uploaded (multipart) to ``key`` and must be copied into place or deleted."""

    size: int
    sha256: str
    inline: bytes | None = None
    key: str | None = None


def _normal_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    endpoint = parse_endpoint(url, allowed_schemes=("http", "https"))
    return f"{parsed.scheme.lower()}://{endpoint.host.lower()}:{endpoint.port}"


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = error.get("Code")
        if code:
            return str(code)
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status:
            return str(status)
    return type(exc).__name__


class S3Connector:
    """A boto3 S3 client bound to one bucket and prefix; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "documents.s3"
    scope: ClassVar[Scope] = "thread"

    def __init__(self, spec: S3Spec) -> None:
        self._spec = spec
        self._client: Any = None
        self._lock = threading.Lock()

    # -- connector protocol ----------------------------------------------------------------
    @classmethod
    def identity(cls, spec: S3Spec) -> ConnectorIdentity:
        endpoint = _normal_endpoint(spec.endpoint_url) if spec.endpoint_url else "aws"
        creds = spec.credentials
        if creds is not None:
            digest = auth_digest(
                "static",
                creds.access_key_id.reveal(),
                creds.secret_access_key.reveal(),
                creds.session_token.reveal() if creds.session_token is not None else "",
            )
        else:
            digest = auth_digest("chain", spec.profile or "")
        return ConnectorIdentity(
            cls.kind,
            {
                "kind": "s3",
                "endpoint": endpoint,
                "region": spec.region,
                "bucket": spec.bucket,
                "prefix": spec.prefix,
            },
            digest,
            {
                "connect_timeout_s": spec.connect_timeout_s,
                "request_timeout_s": spec.request_timeout_s,
                "addressing_style": spec.addressing_style,
                "checksum_mode": spec.checksum_mode,
                "max_pool_connections": spec.max_pool_connections,
            },
            {"kind": "s3", "endpoint": endpoint, "bucket": spec.bucket, "prefix": spec.prefix},
        )

    @classmethod
    def locality(cls, spec: S3Spec) -> Locality:
        if spec.endpoint_url is None:
            return "cluster"
        endpoint = parse_endpoint(spec.endpoint_url, allowed_schemes=("http", "https"))
        return "host" if is_loopback(endpoint.host) else "cluster"

    @property
    def spec(self) -> S3Spec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._client is None:
            raise StorageError("S3 connector is not open — call open() first")
        return self._client

    def describe(self) -> dict[str, Any]:
        """Safe facts for diagnostics: no credentials, no auth digest."""
        spec = self._spec
        return {
            "kind": "s3",
            "endpoint": _normal_endpoint(spec.endpoint_url) if spec.endpoint_url else "aws",
            "region": spec.region,
            "bucket": spec.bucket,
            "prefix": spec.prefix,
            "credentials": "static" if spec.credentials is not None else "chain",
            "open": self._client is not None,
        }

    async def open(self) -> None:
        """Build the client and prove the bucket is reachable (``HeadBucket``) within
        ``connect_timeout_s``; a missing bucket is created only with ``create_bucket``."""
        if self._client is not None:
            return
        try:
            async with asyncio.timeout(self._spec.connect_timeout_s):
                client = await asyncio.to_thread(self._build_client)
                self._client = client
                await asyncio.to_thread(self._ensure_bucket, client)
        except BaseException as exc:
            client, self._client = self._client, None
            if client is not None:
                await asyncio.to_thread(client.close)
            if isinstance(exc, StorageError | ConfigError | MissingServiceError):
                raise
            if isinstance(exc, TimeoutError):
                raise StorageError(
                    f"S3 bucket {self._spec.bucket!r} did not answer within "
                    f"{self._spec.connect_timeout_s}s"
                ) from None
            if isinstance(exc, Exception):
                raise StorageError(
                    f"S3 bucket {self._spec.bucket!r} is not reachable ({_error_code(exc)})"
                ) from None
            raise

    def _build_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise MissingServiceError("provider:s3", extra="aws") from None
        spec = self._spec
        creds = spec.credentials
        if creds is not None:
            session = boto3.session.Session(
                aws_access_key_id=creds.access_key_id.reveal(),
                aws_secret_access_key=creds.secret_access_key.reveal(),
                aws_session_token=(
                    creds.session_token.reveal() if creds.session_token is not None else None
                ),
                region_name=spec.region,
            )
        else:
            session = boto3.session.Session(region_name=spec.region, profile_name=spec.profile)
        options: dict[str, Any] = {
            "connect_timeout": spec.connect_timeout_s,
            "read_timeout": spec.request_timeout_s,
            "retries": {"mode": "standard", "max_attempts": 3},
            "s3": {"addressing_style": spec.addressing_style},
            "request_checksum_calculation": spec.checksum_mode,
            "response_checksum_validation": spec.checksum_mode,
        }
        if spec.max_pool_connections is not None:
            options["max_pool_connections"] = spec.max_pool_connections
        return session.client("s3", endpoint_url=spec.endpoint_url, config=Config(**options))

    def _ensure_bucket(self, client: Any) -> None:
        from botocore.exceptions import ClientError

        bucket = self._spec.bucket
        try:
            client.head_bucket(Bucket=bucket)
            return
        except ClientError as exc:
            code = _error_code(exc)
            if code not in _NO_BUCKET:
                raise StorageError(f"S3 bucket {bucket!r} is not usable ({code})") from None
        if not self._spec.create_bucket:
            raise StorageError(
                f"S3 bucket {bucket!r} does not exist; create it or set "
                "storage.documents.settings.create_bucket: true"
            )
        params: dict[str, Any] = {"Bucket": bucket}
        if self._spec.region and self._spec.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": self._spec.region}
        try:
            client.create_bucket(**params)
        except ClientError as exc:
            code = _error_code(exc)
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise StorageError(f"S3 bucket {bucket!r} could not be created ({code})") from None

    async def health(self, deadline_s: float) -> bool:
        """``HeadBucket``: a real round trip that proves the bucket and the credentials."""
        client = self._client
        if client is None:
            return False

        async def check() -> bool:
            await asyncio.to_thread(client.head_bucket, Bucket=self._spec.bucket)
            return True

        return await probe(deadline_s, check)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.close)

    # -- keys and errors -------------------------------------------------------------------
    def _key(self, rel: str) -> str:
        parts = rel.split("/")
        if (
            not rel
            or rel.startswith("/")
            or any(part in ("", ".", "..") for part in parts[:-1])
            or parts[-1] in (".", "..")
            or "\\" in rel
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in rel)
        ):
            raise StorageError("refused an S3 key outside the documents prefix")
        return f"{self._spec.prefix}{rel}"

    def _relative(self, key: str) -> str:
        return key[len(self._spec.prefix) :]

    async def _call(self, op: str, fn: Any, /, **kwargs: Any) -> Any:
        """Run one boto3 call in a thread; map driver errors to memspine errors."""
        from botocore.exceptions import BotoCoreError, ClientError

        client = self.handle
        try:
            return await asyncio.to_thread(getattr(client, fn), Bucket=self._spec.bucket, **kwargs)
        except ClientError as exc:
            code = _error_code(exc)
            if code in _PRECONDITION:
                raise PreconditionFailedError(f"S3 {op}: precondition failed ({code})") from None
            if code in _NOT_FOUND:
                raise _NotFound(code) from None
            raise StorageError(f"S3 {op} failed ({code})") from None
        except BotoCoreError as exc:
            raise StorageError(f"S3 {op} failed ({type(exc).__name__})") from None

    # -- exclusive section -----------------------------------------------------------------
    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Serialise a read-modify-write across every holder of this connector in this
        process (used when the store does not honour conditional writes)."""
        await asyncio.to_thread(self._lock.acquire)
        try:
            yield
        finally:
            self._lock.release()

    # -- object operations -----------------------------------------------------------------
    async def put_bytes(
        self,
        rel: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str:
        """``PutObject``; returns the new ETag. ``if_match`` / ``if_none_match`` make it a
        conditional write that raises :class:`PreconditionFailedError` on a lost race."""
        kwargs: dict[str, Any] = {
            "Key": self._key(rel),
            "Body": data,
            "ContentType": content_type,
        }
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        response = await self._call("put", "put_object", **kwargs)
        return str(response.get("ETag", ""))

    async def get_bytes(self, rel: str) -> tuple[bytes, str] | None:
        """``GetObject``: the bytes and the ETag, or None when the key does not exist."""
        try:
            response = await self._call("get", "get_object", Key=self._key(rel))
        except _NotFound:
            return None
        body = response["Body"]
        try:
            data = await asyncio.to_thread(body.read)
        except Exception as exc:
            raise StorageError(f"S3 get failed ({type(exc).__name__})") from None
        finally:
            body.close()
        return bytes(data), str(response.get("ETag", ""))

    async def exists(self, rel: str) -> bool:
        try:
            await self._call("head", "head_object", Key=self._key(rel))
        except _NotFound:
            return False
        return True

    async def iter_bytes(self, rel: str, chunk_size: int) -> AsyncIterator[bytes]:
        """Stream the object in chunks of exactly ``chunk_size`` (the last may be shorter);
        a missing key raises :class:`StorageError`."""
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        try:
            response = await self._call("get", "get_object", Key=self._key(rel))
        except _NotFound:
            raise StorageError("no such object under the documents prefix") from None
        body = response["Body"]
        buffer = bytearray()
        try:
            while True:
                try:
                    piece = await asyncio.to_thread(body.read, chunk_size)
                except Exception as exc:
                    raise StorageError(f"S3 get failed ({type(exc).__name__})") from None
                if not piece:
                    break
                buffer += piece
                while len(buffer) >= chunk_size:
                    yield bytes(buffer[:chunk_size])
                    del buffer[:chunk_size]
            if buffer:
                yield bytes(buffer)
        finally:
            body.close()

    async def copy(self, source_rel: str, target_rel: str, *, content_type: str) -> None:
        """Server-side ``CopyObject`` (one request up to 5 GiB)."""
        await self._call(
            "copy",
            "copy_object",
            Key=self._key(target_rel),
            CopySource={"Bucket": self._spec.bucket, "Key": self._key(source_rel)},
            ContentType=content_type,
            MetadataDirective="REPLACE",
        )

    async def delete(self, rels: Sequence[str]) -> None:
        """Delete keys (a missing key is not an error), 1000 per ``DeleteObjects`` request."""
        keys = [self._key(rel) for rel in rels]
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            response = await self._call(
                "delete",
                "delete_objects",
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            errors = response.get("Errors") or []
            if errors:
                code = str(errors[0].get("Code", "unknown"))
                raise StorageError(f"S3 delete failed for {len(errors)} key(s) ({code})")

    async def list_keys(self, rel_prefix: str) -> list[str]:
        """Every key under ``rel_prefix`` (relative to the documents prefix), in S3's
        lexicographic order."""
        prefix = self._key(rel_prefix) if rel_prefix else self._spec.prefix
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Prefix": prefix}
            if token is not None:
                kwargs["ContinuationToken"] = token
            response = await self._call("list", "list_objects_v2", **kwargs)
            keys.extend(self._relative(item["Key"]) for item in response.get("Contents", ()))
            if not response.get("IsTruncated"):
                return keys
            token = response.get("NextContinuationToken")

    async def upload_stream(
        self,
        staging_rel: str,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
        *,
        max_bytes: int,
        part_size: int,
        content_type: str = "application/octet-stream",
    ) -> StagedObject:
        """Read ``chunks`` with a running SHA-256 and a size cap.

        A payload of at most ``part_size`` bytes is returned inline (nothing uploaded); a
        larger one streams to ``staging_rel`` as a multipart upload holding at most one part
        in memory. Over ``max_bytes`` raises :class:`ObjectLimitError` and aborts the upload.
        """
        if part_size < _MIN_PART_BYTES:
            raise ValueError(f"part_size must be at least {_MIN_PART_BYTES} bytes")
        digest = hashlib.sha256()
        size = 0
        buffer = bytearray()
        upload_id: str | None = None
        parts: list[dict[str, Any]] = []
        key = self._key(staging_rel)

        async def flush_part(data: bytes) -> None:
            nonlocal upload_id
            if upload_id is None:
                created = await self._call(
                    "upload", "create_multipart_upload", Key=key, ContentType=content_type
                )
                upload_id = str(created["UploadId"])
            number = len(parts) + 1
            response = await self._call(
                "upload",
                "upload_part",
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                Body=data,
            )
            parts.append({"PartNumber": number, "ETag": response["ETag"]})

        async def take(chunk: bytes) -> None:
            nonlocal size
            if not isinstance(chunk, bytes | bytearray | memoryview):
                raise TypeError("document chunks must be bytes")
            size += len(chunk)
            if size > max_bytes:
                raise ObjectLimitError(f"document exceeds the {max_bytes}-byte limit")
            digest.update(chunk)
            buffer.extend(chunk)
            while len(buffer) > part_size:
                await flush_part(bytes(buffer[:part_size]))
                del buffer[:part_size]

        try:
            if isinstance(chunks, AsyncIterable):
                async for chunk in chunks:
                    await take(chunk)
            else:
                for chunk in chunks:
                    await take(chunk)
            if upload_id is None:
                return StagedObject(size, digest.hexdigest(), inline=bytes(buffer))
            if buffer:
                await flush_part(bytes(buffer))
            await self._call(
                "upload",
                "complete_multipart_upload",
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException:
            if upload_id is not None:
                # The original error matters; a bucket lifecycle rule reaps leftovers.
                with contextlib.suppress(Exception):
                    await self._call(
                        "upload", "abort_multipart_upload", Key=key, UploadId=upload_id
                    )
            raise
        return StagedObject(size, digest.hexdigest(), key=staging_rel)


class _NotFound(StorageError):
    """Internal: the key does not exist (mapped to None/False by the public methods)."""
