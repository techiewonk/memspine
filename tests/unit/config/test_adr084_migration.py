"""ADR-084 provider rejection and idempotent codemod. migrate_config_keys_adr084: skip"""

import pytest
from scripts.migrate_config_keys_adr084 import migrate_source

from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError


@pytest.mark.parametrize("slot", ["embedding", "rerank"])
def test_fastembed_rejected(slot: str) -> None:
    selection = {"provider": "fastembed", "settings": {}}
    value = selection if slot == "embedding" else {"chain": [selection]}
    with pytest.raises(ConfigError, match="onnx"):
        MemspineConfig.model_validate({"model": {slot: value}})


@pytest.mark.parametrize("suffix,source", [
    (".py", 'config = {"model": {"embedding": {"provider": "fastembed"}}}\n'),
    (".yaml", 'model:\n  rerank:\n    chain:\n      - provider: fastembed\n'),
    (".json", '{"model":{"embedding":{"provider":"fastembed"}}}'),
])
def test_migration_idempotent(suffix: str, source: str) -> None:
    target = migrate_source(source, suffix)
    assert "onnx" in target and "fastembed" not in target
    assert migrate_source(target, suffix) == target


def test_python_leaves_non_provider_strings_and_unicode_intact() -> None:
    source = 'x = {"label": "fastembed", "é": "🤖", "provider": "fastembed"} # preserved\n'
    assert migrate_source(source, ".py") == source.replace('"provider": "fastembed"', '"provider": "onnx"')
