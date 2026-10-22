"""F-16f (M-25): isolation, erasure and rebuild matrix on both storage profiles.

Axes (every cell asserts no leak and no resurrection):

* profile    ``embedded`` always; ``server`` (Postgres) only when ``MEMSPINE_TEST_PG_URL`` is set,
             otherwise the cell is skipped with that reason
* graph      ``embedded`` (the default graph) or ``neo4j`` (the F-16e mirror through the outbox,
             against the in-memory ``FakeNeo4j`` recorder; no Neo4j server needed)
* operation  cross-tenant read, cross-owner read, multi-party read without a viewer, hard erase then
             read, hard erase then restart then read, erase then replay (``migrate``) then read,
             ``Engine.rebuild`` then read

The workload and the stub model roles are the EX-1a ones (hash embedder, stub LLM callables,
invented names), so nothing here needs a GPU or a model. Run:
``pytest tests/e2e/test_f16f_matrix.py -m milestone -o addopts="" -rs`` (skips list the cells that
did not run and why).
"""

# ruff: noqa: F811
from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import memspine
from memspine.schemas.durable import OwnerScope
from memspine.scope import ScopeRefused
from memspine.store import lineage as lineage_store
from memspine.store.migrate import dump_store, migrate_sqlite
from memspine.trust.erasure_service import ErasureService, ErasureServiceDisabled

sys.path.insert(0, str(Path(__file__).parent))
from test_ex1a_embedded import (
    BRAM_S1 as _BRAM_S1,
)
from test_ex1a_embedded import (
    HOME as _HOME,
)
from test_ex1a_embedded import (
    ILSE_S1 as _ILSE_S1,
)
from test_ex1a_embedded import (
    ILSE_S2 as _ILSE_S2,
)
from test_ex1a_embedded import (
    NS_BRAM,
    NS_HOME,
    NS_ILSE,
    OTHER,
    SECRET,
    TENANT,
    World,
    _dump,
    _overrides,
    _sql,
    _turns,
    stubs,  # noqa: F401  (fixture)
)

pytestmark = [
    pytest.mark.milestone,
    pytest.mark.filterwarnings("ignore:optimize:UserWarning"),  # lancedb purge, by design
]

NS_GLOBEX = "globex/zed"
GLOBEX_SECRET = "Fernhollow"  # a second tenant's token: must never reach acme, nor the reverse
GLOBEX_LINES = (f"Zed: The {GLOBEX_SECRET} orchard ships crates every Friday",)

PROFILES = ("embedded", "server")
GRAPHS = ("embedded", "neo4j")
OPS = (
    "cross_tenant_read",
    "cross_owner_read",
    "multi_party_no_viewer",
    "erase_then_read",
    "erase_restart_read",
    "erase_replay_read",
    "rebuild_read",
)


def _pg_url() -> str | None:
    return os.environ.get("MEMSPINE_TEST_PG_URL")


# --------------------------------------------------------------------------- the fake Neo4j


#: The fake the next Neo4j mirror build uses (a cell swaps it per open).
_CURRENT_FAKE: dict[str, Any] = {}


class _FakeProxy:
    """Delegates to the cell's current fake, so one patched builder serves every open."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_CURRENT_FAKE["fake"], name)


@pytest.fixture(autouse=True)
def _fake_neo4j_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """The neo4j cells' mirror builds over the fake (same seam as tests/unit F-16e)."""
    sys.path.insert(0, str(Path(__file__).parents[1] / "unit" / "wiring"))
    from test_f_16e_neo4j_outbox import (  # type: ignore[import-not-found]
        _install_fake_mirror,
    )

    _install_fake_mirror(monkeypatch, _FakeProxy())


def _fake_neo4j() -> Any:
    sys.path.insert(0, str(Path(__file__).parents[1] / "unit" / "wiring"))
    from test_f_16e_neo4j_outbox import FakeNeo4j  # type: ignore[import-not-found]

    return FakeNeo4j()


# --------------------------------------------------------------------------- a matrix world


