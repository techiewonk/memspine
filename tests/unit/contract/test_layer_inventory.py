"""Runtime inventory distinguishes configured producers, rows and marker artefacts."""

from datetime import UTC, datetime

import pytest

from memspine import Engine
from memspine.derive.pipeline.stages.profile_card import card_to_dict
from memspine.derive.profile import ProfileCard, ProfileEntry, SupportRef
from memspine.read.pipeline.request import layer_inventory
from memspine.store.events import EventKind, MemoryEvent


async def test_runtime_inventory_counts_latest_card_without_inventing_record_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        offline={"session_close_v2": True, "profile_card_v2": True, "patterns_v2": True},
        read={"legs_v2": True, "episode_leg_v2": True},
    )
    await eng.start()
    try:
        parent = await eng.write(
            "Noor prefers quiet trails.", namespace="inventory", memory_type="episodic"
        )
        now = datetime.now(UTC)
        entry = ProfileEntry(
            "preferences", "trails", "quiet", (SupportRef(parent.record_id, 1.0),), 1, now, now
        )
        card = ProfileCard("inventory", "inventory", (entry,))
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="inventory",
                actor="system",
                payload={"marker": "profile_card_v2", "card": card_to_dict(card)},
            )
        )
        reg = await layer_inventory(eng, "inventory", eng._config().read, selected=["L2"])
        assert reg.is_configured("L2") and reg.is_configured("L4") and reg.is_configured("L5")
        assert reg.count("L5") == 0 and reg.artifact_count("L5") == 1
        assert reg.is_selected("L2") and not reg.is_selected("L5")
        # Verify the actual search wiring uses the same inventory, not an empty template.
        from memspine.read import legs as lib

        observed = []
        run = lib.run_legs

        async def capture(registry, names, ctx, **kwargs):
            observed.append(ctx.layer_registry)
            return await run(registry, names, ctx, **kwargs)

        monkeypatch.setattr(lib, "run_legs", capture)
        await eng.search("quiet trails", namespace="inventory")
        assert observed and observed[0].is_configured("L4")
        assert observed[0].artifact_count("L5") == 1
        assert observed[0].is_selected("L2")
        # A scrubbed latest card replaces the prior snapshot, rather than counting historical markers.
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="inventory",
                actor="system",
                payload={
                    "marker": "profile_card_v2",
                    "card": card_to_dict(ProfileCard("inventory", "inventory")),
                },
            )
        )
        refreshed = await layer_inventory(eng, "inventory", eng._config().read)
        assert refreshed.artifact_count("L5") == 0
    finally:
        await eng.stop()
