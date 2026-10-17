"""isolation.owner_bound_handles: a single-owner handle's principal must own its namespace."""

from __future__ import annotations

import pytest

from memspine.api.engine import Engine
from memspine.api.handle import Memory
from memspine.scope import ScopeRefused


async def _engine(on: bool) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': ":memory:", 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        isolation={"owner_bound_handles": on},
    )
    await eng.start()
    return eng


async def test_flag_off_keeps_namespace_authority() -> None:
    eng = await _engine(False)
    try:
        Memory(eng, "acme/ilse", principal="bram")  # today's behaviour: allowed
    finally:
        await eng.stop()


async def test_flag_on_refuses_a_foreign_principal() -> None:
    eng = await _engine(True)
    try:
        with pytest.raises(ScopeRefused) as err:
            Memory(eng, "acme/ilse", principal="bram")
        assert err.value.reason == "cross_owner"
        Memory(eng, "acme/ilse", principal="ilse")  # the owner is fine
        Memory(eng, "acme/ilse")  # no principal (library use) is not affected
        Memory(eng, "acme", principal="bram")  # tenant-level namespace: no owner segment
    finally:
        await eng.stop()