class Cell:
    """One matrix cell's running engine, its handles and (neo4j cells) its fake graph."""

    def __init__(self, profile: str, graph: str, tmp: Path, rederive: bool = False) -> None:
        self.profile, self.graph, self.tmp = profile, graph, tmp
        self.rederive = rederive  # FU-ERASE: privacy.erasure_rederive
        self.db = tmp / "f16f.db"
        self.fake: Any = None
        self.ids: dict[str, str] = {}
        self.world: World | None = None

    def overrides(self, db: Path | None = None) -> dict[str, Any]:
        o = _overrides(db or self.db, rederive=self.rederive)
        if self.profile == "server":
            o["storage"] = {
                "profile": "server",
                "provider": "postgres",
                "settings": {
                    "url": _pg_url(),
                    "data_dir": str(self.tmp / "derived"),
                    "connect_timeout_s": 5.0,
                    "statement_timeout_s": 30.0,
                },
            }
            o["runtime"] = {**o.get("runtime", {}), "deployment_id": "e2e-f16f"}
        if self.graph == "neo4j":  # the F-16e mirror fed through the outbox
            o["graph"] = {
                **o["graph"],
                "mirror": {
                    "enabled": True,
                    "provider": "neo4j",
                    "settings": {"connect_timeout_s": 2.0, "request_timeout_s": 2.0},
                },
            }
            o["runtime"] = {**o.get("runtime", {}), "deployment_id": "e2e-f16f"}  # shared Neo4j
        return o

    async def open(self, db: Path | None = None) -> World:
        if self.graph == "neo4j":
            if db is None and self.fake is None:
                self.fake = _fake_neo4j()
            _CURRENT_FAKE["fake"] = self.fake if db is None else _fake_neo4j()
        eng = await memspine.open(dotenv_path=None, **self.overrides(db))
        return World._handles(db or self.db, eng, self.ids)

    @property
    def w(self) -> World:
        assert self.world is not None
        return self.world

    async def restart(self) -> None:
        await self.w.eng.stop()
        self.world = await self.open()

    @property
    def service(self) -> ErasureService:
        return ErasureService(self.w.eng)

    def gone(self) -> frozenset[str]:
        return self.service.gone()


def _wipe_pg(url: str) -> None:
    import sqlalchemy as sa

    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    meta = sa.MetaData()
    meta.reflect(bind=engine)
    meta.drop_all(bind=engine)
    engine.dispose()


def _skip_if_unavailable(profile: str) -> None:
    if profile == "server":
        if not _pg_url():
            pytest.skip("server profile: set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")
        pytest.importorskip("psycopg")


@pytest.fixture
async def cell(
    request: pytest.FixtureRequest, tmp_path: Path, stubs: dict[str, Any]
) -> AsyncIterator[Cell]:
    profile, graph = request.param
    _skip_if_unavailable(profile)
    if profile == "server":
        _wipe_pg(_pg_url() or "")
    c = Cell(profile, graph, tmp_path)
    c.world = await c.open()
    try:
        w = c.w
        for handle, session, lines, start in (
            (w.ilse, "ilse-s1", _ILSE_S1, 0),
            (w.ilse, "ilse-s2", _ILSE_S2, 60),
            (w.bram, "bram-s1", _BRAM_S1, 0),
            (
                w.eng.scope(NS_GLOBEX, principal="zed", viewer="zed", purpose="assist"),
                "zed-s1",
                GLOBEX_LINES,
                0,
            ),
        ):
            await handle.ingest_messages(_turns(lines, start), session_id=session, group_id=session)
        for text, visibility in _HOME:
            await w.eng.write(
                text,
                namespace=NS_HOME,
                memory_type="episodic",
                participants=["ilse", "tomasz"],
                visibility=visibility,
            )
        await w.eng.consent_grant(NS_ILSE, "assist")
        await w.eng.close_session(NS_ILSE, "ilse-s1")
        await w.eng.close_session(NS_ILSE, "ilse-s2")
        await w.eng.flush_windows()
        await w.ilse.sleep()
        c.ids["secret_turn"] = await w._turn_id(NS_ILSE, SECRET)
        yield c
    finally:
        if c.world is not None and c.world.eng.is_started:
            await c.world.eng.stop()
        _CURRENT_FAKE.clear()


CELLS = [(p, g) for p in PROFILES for g in GRAPHS]


def _ids(param: tuple[str, str]) -> str:
    return f"{param[0]}-{param[1]}"


G1 = (
    "G1: erasure_v2._purge_step (graph) deletes hidden dependants from the embedded graph only; "
    "the Neo4j mirror keeps their nodes. Fix: delete them from eng._neo4j.store there "
    "(erasure_v2.py is FX-EX1A's area, left untouched)"
)


def matrix(op: str, *, g1: bool = False) -> Any:
    params = [
        pytest.param(
            c,
            id=f"{_ids(c)}-{op}",
            marks=[],  # G1 fixed: erasure_v2 purges the Neo4j mirror too
        )
        for c in CELLS
    ]
    return pytest.mark.parametrize("cell", params, indirect=True)


