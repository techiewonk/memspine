import asyncio
import hashlib
import importlib.metadata as metadata
import json
import pathlib
import sys

from memspine import Engine, plugins
from memspine.providers.registry import EMBEDDERS, ProviderRegistryError


async def main():
    dist = metadata.distribution("memspine-provider-plugin-fixture")
    record = dist.read_text("RECORD")
    assert record is not None
    digest = "sha256:" + hashlib.sha256(record.encode("utf-8")).hexdigest()
    discovered = {
        p.name: p for p in plugins.discover(["embedders"]) if p.name.startswith("fixture_")
    }
    assert set(discovered) == {"fixture_hash", "fixture_bad_contract"}
    assert discovered["fixture_hash"].version == "1.2.3"
    assert discovered["fixture_bad_contract"].version == "1.2.3"
    assert "fixture_hash" not in EMBEDDERS
    assert "memspine_provider_plugin_fixture" not in sys.modules

    mismatch_results = []
    for manifest, error_type, label in (
        (
            {
                "name": "fixture_hash",
                "version": "0.0.0",
                "slot": "embedders:fixture_hash",
                "lifetime": "app",
            },
            plugins.PluginVersionError,
            "version",
        ),
        (
            {
                "name": "fixture_hash",
                "version": "1.2.3",
                "hash": "sha256:" + "0" * 64,
                "slot": "embedders:fixture_hash",
                "lifetime": "app",
            },
            plugins.PluginHashError,
            "hash",
        ),
    ):
        try:
            plugins.enable([manifest])
        except error_type as exc:
            mismatch_results.append(
                {"check": label, "result": "rejected", "error": type(exc).__name__}
            )
        else:
            raise AssertionError(f"{label} mismatch was accepted")
        assert "fixture_hash" not in EMBEDDERS
    assert "memspine_provider_plugin_fixture" not in sys.modules

    bad_contract_pin = {
        "name": "fixture_bad_contract",
        "version": "1.2.3",
        "hash": digest,
        "slot": "embedders:fixture_bad_contract",
        "lifetime": "app",
    }
    try:
        plugins.enable([bad_contract_pin])
    except plugins.PluginContractError as exc:
        assert "contract version 2" in str(exc)
        contract_check = {
            "check": "unsupported_contract",
            "result": "rejected",
            "error": type(exc).__name__,
        }
    else:
        raise AssertionError("unsupported provider contract version was accepted")
    from memspine_provider_plugin_fixture import CONTRACT_V2_BUILD_EVENTS

    assert CONTRACT_V2_BUILD_EVENTS == [], CONTRACT_V2_BUILD_EVENTS
    assert "fixture_bad_contract" not in EMBEDDERS

    pin = {
        "name": "fixture_hash",
        "version": "==1.2.3",
        "hash": digest,
        "slot": "embedders:fixture_hash",
        "lifetime": "app",
    }
    engine = Engine(
        template="core",
        dotenv_path=None,
        user_config={"plugins": [pin]},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "fixture_hash", "settings": {"dim": 64}}},
    )
    await engine.start()
    try:
        embedder_id = engine._embedder.embedder_id
        assert embedder_id == "fixture_hash:64", embedder_id
        written = await engine.write(
            "plugin wheel smoke: amber fox at a bridge", namespace="plugin-wheel"
        )
        matches = await engine.search("amber fox bridge", namespace="plugin-wheel", top_k=3)
        assert any(record.record_id == written.record_id for record, _ in matches)
        enabled = plugins.enabled_manifest()
        assert enabled == [pin | {"version": "1.2.3"}]
        from memspine_provider_plugin_fixture import CLOSE_EVENTS

        assert CLOSE_EVENTS == []
    finally:
        await engine.stop()
    from memspine_provider_plugin_fixture import CLOSE_EVENTS

    assert CLOSE_EVENTS == ["closed"], CLOSE_EVENTS

    try:
        from memspine.providers.registry import validate_settings

        validate_settings(EMBEDDERS.get("fixture_hash"), {"dim": 64, "unexpected": "rejected"})
    except ProviderRegistryError as exc:
        strict_settings = {"result": "rejected", "error": type(exc).__name__, "message": str(exc)}
    else:
        raise AssertionError("unknown plugin setting was accepted")

    print(
        json.dumps(
            {
                "result": "PASS",
                "python": sys.version.split()[0],
                "memspine_file": str(pathlib.Path(__import__("memspine").__file__).resolve()),
                "plugin_distribution": dist.metadata["Name"],
                "plugin_version": dist.version,
                "entrypoint": discovered["fixture_hash"].value,
                "record_hash": digest,
                "discovery_without_enable": True,
                "pin_mismatch_checks": mismatch_results,
                "unsupported_contract_check": contract_check,
                "unsupported_contract_builder_calls": CONTRACT_V2_BUILD_EVENTS,
                "engine_embedder_id": embedder_id,
                "write_search_match": True,
                "shutdown_close_events": CLOSE_EVENTS,
                "strict_settings": strict_settings,
                "enabled_manifest": enabled,
            },
            indent=2,
        )
    )


asyncio.run(main())
