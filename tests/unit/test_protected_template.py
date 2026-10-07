"""W1 (plan v3.2): the ``protected`` template turns the lineage protections on."""

from __future__ import annotations

from memspine import Engine


async def test_protected_template_loads_with_the_protections_on() -> None:
    eng = Engine(
        template="protected",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    await eng.start()
    try:
        cfg = eng._config()
    finally:
        await eng.stop()
    assert cfg.integrity.implicit_parents == "turn"
    assert cfg.firewall.skip_injected_recall is True
    assert cfg.firewall.tag_assistant_claims is True
    assert cfg.read.default_mode == "replay"  # combo-A from base is kept
