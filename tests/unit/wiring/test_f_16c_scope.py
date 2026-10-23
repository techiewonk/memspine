"""F-16c: Scope in every key, viewer required on every multi-party read, isolation probes.

Probes try cross-tenant / cross-owner reads through every engine read verb, the scoped handle and
REST with the flags ON: each must be refused or come back empty. Flags OFF: nothing changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from memspine import Engine
from memspine.api.handle import Memory
from memspine.observability import tap
from memspine.read.tiers import ReadRequestError
from memspine.scope import Scope, ScopeRefused, ViewerPolicy, cross_scope_reason, system_scope

OWNER_1 = "acme/u1"
OWNER_2 = "acme/u2"
OTHER_TENANT = "globex/u1"
GROUP = "acme/family"


async def _engine(**isolation: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"hybrid": False, "record_access": False},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "shared": {"enabled": True},
        },
        isolation=isolation,
    )
    await eng.start()
    return eng


@pytest.fixture
async def on() -> AsyncIterator[Engine]:
    eng = await _engine(scope_keys_v2=True, viewer_required=True)
    yield eng
    await eng.stop()


@pytest.fixture
async def off() -> AsyncIterator[Engine]:
    eng = await _engine()
    yield eng
    await eng.stop()


async def _seed(eng: Engine) -> dict[str, str]:
    ids: dict[str, str] = {}
    for ns in (OWNER_1, OWNER_2, OTHER_TENANT):
        rec = await eng.write(f"secret of {ns} is plum", namespace=ns, memory_type="semantic")
        ids[ns] = rec.record_id
    return ids


async def _group(eng: Engine) -> None:
    await eng.write(
        "the gift is a kayak",
        namespace=GROUP,
        memory_type="episodic",
        participants=["carol", "dave"],
        visibility="private",
    )
    await eng.write(
        "dinner is at eight",
        namespace=GROUP,
        memory_type="episodic",
        participants=["carol", "dave"],
        visibility="participants",
    )


# -- the Scope value object ------------------------------------------------------------------


def test_scope_identity_is_tenant_and_owner_session_is_provenance() -> None:
    a = Scope("acme", "u1", session="s1")
    b = Scope("acme", "u1", session="s2")
    assert a == b and hash(a) == hash(b)
    assert a.key("cache", "q") == b.key("cache", "q")
    assert a.entity_key("Alice  Smith") == b.entity_key("alice smith")
    assert a.key("cache", "q") != Scope("acme", "u2").key("cache", "q")
    assert a.key("cache", "q") != Scope("globex", "u1").key("cache", "q")
    assert a.cache_key("q", viewer="x") != a.cache_key("q", viewer="y")
    assert a.namespace == "acme/u1" and Scope.from_namespace("acme/u1") == a
    assert Scope.from_namespace("solo").namespace == "solo"
    with pytest.raises(ValueError):
        Scope("")


def test_cross_scope_reasons() -> None:
    me = Scope.from_namespace("acme/u1")
    assert cross_scope_reason(me, "acme/u1") is None
    assert cross_scope_reason(me, "acme/u2") == "cross_owner"
    assert cross_scope_reason(me, "globex/u1") == "cross_tenant"
    assert cross_scope_reason(me, "acme") == "cross_owner"  # a parent is not the scope


def test_viewer_policy() -> None:
    assert ViewerPolicy().check() is None
    assert ViewerPolicy(multi_party=True).check() == "viewer_required"
    assert ViewerPolicy(multi_party=True, viewer="  ").check() == "viewer_required"
    assert ViewerPolicy(multi_party=True, viewer="al", principal="al").check() is None
    assert (
        ViewerPolicy(multi_party=True, viewer="bo", principal="al").check()
        == "viewer_not_principal"
    )
    with pytest.raises(ScopeRefused) as err:
        ViewerPolicy(multi_party=True).require()
    assert err.value.reason == "viewer_required"


# -- keys ------------------------------------------------------------------------------------


async def test_read_cache_key_scope_v2(on: Engine, off: Engine) -> None:
    from memspine.read.pipeline import request as rq

    keys = {
        repr(rq._read_cache_key(on, "q", ns, None, ())) for ns in (OWNER_1, OWNER_2, OTHER_TENANT)
    }
    assert len(keys) == 3
    scope_off = rq._cache_scope(off, OWNER_1)
    assert (scope_off.tenant, scope_off.owner) == (OWNER_1, OWNER_1)
    scope_on = rq._cache_scope(on, OWNER_1)
    assert (scope_on.tenant, scope_on.owner) == ("acme", "u1")


def test_graph_edge_key_scoped() -> None:
    from memspine.derive.pipeline.stages import extract_graph as eg
    from memspine.prompts.models import ExtractedEdge

    edge = ExtractedEdge(src_entity="a", rel="knows", dst_entity="b", fact="a knows b")
    plain = eg._edge_key("acme/u1", edge)
    scoped = eg._edge_key("acme/u1", edge, scoped=True)
    assert plain != scoped
    assert scoped == eg._edge_key("acme/u1", edge, scoped=True)
    assert scoped != eg._edge_key("acme/u2", edge, scoped=True)
    assert scoped != eg._edge_key("globex/u1", edge, scoped=True)


# -- isolation probes: engine verbs ----------------------------------------------------------


async def test_engine_verbs_never_cross_namespaces(on: Engine) -> None:
    ids = await _seed(on)
    for ns in (OWNER_2, OTHER_TENANT, "acme", "globex"):
        assert all(r.namespace == ns for r, _ in await on.search("secret plum", namespace=ns))
        read = await on.read("secret plum", namespace=ns, mode="retrieve")
        assert all(r.namespace == ns for r in read.context.records)
        ctx = await on.assemble("secret plum", namespace=ns)
        assert all(r.namespace == ns for r in ctx.records)
        assert all(r.namespace == ns for r in await on.retrieve(namespace=ns))
        assert all(r.namespace == ns for r, _ in await on.shared_search("plum", namespace=ns))
        assert all(r.namespace == ns for r in await on.brief(ns))
    assert await on.retrieve(namespace="acme") == []  # a parent path is not a prefix read
    assert await on.get_record(ids[OWNER_2], namespace=OWNER_1) is None
    assert await on.get_record(ids[OTHER_TENANT], namespace=OWNER_1) is None
    own = await on.get_record(ids[OWNER_1], namespace=OWNER_1)
    assert own is not None and own.namespace == OWNER_1


async def test_get_record_refusals_name_the_reason(
    on: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _seed(on)
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    assert await on.get_record(ids[OWNER_2], namespace=OWNER_1) is None
    assert await on.get_record(ids[OTHER_TENANT], namespace=OWNER_1) is None
    assert [f["reason"] for s, f in seen if s == "scope.refused"] == [
        "cross_owner",
        "cross_tenant",
    ]


# -- isolation probes: viewer required in multi-party namespaces -----------------------------


async def test_multi_party_read_without_viewer_is_refused_on_every_verb(
    on: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _group(on)
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    verbs: list[Any] = [
        lambda: on.search("kayak", namespace=GROUP),
        lambda: on.read("kayak", namespace=GROUP, mode="retrieve"),
        lambda: on.assemble("kayak", namespace=GROUP),
        lambda: on.retrieve(namespace=GROUP),
        lambda: on.shared_search("kayak", namespace=GROUP),
        lambda: on.get_record("x", namespace=GROUP),
        lambda: on.brief(GROUP),
        lambda: on.conversation(GROUP, "s"),
        lambda: on.session_memories(GROUP, ["x"]),
        lambda: on.timeline(GROUP),
        lambda: on.sessions(GROUP),
        lambda: on.namespace_stats(GROUP),
    ]
    for call in verbs:
        with pytest.raises(ScopeRefused) as err:
            await call()
        assert err.value.reason == "viewer_required"
    refused = [f for s, f in seen if s == "scope.refused"]
    assert len(refused) == len(verbs)
    assert {f["reason"] for f in refused} == {"viewer_required"}


async def test_multi_party_viewer_sees_only_what_it_may(on: Engine) -> None:
    await _group(on)
    carol = await on.search("kayak dinner", namespace=GROUP, viewer="carol")
    assert {r.content for r, _ in carol} == {"the gift is a kayak", "dinner is at eight"}
    assert await on.search("kayak dinner", namespace=GROUP, viewer="eve") == []
    assert await on.retrieve(namespace=GROUP, viewer="eve") == []
    assert len(await on.retrieve(namespace=GROUP, viewer="carol")) == 2  # the speaker
    assert len(await on.retrieve(namespace=GROUP, viewer="dave")) == 1  # not the private one
    ctx = await on.assemble("kayak", namespace=GROUP, viewer="eve")
    assert all("kayak" not in r.content for r in ctx.records)
    assert await on.shared_search("kayak", namespace=GROUP, viewer="eve") == []


async def test_configured_namespace_is_multi_party_even_when_empty() -> None:
    eng = await _engine(viewer_required=True, multi_party_namespaces=["acme/team"])
    try:
        with pytest.raises(ScopeRefused):
            await eng.search("x", namespace="acme/team/sub")
        await eng.search("x", namespace="acme/solo")  # single party: no viewer needed
    finally:
        await eng.stop()


async def test_system_scope_internal_reads_are_not_refused(on: Engine) -> None:
    await _group(on)
    with system_scope():
        assert await on.retrieve(namespace=GROUP)


# -- handle ----------------------------------------------------------------------------------


async def test_handle_scopes_every_read_to_its_owner(on: Engine) -> None:
    ids = await _seed(on)
    h = Memory(on, OWNER_1, principal="u1", viewer="u1")
    assert all(r.namespace == OWNER_1 for r, _ in await h.recall("secret plum"))
    assert all(r.namespace == OWNER_1 for r in await h.retrieve())
    assert await h.get_record(ids[OWNER_2]) is None
    assert await h.get_record(ids[OTHER_TENANT]) is None
    assert await h.get_record(ids[OWNER_1]) is not None
    assert all(r.namespace == OWNER_1 for r, _ in await h.shared_search("plum"))


async def test_handle_multi_party_needs_a_principal_bound_viewer(on: Engine) -> None:
    await _group(on)
    with pytest.raises(ReadRequestError):
        Memory(on, GROUP, multi_party=True)
    carol = Memory(on, GROUP, principal="carol", viewer="carol", multi_party=True)
    assert len(await carol.recall("kayak dinner")) == 2
    eve = Memory(on, GROUP, principal="eve", viewer="eve", multi_party=True)
    assert await eve.recall("kayak dinner") == []
    assert await eve.retrieve() == []
    assert await eve.shared_search("kayak") == []
    assert await eve.conversation("s") == []


async def test_declared_multi_party_handle_makes_bare_engine_reads_fail_closed(
    on: Engine,
) -> None:
    Memory(on, "acme/club", principal="p", viewer="p", multi_party=True)
    with pytest.raises(ScopeRefused):
        await on.search("x", namespace="acme/club")


# -- REST ------------------------------------------------------------------------------------


async def test_rest_cannot_cross_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    import httpx

    from memspine.api.rest import create_app
    from memspine.config.schema import RestConfig

    eng = await _engine(scope_keys_v2=True, viewer_required=True)
    try:
        await _seed(eng)
        await _group(eng)
        monkeypatch.setenv("MS_F16C_KEY", "u1-test-key-000001")
        rest = RestConfig.model_validate(
            {
                "auth": {
                    "mode": "api_key",
                    "api_keys": [
                        {
                            "key_env": "MS_F16C_KEY",
                            "principal": "u1",
                            "namespaces": ["acme/u1", "acme/family"],
                        }
                    ],
                }
            }
        )
        transport = httpx.ASGITransport(app=create_app(eng, rest), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://m") as http:

            def hdr(ns: str) -> dict[str, str]:
                return {"Authorization": "Bearer u1-test-key-000001", "X-Memspine-Namespace": ns}

            for ns in (OWNER_2, OTHER_TENANT):
                for route, body in (("/search", {"query": "plum"}), ("/assemble", {"query": "p"})):
                    resp = await http.post(route, json=body, headers=hdr(ns))
                    assert resp.status_code == 403, (route, ns)
                assert (await http.post("/retrieve", json={}, headers=hdr(ns))).status_code == 403
            ok = await http.post("/search", json={"query": "plum"}, headers=hdr(OWNER_1))
            assert ok.status_code == 200
            assert all(item["record"]["namespace"] == OWNER_1 for item in ok.json())
            # a multi-party namespace is read as the principal; u1 is no participant
            grp = await http.post("/search", json={"query": "kayak"}, headers=hdr(GROUP))
            assert grp.status_code == 200 and grp.json() == []
    finally:
        await eng.stop()


# -- flags off: behaviour unchanged ----------------------------------------------------------


async def test_flags_off_do_not_refuse_or_filter(off: Engine) -> None:
    await _group(off)
    hits = await off.search("kayak", namespace=GROUP)  # no viewer: today's behaviour
    assert any("kayak" in r.content for r, _ in hits)
    assert len(await off.retrieve(namespace=GROUP)) == 2
    assert await off.retrieve(namespace=GROUP, viewer="eve") == []  # a new argument filters
    from memspine.read.pipeline import request as rq

    scope = rq._cache_scope(off, GROUP)
    assert (scope.tenant, scope.owner) == (GROUP, GROUP)
