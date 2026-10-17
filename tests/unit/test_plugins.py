"""Pinned plugins register typed provider descriptors after all pins are validated."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
from dataclasses import dataclass
from typing import Any

import pytest

from memspine import plugins
from memspine.exceptions import ConfigError
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import EMBEDDERS, ProviderDescriptor
from memspine.providers.settings import EmptySettings
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


@dataclass(frozen=True)
class _Builder:
    name: str = "acme"

    async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[str]:
        return BuildResult(
            f"{self.name}-embedder",
            ProviderRef("embedding", self.name),
            (),
            frozenset({"embedding"}),
        )


def _descriptor(name: str = "acme", *, version: int = 1) -> ProviderDescriptor:
    return ProviderDescriptor(
        ref=ProviderRef("embedding", name, version),
        settings_model=EmptySettings,
        declared_capabilities=frozenset({"embedding"}),
        requirements=(),
        builder=_Builder(name),
    )


def _old_factory(cfg: Any) -> str:
    return "acme-embedder"


def _pin(**over: Any) -> dict[str, Any]:
    base = {"name": "acme", "version": "1.2.3", "slot": "embedders:acme", "lifetime": "app"}
    return {**base, **over}


@pytest.fixture(autouse=True)
def _clean() -> Any:
    plugins.disable_all()
    yield
    plugins.disable_all()


def _patch(monkeypatch: pytest.MonkeyPatch, groups: dict[str, list[FakeEP]]) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group=None: groups.get(group, []))


def test_discovered_but_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor())]})
    found = plugins.discover(["embedders"])
    assert [(d.kind, d.name, d.version) for d in found] == [("embedders", "acme", "1.2.3")]
    assert "acme" not in EMBEDDERS
    assert plugins.enable([]) == []
    assert "acme" not in EMBEDDERS
    assert plugins.enabled_manifest() == []


def test_enabled_with_pin_registers_descriptor_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor())]})
    plugins.enable([_pin(version="==1.2.3")])
    assert EMBEDDERS.get("acme") == _descriptor()
    assert not EMBEDDERS.is_builtin("acme")
    assert plugins.enabled_manifest() == [
        {"name": "acme", "version": "1.2.3", "hash": None, "slot": "embedders:acme", "lifetime": "app"}
    ]
    plugins.enable([_pin(version="==1.2.3")])
    assert len(plugins.enabled_manifest()) == 1
    plugins.disable_all()
    assert "acme" not in EMBEDDERS


def test_wrong_version_pin_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor())]})
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
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor(), FakeDist(record=record))]})
    with pytest.raises(plugins.PluginHashError):
        plugins.enable([_pin(hash="sha256:" + "0" * 64)])
    plugins.enable([_pin(hash=good)])
    assert plugins.enabled_manifest()[0]["hash"] == good


def test_two_providers_one_slot_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        {"memspine.embedders": [FakeEP("acme", _descriptor()), FakeEP("other", _descriptor("other"))]},
    )
    with pytest.raises(DuplicateSlotError, match=r"acme.*other"):
        plugins.enable([_pin(), _pin(name="other")])


def test_plugin_cannot_shadow_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("hash", _descriptor("hash"))]})
    with pytest.raises(DuplicateSlotError, match="built-in"):
        plugins.enable([_pin(name="hash", slot="embedders:hash")])
    assert EMBEDDERS.is_builtin("hash")


def test_two_entry_points_one_name_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = [FakeEP("acme", _descriptor()), FakeEP("acme", _descriptor())]
    _patch(monkeypatch, {"memspine.embedders": eps})
    with pytest.raises(DuplicateSlotError):
        plugins.enable([_pin()])


def test_lifetime_mismatch_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProviderDescriptor, "__memspine_lifetime__", "request", raising=False)
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor())]})
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


def test_provider_contract_version_and_identity_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor(version=2))]})
    with pytest.raises(plugins.PluginContractError, match="contract version 2"):
        plugins.enable([_pin()])
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor("wrong"))]})
    with pytest.raises(plugins.PluginContractError, match="identity"):
        plugins.enable([_pin()])


def test_old_provider_factory_shape_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _old_factory)]})
    with pytest.raises(plugins.PluginContractError, match="ProviderDescriptor"):
        plugins.enable([_pin()])
    assert "acme" not in EMBEDDERS


def test_config_without_plugins_is_unchanged() -> None:
    from memspine.config.schema import MemspineConfig

    runtime = {
        "provider_services": {
            "health_timeout_s": 5.0,
            "close_timeout_s": 10.0,
            "startup_timeout_s": None,
        }
    }
    cfg = MemspineConfig(runtime=runtime)
    assert cfg.plugins == []
    cfg2 = MemspineConfig.model_validate({"runtime": runtime, "plugins": [_pin()]})
    assert cfg2.plugins[0].slot == "embedders:acme"
    with pytest.raises(Exception, match="exact pin"):
        MemspineConfig.model_validate({"runtime": runtime, "plugins": [_pin(version=">=1.0")]})


def test_plugin_descriptor_builds_through_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, {"memspine.embedders": [FakeEP("acme", _descriptor())]})
    plugins.enable([_pin()])
    result = asyncio.run(EMBEDDERS.get("acme").builder.build(EmptySettings(), None))
    assert result.service == "acme-embedder"
