"""RM-155: ``load_config`` validates the selected providers' settings and the deployment rules
(D-10: fail fast, naming the key path and the provider)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from typing import Any

import pytest

from memspine.config.loader import load_config, template_dir
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.locality import DeploymentLocalityError
from memspine.providers.validation import ProviderValidationError, validate_providers


def _llm(**settings: Any) -> dict[str, Any]:
    return {
        "provider": "openai_compatible",
        "timeout_s": 30.0,
        "settings": {"api_base": "http://test.invalid/v1", "model": "m", **settings},
    }


def _load(**overrides: Any) -> MemspineConfig:
    return load_config(template="base", env={}, overrides=overrides).config


def test_the_shipped_templates_validate() -> None:
    for path in sorted(template_dir().glob("*.yaml")):
        assert load_config(template=path.stem, env={}).config is not None, path.stem


def test_an_llm_settings_key_the_provider_does_not_take_names_the_role_and_provider() -> None:
    with pytest.raises(ProviderValidationError) as exc:
        _load(model={"llm": {"extract": _llm(base_url="http://old")}})
    message = str(exc.value)
    assert "model.llm.extract.settings" in message
    assert "'openai_compatible'" in message
    assert "base_url" in message


def test_an_escalation_target_is_validated_with_its_path() -> None:
    slot = _llm()
    slot["escalation"] = {
        "targets": [{**_llm(nope=1), "timeout_s": 30.0}],
        "max_tokens_per_attempt": 256,
        "timeout_s": 30.0,
    }
    with pytest.raises(ProviderValidationError, match=r"model\.llm\.extract\.escalation"):
        _load(model={"llm": {"extract": slot}})


def test_an_unknown_storage_provider_names_the_slot_and_the_valid_names() -> None:
    with pytest.raises(ProviderValidationError) as exc:
        _load(storage={"vector": {"provider": "nope", "settings": {}}})
    message = str(exc.value)
    assert "storage.vector.provider: unknown provider 'nope'" in message
    assert "lancedb" in message


def test_bad_storage_settings_name_the_slot() -> None:
    with pytest.raises(ProviderValidationError, match=r"storage\.cache\.settings"):
        _load(storage={"cache": {"provider": "memory", "settings": {"nope": 1}}})


def test_a_shared_provider_needs_deployment_id_at_load() -> None:
    embedding = {
        "provider": "ollama",
        "settings": {
            "api_base": "http://test.invalid",
            "model": "e",
            "dim": 8,
            "request_timeout_s": 5.0,
        },
    }
    with pytest.raises(ProviderValidationError) as exc:
        _load(model={"embedding": embedding})
    assert "model.embedding (provider 'ollama'): deployment.id is required" in str(exc.value)
    assert _load(model={"embedding": embedding}, deployment={"id": "d1"})


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(ProviderValidationError) as exc:
        _load(
            storage={"vector": {"provider": "nope", "settings": {}}},
            model={"llm": {"extract": _llm(base_url="x")}},
        )
    assert "storage.vector" in str(exc.value)
    assert "model.llm.extract" in str(exc.value)


def test_rerank_chain_settings_are_validated_with_their_index() -> None:
    with pytest.raises(ProviderValidationError) as exc:
        _load(model={"rerank": {"chain": [{"provider": "onnx", "settings": {"nope": 1}}]}})
    message = str(exc.value)
    assert "model.rerank.chain[0]" in message
    assert "nope" in message


def test_a_locality_violation_fails_at_load() -> None:
    with pytest.raises(DeploymentLocalityError, match="multi_node"):
        _load(deployment={"locality": "multi_node", "id": "d1"})


def test_a_provider_a_pinned_plugin_registers_is_left_to_the_build() -> None:
    vector = {"provider": "plugvec", "settings": {"x": 1}}
    pin = {
        "name": "plugvec",
        "slot": "vector_stores:plugvec",
        "version": "1.0.0",
        "lifetime": "app",
    }
    validate_providers(_load(storage={"vector": vector}, plugins=[pin]))
    with pytest.raises(ProviderValidationError, match="plugvec"):
        _load(storage={"vector": vector})


def test_errors_are_config_errors() -> None:
    assert issubclass(ProviderValidationError, ConfigError)


def test_validation_imports_no_optional_driver() -> None:
    """Descriptors expose their settings models without importing a driver (D-03)."""
    script = textwrap.dedent(
        """
        import sys
        from memspine.config.schema import MemspineConfig
        from memspine.providers.validation import validate_providers
        configs = [
            {"deployment": {"id": "d"}, "read": {"hybrid": True},
             "storage": {"vector": {"provider": "weaviate",
                                     "settings": {"url": "http://w.invalid"}},
                         "lexical": {"provider": "opensearch",
                                      "settings": {"url": "http://o.invalid"}},
                         "graph": {"provider": "neo4j",
                                    "settings": {"uri": "bolt://n.invalid"}},
                         "cache": {"provider": "redis",
                                    "settings": {"url": "redis://r.invalid"}}}},
            {"read": {"hybrid": True},
             "storage": {"vector": {"provider": "lancedb"},
                         "lexical": {"provider": "tantivy"},
                         "graph": {"provider": "ladybug"}}},
        ]
        for raw in configs:
            try:
                validate_providers(MemspineConfig.model_validate(raw))
            except Exception as exc:  # settings shape is not under test here
                print("validation:", type(exc).__name__, exc, file=sys.stderr)
        heavy = ["lancedb", "tantivy", "neo4j", "weaviate", "opensearchpy", "redis",
                 "pymongo", "psycopg", "asyncpg", "onnxruntime", "gliner2", "torch",
                 "transformers", "boto3", "real_ladybug", "cashews", "tokenizers", "pyarrow"]
        loaded = [name for name in heavy if name in sys.modules]
        print(",".join(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == "", result.stdout + result.stderr