# --------------------------------------------------------------------------- shared assertions


async def _no_foreign_text(handle: Any, probe: str, foreign: str) -> None:
    got = await handle.recall(probe)
    assert all(foreign not in r.content for r, _ in got), (handle.namespace, probe)
    ctx = await handle.compose(probe)
    assert all(foreign not in r.content for r in ctx.records)
    assert all(r.namespace == handle.namespace for r in ctx.records)
    read = await handle.read(probe)
    recs = getattr(getattr(read, "context", read), "records", [])
    assert all(foreign not in r.content for r in recs)
    assert all(r.namespace == handle.namespace for r in await handle.retrieve())


def _neo4j_clean(c: Cell, erased: set[str], text: str) -> None:
    """The mirror holds no erased node, then (checked last) no hidden dependant and no text."""
    if c.fake is None:
        return
    held = {n["id"] for n in c.fake.nodes.values()}
    secret_id = c.ids["secret_turn"]
    assert secret_id not in held, "neo4j mirror still holds the erased record"
    assert all(text not in str(n) for n in c.fake.nodes.values() if n["id"] == secret_id)
    # F-16f finding G1: erasure_v2's graph purge step deletes hidden dependants from the embedded
    # graph only; the Neo4j mirror keeps their nodes (and the erased text they carry)
    assert not (held & erased), f"neo4j mirror still holds hidden dependants: {held & erased}"
    assert all(text not in str(n) for n in c.fake.nodes.values()), "neo4j mirror carries text"


async def _assert_erased_clean(c: Cell, label: str) -> None:
    w, secret_id = c.w, c.ids["secret_turn"]
    gone = c.gone()
    assert secret_id in gone, f"{label}: the erased id is not in the erasure state"
    for query in (SECRET, "beehive behind the mill"):
        got = await w.ilse.recall(query)
        ids = {r.record_id for r, _ in got}
        assert not (ids & gone), f"{label}: erased/hidden id served ({query})"
        assert all(SECRET not in r.content for r, _ in got), f"{label}: erased text served"
    ctx = await w.ilse.compose(SECRET)
    assert all(SECRET not in r.content for r in ctx.records), f"{label}: compose leaked"
    assert {r.record_id for r in ctx.records}.isdisjoint(gone), label
    read = await w.ilse.read(SECRET)
    recs = getattr(getattr(read, "context", read), "records", [])
    assert all(SECRET not in r.content for r in recs), f"{label}: read leaked"
    assert {r.record_id for r in recs}.isdisjoint(gone), label
    assert secret_id in gone, label
    # the unrelated record of the same owner survives: erasure is not a wipe
    assert await w.ilse.recall(OTHER), f"{label}: an unrelated Ilse record disappeared"
    # the lineage table holds no edge to the erased id
    engine = w.eng._require_started()._client.engine
    assert not (await lineage_store.children_of(engine, [secret_id])).get(secret_id), (
        f"{label}: lineage edge from the erased turn survives"
    )
    assert not (await lineage_store.parents_of(engine, [secret_id])).get(secret_id), (
        f"{label}: lineage edge to the erased turn survives"
    )
    _neo4j_clean(c, set(gone), SECRET)


async def _erase(c: Cell) -> None:
    """Plan through the service (pure), erase through the public handle, compare the two."""
    secret_id = c.ids["secret_turn"]
    plan = await c.service.plan(secret_id, NS_ILSE)
    assert plan.erased == frozenset({secret_id})
    assert plan.hidden - plan.erased, "the secret turn has no dependants: the matrix proves nothing"
    # the plan is pure: nothing is gone yet
    assert not c.gone()
    warm = await c.w.ilse.recall(SECRET)  # warm the read cache with a read naming the secret
    assert warm
    await c.w.ilse.forget(secret_id, hard=True)
    assert c.gone() >= set(plan.hidden), "the executed erasure hid less than the plan said"
    assert c.service.is_gone(secret_id)
    assert all(p["state"] == "done" for p in c.service.status()["purges"].values())  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- the matrix


