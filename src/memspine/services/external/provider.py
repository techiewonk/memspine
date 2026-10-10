"""External evidence providers: a port, a no-op default, and an env-configured HTTP search.

A provider receives ONLY a :class:`~memspine.services.external.privacy.PublicQuery` text and
returns snippets. No key is ever hard-coded: the HTTP provider reads its endpoint and
credential from the environment.

``MEMSPINE_EXTERNAL_SEARCH_URL``     endpoint; called as ``GET <url>?q=<query>&n=<count>``
``MEMSPINE_EXTERNAL_SEARCH_KEY``     optional credential
``MEMSPINE_EXTERNAL_SEARCH_HEADER``  header carrying it (default ``Authorization``; the value
                                     is ``Bearer <key>`` for that header, the bare key otherwise)

The reply is JSON: a list, or an object with ``results`` / ``items`` / ``data`` holding
objects with ``title``, ``snippet`` (or ``content`` / ``text`` / ``description``) and ``url``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "ExternalProvider",
    "ExternalSnippet",
    "HttpSearchProvider",
    "NoopProvider",
    "ProviderError",
    "ProviderUnavailableError",
    "clean_snippet",
]

URL_ENV = "MEMSPINE_EXTERNAL_SEARCH_URL"
KEY_ENV = "MEMSPINE_EXTERNAL_SEARCH_KEY"
HEADER_ENV = "MEMSPINE_EXTERNAL_SEARCH_HEADER"


class ProviderError(RuntimeError):
    """The provider was reached (or tried) and failed."""


class ProviderUnavailableError(ProviderError):
    """No provider is configured."""


@dataclass(frozen=True, slots=True)
class ExternalSnippet:
    title: str
    text: str
    url: str = ""


class ExternalProvider(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> list[ExternalSnippet]: ...


class NoopProvider:
    """The default: nothing configured. A search is an explicit ``ProviderUnavailableError``."""

    name = "none"

    async def search(self, query: str, max_results: int) -> list[ExternalSnippet]:
        raise ProviderUnavailableError("no external evidence provider is configured")


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_snippet(value: object, limit: int = 400) -> str:
    """One line of plain text: control characters and brackets removed (so a snippet cannot
    forge a ``[...]`` marker), whitespace collapsed, length capped."""
    text = _CONTROL.sub(" ", str(value or ""))
    text = text.replace("[", "(").replace("]", ")")
    return " ".join(text.split())[:limit]


def _items(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("results", "items", "data"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [p for p in inner if isinstance(p, Mapping)]
    return []


class HttpSearchProvider:
    name = "http"

    def __init__(self, url: str, key: str | None = None, header: str = "Authorization") -> None:
        self.url = url
        self.key = key
        self.header = header
        self.timeout = 10.0

    @classmethod
    def from_env(cls) -> HttpSearchProvider:
        url = os.environ.get(URL_ENV, "").strip()
        if not url:
            raise ProviderUnavailableError(f"{URL_ENV} is not set")
        return cls(
            url,
            os.environ.get(KEY_ENV) or None,
            os.environ.get(HEADER_ENV, "Authorization") or "Authorization",
        )

    def _get(self, query: str, max_results: int) -> list[ExternalSnippet]:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme.lower() not in ("http", "https"):
            raise ProviderError(f"unsupported scheme in {URL_ENV}")
        sep = "&" if parsed.query else "?"
        url = f"{self.url}{sep}{urllib.parse.urlencode({'q': query, 'n': max_results})}"
        headers = {"Accept": "application/json", "User-Agent": "memspine-external/1"}
        if self.key:
            headers[self.header] = (
                f"Bearer {self.key}" if self.header.lower() == "authorization" else self.key
            )
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=self.timeout
            ) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderError(f"external search failed: {type(exc).__name__}") from exc
        out: list[ExternalSnippet] = []
        for item in _items(payload):
            text = clean_snippet(
                item.get("snippet")
                or item.get("content")
                or item.get("text")
                or item.get("description")
            )
            if text:
                out.append(
                    ExternalSnippet(
                        title=clean_snippet(item.get("title"), 120),
                        text=text,
                        url=clean_snippet(item.get("url"), 200),
                    )
                )
            if len(out) >= max_results:
                break
        return out

    async def search(self, query: str, max_results: int) -> list[ExternalSnippet]:
        return await asyncio.to_thread(self._get, query, max_results)
