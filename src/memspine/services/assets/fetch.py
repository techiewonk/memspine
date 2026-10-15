"""The controlled fetcher: ONLY the URI a turn itself referenced, once, with hard limits.

No search, no lookalike substitution, no cross-host redirect (a redirect to another host is
how an expired image becomes a "no longer available" placeholder, which is not the asset).
Every failure is an :class:`AssetFetchError` with a short machine reason; nothing is silent.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

__all__ = ["AssetFetchError", "AssetFetcher", "FetchedAsset", "HttpAssetFetcher", "sniff_mime"]

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


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urlparse(newurl).netloc.lower() != urlparse(req.full_url).netloc.lower():
            raise AssetFetchError("redirected_offsite", newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpAssetFetcher:
    """GET one http(s) URI; check size, declared type and the real type of the bytes."""

    def __init__(self, allowed_mime: tuple[str, ...]) -> None:
        self.allowed_mime = tuple(m.lower() for m in allowed_mime)

    def _get(self, uri: str, max_bytes: int, timeout_s: float) -> FetchedAsset:
        if urlparse(uri).scheme.lower() not in ("http", "https"):
            raise AssetFetchError("unsupported_scheme", uri)
        opener = urllib.request.build_opener(_SameHostRedirect)
        request = urllib.request.Request(uri, headers={"User-Agent": "memspine-asset/1"})
        try:
            with opener.open(request, timeout=timeout_s) as response:
                declared = (response.headers.get_content_type() or "").lower()
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise AssetFetchError("too_large", f"{length} > {max_bytes}")
                data = response.read(max_bytes + 1)
        except AssetFetchError:
            raise
        except urllib.error.HTTPError as exc:
            raise AssetFetchError(f"http_{exc.code}", uri) from exc
        except TimeoutError as exc:
            raise AssetFetchError("timeout", uri) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AssetFetchError("unreachable", str(exc)) from exc
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

    async def fetch(self, uri: str, *, max_bytes: int, timeout_s: float) -> FetchedAsset:
        return await asyncio.to_thread(self._get, uri, max_bytes, timeout_s)
