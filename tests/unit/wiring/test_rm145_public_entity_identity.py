"""RM-145: public identity verbs use the canonical evidence-backed marker path."""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine import Engine
from memspine.exceptions import ConflictError, MemspineError, MissingServiceError
from memspine.memories.associative.entity_index import ALIAS_MARKER, MERGE_MARKER
from memspine.read.pipeline import scope_guard
from memspine.scope import ScopeRefused
from memspine.store.events import EventKind
from memspine.trust.privacy import principal_scope


async def _engine(
    path: str = ":memory:", *, associative: bool = True, viewer_required: bool = False
) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": path}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": associative, "policies": {"entity_nodes": True}},
        },
        read={"hybrid": True, "record_access": False},
        isolation={"viewer_required": viewer_required},
    )
    await eng.start()
    return eng


async def _ids(eng: Engine, namespace: str, name: str) -> list[str]:
    assert eng._associative is not None
    return await (await eng._associative.entity_lookup(namespace)).get(name)


async def _markers(eng: Engine) -> list[object]:
    return [
        e
        for e in await eng._require_started().read_events(0, 10_000)
        if e.kind is EventKind.MARKER and e.payload.get("marker") in {ALIAS_MARKER, MERGE_MARKER}
    ]


async def test_public_merge_alias_lookup_retrieval_reopen_rebuild_and_erasure(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "identity.db")
    eng = await _engine(path)
    try:
        mel = await eng.write("Mel paints", namespace="a", entity="Mel")
        melanie = await eng.write("Melanie sings", namespace="a", entity="Melanie")
        evidence = await eng.write("Mel is another name for Melanie", namespace="a")
        [mel_id], [melanie_id] = await _ids(eng, "a", "mel"), await _ids(eng, "a", "melanie")
        await eng.merge_entities(melanie_id, mel_id, [evidence.record_id], namespace="a")
        await eng.add_entity_alias(melanie_id, "Quetzalia", [evidence.record_id], namespace="a")
        assert await _ids(eng, "a", "quetzalia") == [melanie_id]
        assert melanie_id in await eng._graph_seeds("a", "Quetzalia")
        assert set(await eng._graph_seeds("a", "melanie")) == {mel_id, melanie_id}
        hits = await eng.search("Quetzalia", namespace="a", top_k=8)
        assert melanie.record_id in {r.record_id for r, _ in hits}
        assert len(await _markers(eng)) == 2
    finally:
        await eng.stop()

    eng = await _engine(path)
    try:
        assert await _ids(eng, "a", "quetzalia") == [melanie_id]
        await eng.rebuild()
        assert await _ids(eng, "a", "quetzalia") == [melanie_id]
        await eng.forget(evidence.record_id, namespace="a", hard=True)
        assert await _ids(eng, "a", "quetzalia") == []
        assert await eng._graph_seeds("a", "melanie") == [melanie_id]
        alias_markers = [e for e in await _markers(eng) if e.payload.get("marker") == ALIAS_MARKER]
        assert alias_markers and all("Quetzalia" not in str(e.payload) for e in alias_markers)
        await eng.rebuild()
        assert await _ids(eng, "a", "quetzalia") == []
    finally:
        await eng.stop()


async def test_public_identity_rejects_invalid_foreign_and_hidden_evidence_without_markers() -> (
    None
):
    eng = await _engine()
    try:
        await eng.write("Mel paints", namespace="a", entity="Mel")
        await eng.write("Melanie sings", namespace="a", entity="Melanie")
        [mel_id], [melanie_id] = await _ids(eng, "a", "mel"), await _ids(eng, "a", "melanie")
        live = await eng.write("the names refer to one person", namespace="a")
        foreign = await eng.write("private source", namespace="b")
        await eng.write("Bob travels", namespace="b", entity="Bob")
        [foreign_entity] = await _ids(eng, "b", "bob")
        hidden = await eng.write(
            "only Bob knows this identity",
            namespace="a",
            participants=["bob"],
            visibility="private",
        )
        for evidence in ([], ["missing"], [foreign.record_id]):
            with pytest.raises(ConflictError):
                await eng.merge_entities(melanie_id, mel_id, evidence, namespace="a")
        with pytest.raises(ConflictError):
            await eng.merge_entities(mel_id, mel_id, [live.record_id], namespace="a")
        with pytest.raises(ConflictError):
            await eng.add_entity_alias(mel_id, "  ", [live.record_id], namespace="a")
        with pytest.raises(ConflictError):
            await eng.add_entity_alias(foreign_entity, "Mels", [live.record_id], namespace="a")
        with principal_scope("alice"), pytest.raises(ConflictError, match="no such record"):
            await eng.add_entity_alias(mel_id, "Mels", [hidden.record_id], namespace="a")
        assert await _markers(eng) == []
    finally:
        await eng.stop()


