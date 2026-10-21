"""Focused coverage for the pytest-asyncio loop-factory hook."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_ROOT_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"
_ROOT_CONFTEST_SPEC = importlib.util.spec_from_file_location(
    "memspine_root_tests_conftest", _ROOT_CONFTEST
)
assert _ROOT_CONFTEST_SPEC is not None and _ROOT_CONFTEST_SPEC.loader is not None
conftest = importlib.util.module_from_spec(_ROOT_CONFTEST_SPEC)
_ROOT_CONFTEST_SPEC.loader.exec_module(conftest)


def _item(module_name: str, name: str = "test_example") -> SimpleNamespace:
    return SimpleNamespace(module=SimpleNamespace(__name__=module_name), name=name)


def test_windows_live_postgres_uses_selector_loop_factory(monkeypatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.setenv("MEMSPINE_TEST_PG_URL", "postgresql://localhost/test")

    factories = conftest.pytest_asyncio_loop_factories(None, _item("test_storage_backends"))

    assert factories == {"windows_selector": asyncio.SelectorEventLoop}
    loop = factories["windows_selector"]()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_selector_factory_requires_live_postgres_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.delenv("MEMSPINE_TEST_PG_URL", raising=False)
    monkeypatch.delenv("MEMSPINE_TEST_POSTGRES_URL", raising=False)
    monkeypatch.delenv("MEMSPINE_TEST_LIVE_POSTGRES_URL", raising=False)

    factories = conftest.pytest_asyncio_loop_factories(None, _item("test_storage_backends"))

    assert factories == {"default": asyncio.new_event_loop}


def test_selector_factory_is_limited_to_postgres_modules(monkeypatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.setenv("MEMSPINE_TEST_PG_URL", "postgresql://localhost/test")

    factories = conftest.pytest_asyncio_loop_factories(None, _item("test_regular_async_tests"))

    assert factories == {"default": asyncio.new_event_loop}


def test_windows_native_pgvector_contract_uses_live_postgres_url(monkeypatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.setenv("MEMSPINE_TEST_LIVE_POSTGRES_URL", "postgresql://localhost/test")

    factories = conftest.pytest_asyncio_loop_factories(
        None, _item("test_pgvector_contract", "test_pgvector_native_projection_contract")
    )

    assert factories == {"windows_selector": asyncio.SelectorEventLoop}


def test_windows_provider_postgres_contract_is_selected_by_test_name(monkeypatch) -> None:
    monkeypatch.setattr(conftest.sys, "platform", "win32")
    monkeypatch.setenv("MEMSPINE_TEST_LIVE_POSTGRES_URL", "postgresql://localhost/test")

    postgres = conftest.pytest_asyncio_loop_factories(
        None, _item("test_provider_live_contracts", "test_postgres_storage_contract")
    )
    remote = conftest.pytest_asyncio_loop_factories(
        None, _item("test_provider_live_contracts", "test_remote_search_contract")
    )

    assert postgres == {"windows_selector": asyncio.SelectorEventLoop}
    assert remote == {"default": asyncio.new_event_loop}


async def test_pytest_asyncio_executes_with_hook_factory() -> None:
    """The pytest-asyncio hook is exercised on actual async test collection."""
    assert asyncio.get_running_loop().is_running()
