"""MOD-02: entry-point plug-ins -- discovery is not enablement (schema session 12)."""

from __future__ import annotations

import hashlib
import importlib.metadata
from typing import Any

import pytest

from memspine import plugins
from memspine.exceptions import ConfigError
from memspine.models.registry import EMBEDDERS
from memspine.schemas.public.plugins import DuplicateSlotError, LifetimeViolation


class FakeDist:
    def __init__(self, version: str = "1.2.3", record: str = "a,sha256=x,1\n") -> None:
        self.version = version
        self.metadata = {"Name": "acme-embed"}
        self._record = record

    def read_text(self, name: str) -> str | None:
        return self._record if name == "RECORD" else None


class FakeEP:
    def __init__(self, name: str, obj: Any, dist: FakeDist | None = None) -> None:
        self.name, self.value, self.dist, self._obj = name, f"acme:{name}", dist or FakeDist(), obj

    def load(self) -> Any:
        return self._obj


def _factory(cfg: Any) -> str:
    return "acme-embedder"


def _pin(**over: Any) -> dict[str, Any]:
    base = {
        "name": "acme",
        "version": "1.2.3",
        "slot": "embedders:acme",
        "lifetime": "app",
    }
    return {**base, **over}


@pytest.fixture(autouse=True)
def _clean() -> Any:
    plugins.disable_all()
    yield
    plugins.disable_all()


def _patch(monkeypatch: pytest.MonkeyPatch, groups: dict[str, list[FakeEP]]) -> None:
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda group=None: groups.get(group, [])
    )


def test_discovered_but_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _factory)]})
    found = plugins.discover(["embedders"])
    assert [(d.kind, d.name, d.version) for d in found] == [("embedders", "acme", "1.2.3")]
    assert "acme" not in EMBEDDERS
    assert plugins.enable([]) == []  # empty plugins: no-op
    assert "acme" not in EMBEDDERS
    assert plugins.enabled_manifest() == []


def test_enabled_with_pin_registers_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _factory)]})
    plugins.enable([_pin(version="==1.2.3")])
    assert EMBEDDERS.get("acme") is _factory
    assert not EMBEDDERS.is_builtin("acme")
    assert plugins.enabled_manifest() == [
        {
            "name": "acme",
            "version": "1.2.3",
            "hash": None,
            "slot": "embedders:acme",
            "lifetime": "app",
        }
    ]
    plugins.enable([_pin(version="==1.2.3")])  # same pins again (second engine): idempotent
    assert len(plugins.enabled_manifest()) == 1
    plugins.disable_all()
    assert "acme" not in EMBEDDERS


def test_wrong_version_pin_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _factory)]})
    with pytest.raises(plugins.PluginVersionError, match=r"1.0.0.*1.2.3"):
        plugins.enable([_pin(version="1.0.0")])
    assert "acme" not in EMBEDDERS


def test_not_advertised_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {})
    with pytest.raises(plugins.PluginNotFoundError):
        plugins.enable([_pin()])


def test_hash_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    record = "acme/__init__.py,sha256=abc,10\n"
    good = "sha256:" + hashlib.sha256(record.encode()).hexdigest()
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _factory, FakeDist(record=record))]})
    with pytest.raises(plugins.PluginHashError):
        plugins.enable([_pin(hash="sha256:" + "0" * 64)])
    plugins.enable([_pin(hash=good)])
    assert plugins.enabled_manifest()[0]["hash"] == good


def test_two_providers_one_slot_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        {"memspine.embedders": [FakeEP("acme", _factory), FakeEP("other", _factory)]},
    )
    # Same slot claimed by two plug-ins: the typed startup error names both.
    with pytest.raises(DuplicateSlotError, match=r"acme.*other"):
        plugins.enable([_pin(), _pin(name="other")])


def test_plugin_cannot_shadow_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("hash", _factory)]})
    with pytest.raises(DuplicateSlotError, match="built-in"):
        plugins.enable([_pin(name="hash", slot="embedders:hash")])
    assert EMBEDDERS.is_builtin("hash")


def test_two_entry_points_one_name_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = [FakeEP("acme", _factory), FakeEP("acme", _factory)]
    _patch(monkeypatch, {"memspine.embedders": eps})
    with pytest.raises(DuplicateSlotError):
        plugins.enable([_pin()])


def test_lifetime_mismatch_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def per_request(cfg: Any) -> None:  # declares a shorter lifetime than the pin
        return None

    per_request.__memspine_lifetime__ = "request"  # type: ignore[attr-defined]
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", per_request)]})
    with pytest.raises(plugins.PluginLifetimeError) as err:
        plugins.enable([_pin(lifetime="app")])
    assert isinstance(err.value, LifetimeViolation)
    assert "acme" not in EMBEDDERS
    plugins.enable([_pin(lifetime="request")])
    assert EMBEDDERS.lifetime_of("acme") == "request"


def test_bad_slot_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {})
    with pytest.raises(plugins.PluginError):
        plugins.enable([_pin(slot="nonsense")])
    with pytest.raises(plugins.PluginError):
        plugins.enable([_pin(slot="nokind:acme")])


def test_errors_are_config_errors() -> None:
    assert issubclass(plugins.PluginError, ConfigError)
    assert issubclass(plugins.UnknownProviderError, ConfigError)


def test_config_without_plugins_is_unchanged() -> None:
    from memspine.config.schema import MemspineConfig

    cfg = MemspineConfig()
    assert cfg.plugins == []
    cfg2 = MemspineConfig.model_validate({"plugins": [_pin()]})
    assert cfg2.plugins[0].slot == "embedders:acme"
    with pytest.raises(Exception, match="exact pin"):
        MemspineConfig.model_validate({"plugins": [_pin(version=">=1.0")]})


def test_plugin_embedder_selected_through_engine_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from memspine.models.registry import build_embedder

    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _factory)]})
    plugins.enable([_pin()])
    assert build_embedder(SimpleNamespace(provider="acme")) == "acme-embedder"
