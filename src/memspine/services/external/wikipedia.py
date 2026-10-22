"""E03 provider: the MediaWiki search API plus the REST page summary. No key.

Receives only the already privacy-filtered generic query text (the broker applies the
privacy guard before any provider call; the query is re-asserted generic here too).
Wikimedia policy: an identifying ``User-Agent``, modest request rate, honest timeout. The
transport is the Engine's shared :class:`~memspine.clients.http.HttpConnector` (RM-113), or an
injected callable (tests).

``search``: ``GET /w/api.php?action=query&list=search&srsearch=<q>`` for titles, then
``GET /api/rest_v1/page/summary/<title>`` for a short extract of each. Citations are the
page title and canonical URL.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from memspine.clients.http import HttpConnector, HttpError
from memspine.services.external.provider import (
    ExternalSnippet,
    ProviderError,
    clean_snippet,
)

__all__ = ["WikipediaProvider"]

USER_AGENT = (
    "memspine-research/1.0 (https://github.com/hemprasad/memspine; "
    "hemprasad.badagujar@gmail.com) python-httpx"
)
API = "https://en.wikipedia.org/w/api.php"
SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
#: The largest response body read (bytes).
MAX_BODY_BYTES = 2_000_000

#: A transport maps (url, headers, timeout) to the raw response body (sync or async).
Transport = Callable[[str, dict[str, str], float], bytes | Awaitable[bytes]]


def http_transport(http: HttpConnector) -> Transport:
    """The transport over a shared HTTP connector: any failure is a :class:`ProviderError`."""

    async def get(url: str, headers: dict[str, str], deadline_s: float) -> bytes:
        try:
            reply = await http.request(
                "GET", url, headers=headers, timeout_s=deadline_s, max_bytes=MAX_BODY_BYTES
            )
        except HttpError as exc:
            raise ProviderError(f"wikipedia request failed: {exc}") from exc
        if reply.status >= 400:
            raise ProviderError(f"wikipedia request failed: HTTP {reply.status}")
        return reply.body[:MAX_BODY_BYTES]

    return get


class WikipediaProvider:
    name = "wikipedia"

    def __init__(
        self,
        *,
        http: HttpConnector | None = None,
        transport: Transport | None = None,
        user_agent: str = USER_AGENT,
        timeout: float = 10.0,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if transport is None:
            if http is None:
                raise ValueError("WikipediaProvider needs an HTTP connector or a transport")
            transport = http_transport(http)
        self._transport = transport
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last = -1e9

    async def _get_json(self, url: str) -> Any:
        # Rate limit: at least min_interval_s between request starts. The slot is reserved
        # before any await, so concurrent searches on one loop queue up correctly.
        now = self._clock()
        start = max(now, self._last + self.min_interval_s)
        self._last = start
        if start > now:
            await asyncio.to_thread(self._sleep, start - now)
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        raw = self._transport(url, headers, self.timeout)
        if inspect.isawaitable(raw):
            raw = await raw
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ProviderError("wikipedia returned non-JSON") from exc

    async def search(self, query: str, max_results: int) -> list[ExternalSnippet]:
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": max_results,
                "srprop": "",
                "format": "json",
                "formatversion": 2,
            }
        )
        payload = await self._get_json(f"{API}?{params}")
        hits = ((payload or {}).get("query") or {}).get("search") or []
        out: list[ExternalSnippet] = []
        for hit in hits:
            title = str(hit.get("title") or "").strip()
            if not title:
                continue
            slug = urllib.parse.quote(title.replace(" ", "_"), safe="")
            try:
                page = await self._get_json(SUMMARY + slug)
            except ProviderError:
                continue  # one missing summary does not fail the lookup
            text = clean_snippet((page or {}).get("extract"), 500)
            if not text:
                continue
            url = (((page or {}).get("content_urls") or {}).get("desktop") or {}).get(
                "page"
            ) or f"https://en.wikipedia.org/wiki/{slug}"
            out.append(
                ExternalSnippet(
                    title=clean_snippet((page or {}).get("title") or title, 120),
                    text=text,
                    url=clean_snippet(url, 200),
                )
            )
            if len(out) >= max_results:
                break
        return out
