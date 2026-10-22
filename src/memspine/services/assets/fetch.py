"""The controlled fetcher: ONLY the URI a turn itself referenced, once, with hard limits.

No search, no lookalike substitution, no cross-host redirect (a redirect to another host is
how an expired image becomes a "no longer available" placeholder, which is not the asset).
Every failure is an :class:`AssetFetchError` with a short machine reason; nothing is silent.
The transport is the Engine's shared :class:`~memspine.clients.http.HttpConnector` (RM-113);
it never follows redirects itself, so each hop is checked here first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlparse

from memspine.clients.http import HttpConnector, HttpError, HttpTimeout

__all__ = [
    "AssetFetchError",
    "AssetFetcher",
    "FetchedAsset",
    "HttpAssetFetcher",
    "check_redirect",
    "sniff_mime",
]

#: Same-host redirect hops followed before the fetch fails (``too_many_redirects``).
MAX_REDIRECTS = 5

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(data: bytes) -> str | None:
    """The image type the bytes actually are (None when not a known image)."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class AssetFetchError(Exception):
    """A fetch that did not yield the referenced asset. ``reason`` is a short code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class FetchedAsset:
    data: bytes
    mime: str


class AssetFetcher(Protocol):
    async def fetch(self, uri: str, *, max_bytes: int, timeout_s: float) -> FetchedAsset: ...


def _check_scheme(uri: str) -> None:
    if urlparse(uri).scheme.lower() not in ("http", "https"):
        raise AssetFetchError("unsupported_scheme", uri)


def check_redirect(current: str, location: str) -> str:
    """The absolute redirect target, refusing another host or a non-http(s) scheme."""
    target = urljoin(current, location)
    if urlparse(target).netloc.lower() != urlparse(current).netloc.lower():
        raise AssetFetchError("redirected_offsite", target)
    _check_scheme(target)
    return target


class HttpAssetFetcher:
    """GET one http(s) URI; check size, declared type and the real type of the bytes."""

    def __init__(self, allowed_mime: tuple[str, ...], http: HttpConnector) -> None:
        self.allowed_mime = tuple(m.lower() for m in allowed_mime)
        self._http = http

    async def fetch(self, uri: str, *, max_bytes: int, timeout_s: float) -> FetchedAsset:
        _check_scheme(uri)
        current = uri
        for _ in range(MAX_REDIRECTS + 1):
            try:
                reply = await self._http.request(
                    "GET",
                    current,
                    headers={"User-Agent": "memspine-asset/1"},
                    timeout_s=timeout_s,
                    max_bytes=max_bytes,
                )
            except HttpTimeout as exc:
                raise AssetFetchError("timeout", uri) from exc
            except HttpError as exc:
                raise AssetFetchError("unreachable", str(exc)) from exc
            if reply.is_redirect:
                current = check_redirect(current, reply.headers["location"])
                continue
            if reply.status >= 400:
                raise AssetFetchError(f"http_{reply.status}", uri)
            declared = (reply.headers.get("content-type") or "").split(";")[0].strip().lower()
            length = reply.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise AssetFetchError("too_large", f"{length} > {max_bytes}")
            return self._check(uri, reply.body, declared, max_bytes)
        raise AssetFetchError("too_many_redirects", uri)

    def _check(self, uri: str, data: bytes, declared: str, max_bytes: int) -> FetchedAsset:
        if len(data) > max_bytes:
            raise AssetFetchError("too_large", f"> {max_bytes}")
        if not data:
            raise AssetFetchError("empty", uri)
        actual = sniff_mime(data)
        if actual is None:
            raise AssetFetchError("not_an_image", f"declared {declared or 'unknown'}")
        if declared.startswith("image/") and declared != actual and declared != "image/jpg":
            raise AssetFetchError("mime_mismatch", f"declared {declared}, bytes are {actual}")
        if actual not in self.allowed_mime:
            raise AssetFetchError("mime_not_allowed", actual)
        return FetchedAsset(data=data, mime=actual)
