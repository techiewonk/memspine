"""LadybugDB FTS extension: installed on first use, degraded (never fatal) when unavailable.

Regression: a fresh environment had FTS neither installed nor loaded, ``LOAD FTS`` raised, and
every entity-embedding write failed (graph.entity_embedding_failed in all GC eval runs).
"""

from __future__ import annotations

import asyncio

from memspine.services.graph import ladybug as ladybug_mod


class _Fake:
    """Stands in for the store: only ``_execute`` and the FTS state are used."""

    def __init__(self, installable: bool) -> None:
        self.calls: list[str] = []
        self.installed = False
        self.installable = installable
        self._fts_ok = None

    async def _execute(self, query: str, params: dict) -> list:
        self.calls.append(query)
        if query == "INSTALL FTS":
            if not self.installable:
                raise RuntimeError("network unavailable")
            self.installed = True
            return []
        if query == "LOAD FTS" and not self.installed:
            raise RuntimeError("Extension: fts has not been installed.")
        return []


def _load(fake: _Fake) -> bool:
    store_cls = next(
        v for v in vars(ladybug_mod).values() if isinstance(v, type) and hasattr(v, "_load_fts")
    )
    return asyncio.run(store_cls._load_fts(fake))


def test_fts_installed_on_first_use() -> None:
    fake = _Fake(installable=True)
    assert _load(fake) is True
    assert fake.calls == ["LOAD FTS", "INSTALL FTS", "LOAD FTS"]
    # remembered: no second round trip
    assert _load(fake) is True and len(fake.calls) == 3


def test_fts_unavailable_degrades_instead_of_failing() -> None:
    fake = _Fake(installable=False)
    assert _load(fake) is False
    assert fake._fts_ok is False
