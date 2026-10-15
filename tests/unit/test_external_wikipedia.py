"""E03 Wikipedia provider: fake transport only (no network)."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, unquote, urlparse

from memspine.services.external import (
    EvidenceCache,
    ExternalBroker,
    ProviderError,
    WikipediaProvider,
)


class FakeWiki:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> bytes:
        self.calls.append((url, headers))
        parsed = urlparse(url)
        if parsed.path == "/w/api.php":
            hits = [{"title": "Border Collie"}, {"title": "Dog [training]"}, {"title": "Gone"}]
            return json.dumps({"query": {"search": hits}}).encode()
        if parsed.path.endswith("/Gone"):
            raise ProviderError("404")
        title = unquote(parsed.path.rsplit("/", 1)[1]).replace("_", " ")
        return json.dumps(
            {
                "title": title,
                "extract": f"{title} is a [breed] known for\nintelligence. " + "x" * 800,
                "content_urls": {"desktop": {"page": f"https://en.wikipedia.org/wiki/{title}"}},
            }
        ).encode()


def _provider(fake: FakeWiki, **kw: object) -> WikipediaProvider:
    return WikipediaProvider(transport=fake, min_interval_s=0.0, **kw)  # type: ignore[arg-type]


def test_search_then_summary_with_citations_and_user_agent() -> None:
    fake = FakeWiki()
    out = asyncio.run(_provider(fake).search("border collie temperament", 3))
    first = parse_qs(urlparse(fake.calls[0][0]).query)
    assert first["srsearch"] == ["border collie temperament"] and first["list"] == ["search"]
    assert all("memspine" in h["User-Agent"] and "@" in h["User-Agent"] for _, h in fake.calls)
    assert [s.title for s in out] == ["Border Collie", "Dog (training)"]  # failed summary skipped
    assert out[0].url == "https://en.wikipedia.org/wiki/Border Collie"
    assert "[" not in out[0].text and "\n" not in out[0].text and len(out[0].text) <= 500


def test_respects_max_results() -> None:
    out = asyncio.run(_provider(FakeWiki()).search("dogs", 1))
    assert len(out) == 1


def test_rate_limit_spaces_requests() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    p = WikipediaProvider(
        transport=FakeWiki(), min_interval_s=1.0, clock=lambda: now[0], sleep=sleep
    )
    asyncio.run(p.search("dogs", 2))
    assert len(slept) >= 2 and all(abs(s - 1.0) < 1e-9 for s in slept)


def test_non_json_is_a_provider_error() -> None:
    p = WikipediaProvider(transport=lambda u, h, t: b"<html>", min_interval_s=0.0)
    try:
        asyncio.run(p.search("dogs", 1))
    except ProviderError:
        return
    raise AssertionError("expected ProviderError")


def test_broker_filters_names_before_wikipedia_and_caches(tmp_path) -> None:
    fake = FakeWiki()
    broker = ExternalBroker(_provider(fake), EvidenceCache(tmp_path), max_calls=5)
    q = "Would Melanie like a border collie temperament?"
    first = asyncio.run(broker.lookup(q, ["Melanie"], allow_network=True))
    assert first.status == "ok" and first.snippets
    sent = " ".join(parse_qs(urlparse(u).query).get("srsearch", [""])[0] for u, _ in fake.calls)
    assert "melanie" not in sent.lower()
    n = len(fake.calls)
    again = asyncio.run(broker.lookup(q, ["Melanie"], allow_network=True))
    assert again.status == "cache_hit" and len(fake.calls) == n
    assert (tmp_path / "external_cache.json").exists()