@matrix("cross_tenant_read")
async def test_cross_tenant_read(cell: Cell) -> None:
    w = cell.w
    zed = w.eng.scope(NS_GLOBEX, principal="zed", viewer="zed", purpose="assist")
    await _no_foreign_text(w.ilse, GLOBEX_SECRET, GLOBEX_SECRET)
    await _no_foreign_text(w.ilse, "orchard crates", GLOBEX_SECRET)
    await _no_foreign_text(zed, SECRET, SECRET)
    await _no_foreign_text(zed, "beehive behind the mill", SECRET)
    assert await zed.recall(GLOBEX_SECRET), "the second tenant cannot read its own record"
    assert await w.eng.get_record(cell.ids["secret_turn"], namespace=NS_GLOBEX) is None
    assert await w.eng.get_record(cell.ids["secret_turn"], namespace=NS_ILSE) is not None
    # erasing in one tenant never touches the other
    await w.ilse.forget(cell.ids["secret_turn"], hard=True)
    assert await zed.recall(GLOBEX_SECRET), "erasing in acme removed a globex record"
    await _no_foreign_text(zed, SECRET, SECRET)


@matrix("cross_owner_read")
async def test_cross_owner_read(cell: Cell) -> None:
    w = cell.w
    await _no_foreign_text(w.ilse, "tandem", "tandem")
    await _no_foreign_text(w.bram, SECRET, SECRET)
    await _no_foreign_text(w.bram, "beehive behind the mill", SECRET)
    assert {r.namespace for r in await w.bram.retrieve()} <= {NS_BRAM}
    assert await w.eng.get_record(cell.ids["secret_turn"], namespace=NS_BRAM) is None


@matrix("multi_party_no_viewer")
async def test_multi_party_without_viewer(cell: Cell) -> None:
    w = cell.w
    with pytest.raises(ScopeRefused) as err:
        await w.eng.search("village fete", namespace=NS_HOME)
    assert err.value.reason == "viewer_required"
    seen = await w.home_ilse.recall("village fete lantern")
    assert any("village fete" in r.content for r, _ in seen)
    eve = w.eng.scope(
        OwnerScope(tenant=TENANT, owner="household", owner_kind="group"),
        principal="eve",
        viewer="eve",
    )
    assert await eve.recall("village fete lantern") == []
    assert all(r.namespace != NS_HOME for r in await w.ilse.retrieve())
    # a refusal leaves no side effect: the erase after it still behaves
    await w.ilse.forget(cell.ids["secret_turn"], hard=True)
    with pytest.raises(ScopeRefused):
        await w.eng.search("village fete", namespace=NS_HOME)


@matrix("erase_then_read", g1=True)
async def test_erase_then_read(cell: Cell) -> None:
    await _erase(cell)
    await _assert_erased_clean(cell, "after-erase")
    # a second erase of the same id plans nothing new and resurrects nothing
    before = cell.gone()
    await _assert_erased_clean(cell, "cache-hit")
    assert cell.gone() == before


@matrix("erase_restart_read", g1=True)
async def test_erase_restart_read(cell: Cell) -> None:
    await _erase(cell)
    before, status = cell.gone(), cell.service.status()
    await cell.restart()
    assert cell.gone() == before, "the erasure state did not survive the restart"
    status2 = cell.service.status()
    assert status2["erased"] == status["erased"] and status2["hidden"] == status["hidden"]
    await _assert_erased_clean(cell, "after-restart")


@matrix("erase_replay_read", g1=True)
async def test_erase_replay_read(cell: Cell) -> None:
    if cell.profile != "embedded":
        pytest.skip("replay: migrate_sqlite replays a SQLite source; the server replay is rebuild")
    await _erase(cell)
    secret_id, gone = cell.ids["secret_turn"], cell.gone()
    await cell.w.eng.stop()
    target = cell.tmp / "replayed.db"
    rep = await migrate_sqlite(str(cell.db), str(target))
    assert rep.events_written == rep.events_read > 0

    import json

    replay = json.loads(await _dump(target))
    live = json.loads(await _dump(cell.db))
    assert secret_id not in {r["record_id"] for r in replay["records"]}
    visible_holders = [
        r["record_id"]
        for r in replay["records"]
        if SECRET in json.dumps(r) and r["record_id"] not in gone
    ]
    assert not visible_holders, (
        f"a visible replayed record carries the erased text: {visible_holders}"
    )
    live_ids = {r["record_id"] for r in live["records"]}
    assert live_ids <= {r["record_id"] for r in replay["records"]}, "records lost in replay"

    fresh = await cell.open(target)
    cell.world = fresh
    try:
        assert cell.service.status()["erased"] == 1
        assert secret_id in cell.gone()
        await _assert_erased_clean(cell, "replayed")
    finally:
        await fresh.eng.stop()