async def test_public_identity_requires_started_associative_engine() -> None:
    eng = Engine(template="core", dotenv_path=None)
    with pytest.raises(MemspineError, match="not started"):
        await eng.merge_entities("keep", "absorb", ["citation"])
    disabled = await _engine(associative=False)
    try:
        with pytest.raises(MissingServiceError, match="associative entity identity"):
            await disabled.add_entity_alias("entity", "alias", ["citation"])
    finally:
        await disabled.stop()


@pytest.mark.parametrize("verb", ["merge", "alias"])
async def test_public_identity_cannot_mutate_an_entity_owned_only_by_hidden_records(
    verb: str,
) -> None:
    """A visible citation does not authorize changing another viewer's private entity."""
    eng = await _engine(viewer_required=True)
    try:
        await eng.write("Alice paints", namespace="a", entity="Alice")
        await eng.write(
            "Bob travels",
            namespace="a",
            entity="Bob",
            participants=["bob"],
            visibility="private",
        )
        evidence = await eng.write("Alice knows this public fact", namespace="a")
        [alice_id] = await _ids(eng, "a", "alice")
        [bob_id] = await _ids(eng, "a", "bob")
        with principal_scope("alice"), pytest.raises(ConflictError, match="no such entity"):
            if verb == "merge":
                await eng.merge_entities(alice_id, bob_id, [evidence.record_id], namespace="a")
            else:
                await eng.add_entity_alias(bob_id, "Bobby", [evidence.record_id], namespace="a")
        assert await _markers(eng) == []
    finally:
        await eng.stop()


@pytest.mark.parametrize("verb", ["merge", "alias"])
async def test_public_identity_no_viewer_gate_sees_private_write_from_peer_engine(
    tmp_path: Path, verb: str
) -> None:
    """A clean-cache entry cannot hide a peer Engine's committed private write."""
    path = str(tmp_path / "peer-identity.db")
    first = await _engine(path, viewer_required=True)
    second: Engine | None = None
    try:
        await first.write("Mel paints", namespace="a", entity="Mel")
        await first.write("Melanie sings", namespace="a", entity="Melanie")
        evidence = await first.write("Mel is Melanie", namespace="a")
        [mel_id] = await _ids(first, "a", "mel")
        [melanie_id] = await _ids(first, "a", "melanie")
        assert not await scope_guard.is_multi_party(first, "a")

        second = await _engine(path, viewer_required=True)
        await second.write(
            "Bob's private source",
            namespace="a",
            participants=["bob"],
            visibility="private",
        )
        with pytest.raises(ScopeRefused, match="viewer_required"):
            if verb == "merge":
                await first.merge_entities(melanie_id, mel_id, [evidence.record_id], namespace="a")
            else:
                await first.add_entity_alias(mel_id, "Mels", [evidence.record_id], namespace="a")
        assert await _markers(first) == []
    finally:
        if second is not None:
            await second.stop()
        await first.stop()


@pytest.mark.parametrize("verb", ["merge", "alias"])
async def test_public_identity_rechecks_after_first_private_writer(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """A private write after a stale no-viewer check must prevent a marker."""
    eng = await _engine(viewer_required=True)
    try:
        await eng.write("Mel paints", namespace="a", entity="Mel")
        await eng.write("Melanie sings", namespace="a", entity="Melanie")
        evidence = await eng.write("Mel is Melanie", namespace="a")
        [mel_id] = await _ids(eng, "a", "mel")
        [melanie_id] = await _ids(eng, "a", "melanie")
        inserted = False
        original = scope_guard.is_multi_party

        async def interleave_private_write(engine: Engine, namespace: str) -> bool:
            nonlocal inserted
            multi = await original(engine, namespace)
            if not multi and not engine._namespace_lock(namespace).locked() and not inserted:
                # A caller's first check saw a single-party namespace. Commit a
                # private write before it can acquire the marker's namespace lock.
                await eng.write(
                    "Bob's private identity evidence",
                    namespace="a",
                    participants=["bob"],
                    visibility="private",
                )
                inserted = True
            return multi

        async def identity_change() -> None:
            if verb == "merge":
                await eng.merge_entities(melanie_id, mel_id, [evidence.record_id], namespace="a")
            else:
                await eng.add_entity_alias(mel_id, "Mels", [evidence.record_id], namespace="a")

        monkeypatch.setattr(scope_guard, "is_multi_party", interleave_private_write)
        try:
            await identity_change()
        except ScopeRefused as exc:
            assert "viewer_required" in str(exc)
            refused = True
        else:
            refused = False
        if inserted:
            assert await _markers(eng) == []
            assert refused
        else:
            # A locked gate makes the write wait until after this call. Verify
            # the next call with the private record present is denied.
            assert not refused
            before = len(await _markers(eng))
            await eng.write(
                "Bob's private identity evidence",
                namespace="a",
                participants=["bob"],
                visibility="private",
            )
            with pytest.raises(ScopeRefused, match="viewer_required"):
                await identity_change()
            assert len(await _markers(eng)) == before
    finally:
        await eng.stop()
