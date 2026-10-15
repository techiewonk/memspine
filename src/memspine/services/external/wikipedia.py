"""E03 provider: the MediaWiki search API plus the REST page summary. No key.

Receives only the already privacy-filtered generic query text (the broker applies the
privacy guard before any provider call; the query is re-asserted generic here too).
Wikimedia policy: an identifying ``User-Agent``, modest request rate, honest timeout.

``search``: ``GET /w/api.php?action=query&list=search&srsearch=<q>`` for titles, then
``GET /api/rest_v1/page/summary/<title>`` for a short extract of each. Citations are the
page title and canonical URL.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from memspine.services.external.provider import (
    ExternalSnippet,
    ProviderError,
    clean_snippet,
)

__all__ = ["WikipediaProvider"]

USER_AGENT = (
    "memspine-research/1.0 (https://github.com/hemprasad/memspine; "
    "hemprasad.badagujar@gmail.com) python-urllib"
)
API = "https://en.wikipedia.org/w/api.php"
SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"

#: A transport maps (url, headers, timeout) to the raw response body.
Transport = Callable[[str, dict[str, str], float], bytes]


def _urllib_transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=timeout
        ) as response:
            return bytes(response.read(2_000_000))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"wikipedia request failed: {type(exc).__name__}") from exc


class WikipediaProvider:
    name = "wikipedia"

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        user_agent: str = USER_AGENT,
        timeout: float = 10.0,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport or _urllib_transport
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last = -1e9
        self._lock = threading.Lock()

    def _get_json(self, url: str) -> Any:
        with self._lock:  # rate limit: at least min_interval_s between requests
            wait = self.min_interval_s - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        raw = self._transport(url, headers, self.timeout)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ProviderError("wikipedia returned non-JSON") from exc

    def _search(self, query: str, max_results: int) -> list[ExternalSnippet]:
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
        payload = self._get_json(f"{API}?{params}")
        hits = ((payload or {}).get("query") or {}).get("search") or []
        out: list[ExternalSnippet] = []
        for hit in hits:
            title = str(hit.get("title") or "").strip()
            if not title:
                continue
            slug = urllib.parse.quote(title.replace(" ", "_"), safe="")
            try:
                page = self._get_json(SUMMARY + slug)
            except ProviderError:
                continue  # one missing summary does not fail the lookup
            text = clean_snippet((page or {}).get("extract"), 500)
            if not text:
                continue
            url = (
                (((page or {}).get("content_urls") or {}).get("desktop") or {}).get("page")
                or f"https://en.wikipedia.org/wiki/{slug}"
            )
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

    async def search(self, query: str, max_results: int) -> list[ExternalSnippet]:
        return await asyncio.to_thread(self._search, query, max_results)