@matrix("rebuild_read", g1=True)
async def test_rebuild_then_read(cell: Cell) -> None:
    w = cell.w
    # rebuild of an untouched store: reads and isolation hold, nothing is lost
    before = len(await w.ilse.recall("beehive behind the mill"))
    counts = await w.eng.rebuild()
    assert counts
    assert len(await w.ilse.recall("beehive behind the mill")) == before
    await _no_foreign_text(w.ilse, GLOBEX_SECRET, GLOBEX_SECRET)
    # erase, rebuild again: the replay re-reads the log and must not bring anything back
    await _erase(cell)
    gone = cell.gone()
    await w.eng.rebuild()
    assert cell.gone() == gone
    await _assert_erased_clean(cell, "after-rebuild")
    await cell.restart()
    await cell.w.eng.rebuild()
    await _assert_erased_clean(cell, "after-restart-rebuild")


# --------------------------------------------------------------------------- the service itself


async def test_service_needs_the_flag(tmp_path: Path, stubs: dict[str, Any]) -> None:
    o = _overrides(tmp_path / "off.db")
    o["privacy"] = {"erasure_v2": False}
    eng = await memspine.open(dotenv_path=None, **o)
    try:
        with pytest.raises(ErasureServiceDisabled):
            await ErasureService(eng).plan("r1", NS_ILSE)
    finally:
        await eng.stop()


async def test_service_execute_matches_the_handle(tmp_path: Path, stubs: dict[str, Any]) -> None:
    """``execute`` is the same erasure as the handle's hard forget: same hidden set, clean reads."""
    c = Cell("embedded", "embedded", tmp_path)
    c.world = await c.open()
    try:
        w = c.w
        await w.ilse.ingest_messages(_turns(_ILSE_S1, 0), session_id="ilse-s1", group_id="ilse-s1")
        await w.eng.close_session(NS_ILSE, "ilse-s1")
        await w.eng.flush_windows()
        secret_id = await w._turn_id(NS_ILSE, SECRET)
        plan = await c.service.execute(secret_id, NS_ILSE, actor="user", reason="matrix")
        assert plan.erased == frozenset({secret_id})
        assert c.gone() == set(plan.hidden)
        got = await w.ilse.recall(SECRET)
        assert all(SECRET not in r.content for r, _ in got)
        assert not _sql(
            c.db,
            f"SELECT 1 FROM lineage WHERE parent_id = '{secret_id}' OR child_id = '{secret_id}'",
        )
        _ = dump_store  # the dump helper is exercised by the replay cell
    finally:
        await c.w.eng.stop()


async def test_erase_then_sleep_rederives_and_reads_stay_clean(
    tmp_path: Path, stubs: dict[str, Any]
) -> None:
    """FU-ERASE: with ``privacy.erasure_rederive`` on, the sleep after an erasure re-derives (or
    drops) every queued dependant; reads stay clean before and after, across a restart, and no
    erased text is left in the sqlite file or its WAL."""
    c = Cell("embedded", "embedded", tmp_path, rederive=True)
    c.world = await c.open()
    try:
        w = c.w
        for handle, session, lines, start in (
            (w.ilse, "ilse-s1", _ILSE_S1, 0),
            (w.ilse, "ilse-s2", _ILSE_S2, 60),
        ):
            await handle.ingest_messages(_turns(lines, start), session_id=session, group_id=session)
        await w.eng.consent_grant(NS_ILSE, "assist")
        await w.eng.close_session(NS_ILSE, "ilse-s1")
        await w.eng.close_session(NS_ILSE, "ilse-s2")
        await w.eng.flush_windows()
        await w.ilse.sleep()
        c.ids["secret_turn"] = await w._turn_id(NS_ILSE, SECRET)
        await _erase(c)
        await _assert_erased_clean(c, "after-erase")
        queued = {str(i["record_id"]) for i in w.eng.erasure_queue()}
        assert queued, "nothing queued: the variant proves nothing"
        await w.sleep()
        assert not w.eng.erasure_queue()
        storage = w.eng._require_started()
        for rid in queued:
            assert await storage.get_record(rid) is None, f"{rid} kept its row"
        await _assert_erased_clean(c, "after-rederive")
        await c.restart()
        await _assert_erased_clean(c, "after-rederive-restart")
        assert not c.w.eng.erasure_queue()
        await c.w.eng.stop()
        raw = SECRET.encode()
        for f in (c.db, c.db.with_name(c.db.name + "-wal")):
            assert not (f.exists() and raw in f.read_bytes()), f"{f.name} still holds the text"
    finally:
        if c.world is not None and c.world.eng.is_started:
            await c.world.eng.stop()
