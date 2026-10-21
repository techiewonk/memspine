"""PA-046: selected providers may declare modules to import ahead of build()."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from memspine.providers.builtin.vector import LanceDBSettings
from memspine.providers.contracts import ProviderRef
from memspine.providers.registry import ProviderDescriptor, get_registry
from memspine.runtime import container


class _Builder:
    async def build(self, settings: Any, ctx: Any) -> Any:  # pragma: no cover - never built
        raise AssertionError


def _descriptor(**kw: Any) -> ProviderDescriptor:
    return ProviderDescriptor(
        ProviderRef("vector", "prewarm_fixture"), LanceDBSettings, frozenset(), (), _Builder(), **kw
    )


@pytest.mark.parametrize("bad", [["lancedb"], ("",), (" lancedb",), (1,)])
def test_prewarm_modules_must_be_a_tuple_of_module_names(bad: Any) -> None:
    with pytest.raises(TypeError, match="prewarm_modules"):
        _descriptor(prewarm_modules=bad)


def test_lancedb_descriptor_declares_its_slow_import() -> None:
    assert get_registry("vector_stores").get("lancedb").prewarm_modules == ("lancedb",)


def _config(provider: str) -> Any:
    return SimpleNamespace(vector=SimpleNamespace(provider=provider))


def test_unknown_provider_is_left_to_the_build_error_path() -> None:
    assert container.prewarm_provider_imports(_config("no-such-provider")) is None


def test_already_imported_modules_start_no_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "lancedb", sys.modules.get("lancedb", object()))
    assert container.prewarm_provider_imports(_config("lancedb")) is None


def test_missing_and_failing_modules_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptor = _descriptor(prewarm_modules=("memspine_no_such_module_xyz", "json"))
    monkeypatch.setattr(container, "_descriptor", lambda *_a: descriptor)
    monkeypatch.delitem(sys.modules, "json", raising=False)
    calls: list[str] = []

    def fake_import(name: str) -> Any:
        calls.append(name)
        raise ImportError(name)

    monkeypatch.setattr(container.importlib, "import_module", fake_import)
    thread = container.prewarm_provider_imports(_config("lancedb"))
    assert thread is not None
    thread.join(timeout=5)
    assert calls == ["json"]  # absent package skipped by find_spec; import error swallowed
