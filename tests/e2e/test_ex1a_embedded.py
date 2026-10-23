"""EX-1a: one synthetic workload survives restart, erasure and replay on the embedded profile.

Milestone gate (``docs/v2/PLAN.md``, ER-05): "restart, erasure and replay on the embedded
profile; not a server release". Every foundation flag that has landed is ON, the workload is
driven through the public API (``memspine.open`` and the scoped ``Memory`` handle), and nothing
touches a GPU or a real model: the embedder is the hash embedder and the window / session-close
roles are stub callables. All text is invented (Ilse, Bram, Noor, Tomasz).

Steps (each its own test; each rebuilds the workload, which takes well under a second):

1. ``test_step1_workload_written``   sessions for 2 owners in 1 tenant + a shared namespace
2. ``test_step2_sleep_and_read``     sleep, cited answers, no cross-owner leak, viewer refusal
3. ``test_step3_restart_state``      stop, reopen the same store, state identical / rebuilt
4. ``test_step4_hard_erase``         dependants hidden, purge done, reads (cache, restart) clean
5. ``test_step5_replay_migrate``     ``migrate_sqlite`` into a fresh store, compare, no resurrection

Run: ``pytest tests/e2e/test_ex1a_embedded.py -m milestone -o addopts=""`` (see
``docs/v2/MILESTONES.md``). A JSON report lands in ``docs/v2/data/milestones/ex1a_<date>.json``
when the module finishes (only with ``EX1A_REPORT=1``).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import memspine
from memspine import Engine, Memory
from memspine.runtime.jobs import TERMINAL
from memspine.schemas.durable import OwnerScope
from memspine.scope import ScopeRefused
from memspine.services.llm.base import LLMRouter
from memspine.store import lineage as lineage_store
from memspine.store.migrate import dump_store, migrate_sqlite
from memspine.trust.pipeline import consent as trust_consent
from memspine.trust.pipeline import erasure_v2 as trust_erasure

pytestmark = [
    pytest.mark.milestone,
    pytest.mark.filterwarnings("ignore:optimize:UserWarning"),  # lancedb purge, by design
]

TENANT = "acme"
NS_ILSE = "acme/ilse"
NS_BRAM = "acme/bram"
NS_HOME = "acme/household"
T0 = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)

#: invented, unmistakable tokens: a byte scan for them is unambiguous
SECRET = "Quillfeather"  # the erased turn's content (and so its dependants')
OTHER = "Marrowbrook"  # an Ilse turn that must survive the erasure untouched

ILSE_S1 = (
    f"Ilse: I keep a beehive named {SECRET} behind the mill",
    "Ilse: The honey harvest starts in the last week of August",
    "Ilse: Noor is helping me paint the hive boxes pale green",
)
ILSE_S2 = (
    f"Ilse: The {OTHER} footbridge was repaired by Tomasz this spring",
    "Ilse: I booked a kayak tour along the northern coast for June",
    "Ilse: Tomasz lends me his ladder every autumn",
)
BRAM_S1 = (
    "Bram: I repair bicycles in the garage on Sundays",
    "Bram: Noor wants a second-hand tandem for the summer",
    "Bram: The chain of the red tandem needs a new link",
)
HOME = (
    ("the village fete is on the second Saturday of July", "participants"),
    ("Tomasz hides the surprise lantern in the cellar", "private"),
)

_LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)
_CLOSE_LINE = re.compile(r"^\[(\d+)\] \S+ (\S+): (.+)$", re.M)


# --------------------------------------------------------------------------- stub model roles


class StubWindowLLM:
    """The ``window_extract`` role: one fact, one edge and an episode, each citing its line."""

    provider_id = "stub:window"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        user = messages[-1]["content"]
        sections = re.search(r"sections: (.+)\.$", user.strip(), re.M).group(1).split(", ")  # type: ignore[union-attr]
        lines = [(int(n), text) for n, text in _LINE.findall(user)]
        out: dict[str, Any] = {}
        if "facts" in sections:
            out["facts"] = [
                {
                    "entity": text.split(":")[0],
                    "attribute": "event",
                    "value": f"{text.split(':')[0]} said {text.split(': ', 1)[1]}",
                    "kind": "event",
                    "turns": [n],
                    "span": text.split(": ", 1)[1],
                    "persons": [text.split(":")[0]],
                    "topic": "activities",
                }
                for n, text in lines
            ]
        if "edges" in sections:
            out["edges"] = [
                {
                    "src_entity": text.split(":")[0],
                    "rel": "mentioned",
                    "dst_entity": f"item{n}",
                    "fact": f"{text.split(':')[0]} mentioned item{n}",
                    "turns": [n],
                }
                for n, text in lines
            ]
        if "episode" in sections:
            out["episode"] = {
                "title": "A chat",
                "summary": "People chat.",
                "narrative": " ".join(text for _, text in lines),
                "turns": [n for n, _ in lines],
            }
        return json.dumps(out)


class StubCloseLLM:
    """The ``session_close`` role (call 2): a summary and one episode citing every line."""

    provider_id = "stub:close"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        user = messages[-1]["content"]
        lines = [int(n) for n, _, _ in _CLOSE_LINE.findall(user)]
        return json.dumps(
            {
                "summary": {"text": "A short chat about bees, bridges and bicycles."},
                "episodes": [
                    {"text": "The speakers talk about their weekend plans.", "lines": lines}
                ],
                "profile_ops": [],
            }
        )


# --------------------------------------------------------------------------- the workload


def _overrides(db: Path, redact: bool = False, rederive: bool = False) -> dict[str, Any]:
    """Every landed foundation flag ON, on the embedded (SQLite) profile.

    ``redact`` turns ``privacy.erasure_redact_hidden`` on (finding F2): the whole milestone runs
    with it off (the default) and on. ``rederive`` turns ``privacy.erasure_rederive`` on (FU-ERASE):
    the sleep stage ``erasure_rederive`` then re-derives the hidden dependants.
    """
    return {
        "template": "core",
        "storage": {
            "records": {"provider": "sqlite", "settings": {"path": str(db), "busy_timeout_s": 5.0}}
        },
        "model": {
            "embedding": {"provider": "hash"},
            "llm": {
                "window_extract": {
                    "provider": "openai_compatible",
                    "timeout_s": 60.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "stub/window"},
                },
                "session_close": {
                    "provider": "openai_compatible",
                    "timeout_s": 60.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "stub/close"},
                },
            },
        },
        "memories": {
            "episodic": {"enabled": True},
            "semantic": {"enabled": True},
            "shared": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        "read": {
            "hybrid": False,
            "record_access": False,
            "cache": {"enabled": True},
            "legs_v2": True,  # read.legs_v2
        },
        "write": {
            "lineage_table": True,  # write.lineage_table
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
            # write.participants stays at its default (off). Finding F1 is fixed: with
            # isolation.viewer_required on, a multi-party namespace behaves as participants=session
            # (raw turns are tagged with their speakers, derived records inherit their parents'
            # participant and visibility tags), so no explicit participants setting is needed.
            "participants": "off",
        },
        "workers": {"outbox": True, "transport": "inline"},  # workers.outbox (inline transport)
        "isolation": {
            "scope_keys_v2": True,  # isolation.scope_keys_v2
            "viewer_required": True,  # isolation.viewer_required
        },
        "privacy": {
            "erasure_v2": True,
            "erasure_redact_hidden": redact,
            "erasure_rederive": rederive,
        },  # privacy.erasure_v2
        "retention": {  # retention.v2 (a class far beyond the workload: plans, expires nothing)
            "v2": True,
            "classes": [{"namespace": "*", "ttl_days": 36500}],
        },
        "audit": {"chain_v2": True, "actions": True, "reads": True},  # audit.chain_v2
        "consent": {"v2": True},  # consent.v2
        "graph": {"edge_compiler_v2": True},  # graph.edge_compiler_v2
        "offline": {"session_close_v2": True},  # offline.session_close_v2
    }


FLAGS_ON = (
    "write.lineage_table",
    "workers.outbox",
    "isolation.scope_keys_v2",
    "isolation.viewer_required",
    "privacy.erasure_v2",
    "retention.v2",
    "audit.chain_v2",
    "consent.v2",
    "graph.edge_compiler_v2",
    "offline.session_close_v2",
    "read.legs_v2",
)


def _turns(lines: tuple[str, ...], start: int) -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": text,
            "timestamp": (T0 + timedelta(minutes=start + i)).isoformat(),
        }
        for i, text in enumerate(lines)
    ]


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    window, close = StubWindowLLM(), StubCloseLLM()

    async def router(config: Any, hooks: object = None, wiring: object = None) -> LLMRouter:
        return LLMRouter({"window_extract": window, "session_close": close})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    return {"window": window, "close": close}


@dataclass
class World:
    """The running engine plus the ids the steps check."""

    db: Path
    eng: Engine
    ilse: Memory
    bram: Memory
    home_ilse: Memory
    ids: dict[str, str]
    redact: bool = False
    rederive: bool = False

    @classmethod
    async def build(cls, db: Path, redact: bool = False, rederive: bool = False) -> World:
        eng = await memspine.open(dotenv_path=None, **_overrides(db, redact, rederive))
        return cls._handles(db, eng, {}, redact, rederive)

    @staticmethod
    def _handles(
        db: Path, eng: Engine, ids: dict[str, str], redact: bool = False, rederive: bool = False
    ) -> World:
        home = OwnerScope(tenant=TENANT, owner="household", owner_kind="group")
        return World(
            db=db,
            eng=eng,
            ilse=eng.scope(NS_ILSE, principal="ilse", viewer="ilse", purpose="assist"),
            bram=eng.scope(NS_BRAM, principal="bram", viewer="bram", purpose="assist"),
            home_ilse=eng.scope(home, principal="ilse", viewer="ilse"),
            ids=ids,
            redact=redact,
            rederive=rederive,
        )

    async def restart(self) -> None:
        if self.eng.is_started:
            await self.eng.stop()
        eng = await memspine.open(
            dotenv_path=None, **_overrides(self.db, self.redact, self.rederive)
        )
        fresh = self._handles(self.db, eng, self.ids, self.redact, self.rederive)
        self.eng, self.ilse, self.bram, self.home_ilse = (
            fresh.eng,
            fresh.ilse,
            fresh.bram,
            fresh.home_ilse,
        )

    async def populate(self) -> None:
        """Three sessions of turns for two owners, a shared namespace, consent choices."""
        for handle, session, lines, start in (
            (self.ilse, "ilse-s1", ILSE_S1, 0),
            (self.ilse, "ilse-s2", ILSE_S2, 60),
            (self.bram, "bram-s1", BRAM_S1, 0),
        ):
            await handle.ingest_messages(_turns(lines, start), session_id=session, group_id=session)
        for text, visibility in HOME:
            await self.eng.write(
                text,
                namespace=NS_HOME,
                memory_type="episodic",
                participants=["ilse", "tomasz"],
                visibility=visibility,
            )
        await self.eng.consent_grant(NS_ILSE, "assist")
        await self.eng.consent_revoke(NS_BRAM, "marketing")
        await self.eng.consent_do_not_derive(NS_BRAM, "Noor")
        for key, handle in (("ilse", self.ilse), ("bram", self.bram)):
            await self.eng.close_session(handle.namespace, f"{key}-s1")
        await self.eng.close_session(NS_ILSE, "ilse-s2")
        self.ids["secret_turn"] = await self._turn_id(NS_ILSE, SECRET)

    async def _turn_id(self, ns: str, needle: str) -> str:
        storage = self.eng._require_started()
        hits = [
            r
            for r in await storage.list_records(ns, "episodic")
            if needle in r.content and r.source.channel == "messages"
        ]
        assert len(hits) == 1, (ns, needle, len(hits))
        return hits[0].record_id

    async def sleep(self) -> dict[str, Any]:
        await self.eng.flush_windows()
        return await self.ilse.sleep()


@pytest.fixture(
    params=["off", "redact", "rederive"], ids=["redact_off", "redact_on", "rederive_on"]
)
async def world(
    request: pytest.FixtureRequest, tmp_path: Path, stubs: dict[str, Any]
) -> AsyncIterator[World]:
    w = await World.build(
        tmp_path / "ex1a.db",
        redact=request.param == "redact",
        rederive=request.param == "rederive",
    )
    try:
        await w.populate()
        yield w
    finally:
        if w.eng.is_started:
            await w.eng.stop()


def _gone(w: World) -> set[str]:
    """Erased and hidden ids (POL-1): invisible to every read."""
    st = trust_erasure.state(w.eng)
    return set(st.erased) | set(st.hidden)


def _cites(records: Any) -> set[str]:
    """Record ids an answer rests on: the records served and the source turns they cite."""
    out: set[str] = set()
    for r in records:
        out.add(r.record_id)
        out.update(r.source.parents)
    return out


# --------------------------------------------------------------------------- snapshots


def _sql(db: Path, query: str) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


async def snapshot(w: World) -> dict[str, Any]:
    """State that a restart must reproduce exactly (ids and structure, never content)."""
    storage = w.eng._require_started()
    records: dict[str, list[tuple[str, str, str]]] = {}
    for ns in (NS_ILSE, NS_BRAM, NS_HOME):
        rows = await storage.list_records(ns)
        records[ns] = sorted((r.record_id, r.memory_type, r.status.value) for r in rows)
    # behavioural consent probe first: it must hold on a freshly started engine, before anything
    # has asked for the registry (the registry loads lazily at the read gate)
    marketing = w.eng.scope(NS_BRAM, principal="bram", viewer="bram", purpose="marketing")
    probes = {
        "bram_marketing": len(await marketing.recall("red tandem chain")),
        "bram_assist": len(await w.bram.recall("red tandem chain")),
    }
    chain = await w.eng.verify_audit_chain_v2()
    reg = await trust_consent.ensure_registry(w.eng)
    return {
        "probes": probes,
        "records": records,
        "lineage": _sql(w.db, "SELECT child_id, parent_id, role FROM lineage ORDER BY 1, 2, 3"),
        "outbox_states": _sql(w.db, "SELECT state, COUNT(*) FROM outbox GROUP BY state ORDER BY 1"),
        "audit": {"ok": chain.ok, "checked": chain.checked},
        "consent": reg.model_dump(mode="json"),
        "erasure": {k: v for k, v in w.eng.erasure_status().items() if k != "purges"},
    }


# --------------------------------------------------------------------------- the report


STEPS = (
    "test_step1_workload_written",
    "test_step2_sleep_and_read",
    "test_step3_restart_state",
    "test_step4_hard_erase",
    "test_step5_replay_migrate",
)


#: Behaviours the run observed that are not test failures but the owner should know (the steps
#: assert the bounds around each, so a change in kind shows up as a failure).
FINDINGS = {
    "F1": "FIXED (FX-EX1A): with write.participants=off a fact mined from a private turn of a "
    "multi-party namespace carried no visibility tag and was served to a non-participant. Under "
    "isolation.viewer_required a multi-party namespace now behaves as participants=session and "
    "derived records never widen their parents' visibility. The workload no longer sets "
    "write.participants.",
    "F2": "privacy.erasure_v2 hides dependants for re-derivation but keeps their rows and their "
    "WRITE events, which still carry the erased text on disk (not served by any read). Replay "
    "reproduces exactly that set. privacy.erasure_redact_hidden=on hard-forgets them instead (the "
    "milestone runs both ways; with it on no erased text remains in the sqlite file).",
    "F3": "OPEN BY DESIGN: a kept dependant (another live support parent) still lists the erased "
    "id in source.parents; the lineage table edge is removed. An id, no text.",
    "F4": "FIXED (FX-EX1A): Engine.verify_forget() is erasure_v2-aware: clean when every dependant "
    "is kept or hidden and queued; fields descendants_hidden_pending_rederive / _kept / _leaked.",
    "F5": "Memory.retrieve() with a handle purpose raised TypeError (fixed in src/memspine/api/"
    "handle.py).",
}


class Report:
    """Per-step outcome and timing, written to ``docs/v2/data/milestones/`` at module end."""

    def __init__(self) -> None:
        self.steps: dict[str, dict[str, Any]] = {}

    def step(self, name: str) -> Callable[[], Any]:
        started = time.perf_counter()

        def done(**facts: Any) -> None:
            self.steps[name] = {
                "result": "pass",
                "seconds": round(time.perf_counter() - started, 3),
                **facts,
            }

        return done

    def write(self) -> Path | None:
        if os.environ.get("EX1A_REPORT") != "1":
            return None
        root = Path(__file__).resolve().parents[2]
        out = (
            root / "docs" / "v2" / "data" / "milestones" / f"ex1a_{datetime.now(UTC):%Y-%m-%d}.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        for name in STEPS:
            self.steps.setdefault(name, {"result": "not_passed"})
        body = {
            "milestone": "EX-1a",
            "profile": "embedded (SQLite, hash embedder, stub LLM roles)",
            "flags_on": list(FLAGS_ON),
            "findings": FINDINGS,
            "steps": self.steps,
        }
        out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return out


@pytest.fixture(scope="module")
def report() -> Any:
    rep = Report()
    yield rep
    rep.write()


# --------------------------------------------------------------------------- the steps


async def test_step1_workload_written(world: World, report: Report) -> None:
    done = report.step("test_step1_workload_written")
    eng = world.eng
    cfg = eng.config
    assert cfg.write.lineage_table and cfg.workers.outbox and cfg.workers.transport == "inline"
    assert cfg.isolation.scope_keys_v2 and cfg.isolation.viewer_required
    assert cfg.privacy.erasure_v2 and cfg.retention.v2 and cfg.audit.chain_v2 and cfg.consent.v2
    assert cfg.graph.edge_compiler_v2 and cfg.offline.session_close_v2 and cfg.read.legs_v2
    storage = eng._require_started()
    counts = {ns: len(await storage.list_records(ns)) for ns in (NS_ILSE, NS_BRAM, NS_HOME)}
    turns = {
        ns: len(
            [
                r
                for r in await storage.list_records(ns, "episodic")
                if r.source.channel == "messages"
            ]
        )
        for ns in (NS_ILSE, NS_BRAM)
    }
    assert turns == {NS_ILSE: 6, NS_BRAM: 3}
    assert counts[NS_HOME] == 2
    done(records=counts, turns=turns)


async def test_step2_sleep_and_read(world: World, report: Report, stubs: dict[str, Any]) -> None:
    done = report.step("test_step2_sleep_and_read")
    sleep = await world.sleep()
    assert sleep  # the cycle ran
    secret_id = world.ids["secret_turn"]

    # answers cite the right records: the exact turn is in the cited set, and every cited record
    # is Ilse's (a turn or a record derived from her turns)
    ctx = await world.ilse.compose(f"I keep a beehive named {SECRET} behind the mill")
    cited = {r.record_id for r in ctx.records}
    assert secret_id in _cites(ctx.records), "the answer does not cite the beehive turn"
    assert all(r.namespace == NS_ILSE for r in ctx.records)
    hits = await world.ilse.recall(f"I keep a beehive named {SECRET} behind the mill")
    assert hits and all(r.namespace == NS_ILSE for r, _ in hits)
    assert secret_id in _cites([r for r, _ in hits])

    # no cross-owner leak: Bram's words never appear in Ilse's reads and the other way round
    for handle, foreign in ((world.ilse, "tandem"), (world.bram, SECRET)):
        got = await handle.recall(foreign)
        assert all(foreign not in r.content for r, _ in got), (handle.namespace, foreign)
        ctx2 = await handle.compose(foreign)
        assert all(foreign not in r.content for r in ctx2.records)
        assert all(r.namespace == handle.namespace for r in ctx2.records)
    other_ns = {r.namespace for r in await world.bram.retrieve()}
    assert other_ns <= {NS_BRAM}
    # the engine verbs refuse nothing but return nothing across owners
    assert await world.eng.get_record(secret_id, namespace=NS_BRAM) is None

    # a multi-party read without a viewer is refused; the listed participant gets her records
    with pytest.raises(ScopeRefused) as err:
        await world.eng.search("village fete", namespace=NS_HOME)
    assert err.value.reason == "viewer_required"
    seen = await world.home_ilse.recall("village fete lantern")
    texts = {r.content for r, _ in seen}
    assert any("village fete" in t for t in texts)
    eve = world.eng.scope(
        OwnerScope(tenant=TENANT, owner="household", owner_kind="group"),
        principal="eve",
        viewer="eve",
    )
    assert await eve.recall("village fete lantern") == []

    snap = await snapshot(world)
    assert snap["audit"]["ok"] is True
    assert snap["lineage"], "lineage_table wrote no edges"
    done(
        sleep_stages=len(sleep),
        cited=len(cited),
        lineage_edges=len(snap["lineage"]),
        window_calls=stubs["window"].calls,
        close_calls=stubs["close"].calls,
    )


async def test_step3_restart_state(world: World, report: Report) -> None:
    done = report.step("test_step3_restart_state")
    await world.sleep()
    before = await snapshot(world)
    await world.restart()
    after = await snapshot(world)
    assert before["records"] == after["records"], "record ids changed across restart"
    assert before["lineage"] == after["lineage"], "lineage edges changed across restart"
    assert after["audit"]["ok"] is True and after["audit"]["checked"] >= before["audit"]["checked"]
    assert before["consent"] == after["consent"], "consent registry not rebuilt from the log"
    assert before["erasure"] == after["erasure"]
    assert before["probes"] == after["probes"], "consent behaviour changed across restart"
    assert after["probes"]["bram_marketing"] == 0 < after["probes"]["bram_assist"]
    # outbox: nothing unfinished may be left over (empty or resumed to done)
    unfinished = [s for s, _ in after["outbox_states"] if s not in TERMINAL]
    assert not unfinished, after["outbox_states"]
    assert after["outbox_states"] == before["outbox_states"], "a restart re-ran or lost a job"
    # the rebuilt registry still gates: Bram's do-not-derive survived, reads still work
    assert world.ilse and await world.ilse.recall("beehive")
    done(
        records=sum(len(v) for v in after["records"].values()),
        lineage_edges=len(after["lineage"]),
        outbox=dict((s, n) for s, n in after["outbox_states"]),
        audit_checked=after["audit"]["checked"],
        consent_generation=after["consent"].get("generation"),
    )


async def test_step4_hard_erase(world: World, report: Report) -> None:
    done = report.step("test_step4_hard_erase")
    await world.sleep()
    storage = world.eng._require_started()
    secret_id = world.ids["secret_turn"]
    engine = storage._client.engine
    kids = [
        e.child for e in (await lineage_store.children_of(engine, [secret_id])).get(secret_id, [])
    ]
    assert kids, "the secret turn has no dependants: the erasure would prove nothing"
    hidden_kids: set[str] = set()

    # warm the read cache with a read that names the secret
    warm = await world.ilse.recall(SECRET)
    assert secret_id in _cites([r for r, _ in warm])
    warm_read = await world.ilse.read(SECRET)
    assert warm_read is not None

    await world.ilse.forget(secret_id, hard=True)

    status = world.eng.erasure_status()
    assert status["erased"] == 1 and status["hidden"] >= 1
    assert all(p["state"] == "done" for p in status["purges"].values()), status["purges"]  # type: ignore[union-attr]
    assert await storage.get_record(secret_id) is None
    hidden_kids = _gone(world) - {secret_id}
    assert hidden_kids, "no dependant was hidden"
    # a dependant that is not hidden (an unbound support record) must not carry the erased text
    for rid in kids:
        row = await storage.get_record(rid)
        if rid not in hidden_kids and row is not None:
            assert SECRET not in row.content, f"visible dependant {rid} carries the erased text"

    async def clean(label: str) -> None:
        for verb in (
            lambda: world.ilse.recall(SECRET),
            lambda: world.ilse.recall("beehive behind the mill"),
        ):
            got = await verb()
            ids = {r.record_id for r, _ in got}
            gone = _gone(world)
            assert secret_id in gone and not (ids & gone), f"{label}: erased/hidden id served"
            # source.parents of a kept (still-supported) summary may still name the erased id: a
            # dangling id, no text (finding F3); the lineage table must not
            assert not _sql(
                world.db,
                "SELECT 1 FROM lineage WHERE "
                f"parent_id = '{secret_id}' OR child_id = '{secret_id}'",
            ), f"{label}: lineage edge to the erased turn survives"
            assert all(SECRET not in r.content for r, _ in got), f"{label}: erased text served"
        ctx = await world.ilse.compose(SECRET)
        assert all(SECRET not in r.content for r in ctx.records), f"{label}: compose leaked"
        assert {r.record_id for r in ctx.records}.isdisjoint(_gone(world)), label
        read = await world.ilse.read(SECRET)
        recs = getattr(getattr(read, "context", read), "records", [])
        assert all(SECRET not in r.content for r in recs), f"{label}: read leaked"

    await clean("cache-hit")  # the same queries run again: a cache hit must not resurface it
    verdict = await world.eng.verify_forget(secret_id, NS_ILSE)
    # F4: erasure_v2-aware. A hidden dependant queued for re-derivation (or a kept one with another
    # live parent) is not a leak; only a dependant that is neither is.
    assert verdict["clean"] is True, verdict
    assert verdict["erasure_v2"] is True and verdict["descendants_leaked"] == [], verdict
    pending = set(verdict["descendants_hidden_pending_rederive"])  # type: ignore[arg-type]
    kept = set(verdict["descendants_kept"])  # type: ignore[arg-type]
    assert pending <= hidden_kids and not (pending & kept), verdict
    assert pending | kept == set(verdict["descendants_remaining"]), verdict  # type: ignore[arg-type]
    status_now = world.eng.erasure_status()
    if world.redact:
        # F2 on: hidden dependants were hard-forgotten; none waits for re-derivation
        assert not pending and not world.eng.erasure_queue(), verdict
        assert status_now["queued"] == 0
        for rid in hidden_kids:
            assert await storage.get_record(rid) is None, f"hidden dependant {rid} kept its row"
    else:
        assert pending, "no hidden dependant was left pending re-derivation"
        assert status_now["dropped_no_rederive"] == 0
    # Every physical check on the erased record itself must hold, and each remaining descendant
    # must be hidden or free of the erased text.
    for key in ("record_absent", "vector_absent", "vector_history_absent", "log_redacted"):
        assert verdict[key] is True, (key, verdict)
    assert not verdict["log_retained_fields"], verdict
    for rid in verdict["descendants_remaining"]:  # type: ignore[attr-defined]
        row = await storage.get_record(rid)
        assert rid in hidden_kids or row is None or SECRET not in row.content, rid

    manifest_events = [
        e
        for e in await storage.read_events(after_seq=0, limit=100000)
        if e.payload.get("marker") == "erasure_manifest"
    ]
    assert len(manifest_events) == 1
    assert SECRET not in json.dumps(manifest_events[0].payload), "manifest carries content"

    await world.restart()
    await clean("after-restart")
    status2 = world.eng.erasure_status()
    assert status2["erased"] == 1 and status2["hidden"] == status["hidden"]
    assert await world.ilse.recall(OTHER), "an unrelated Ilse record disappeared"
    assert await world.eng.verify_audit_chain_v2()
    if world.redact:
        # F2 on: no erased text anywhere in the sqlite file (nor its WAL) once the engine stops
        await world.eng.stop()
        holders = _byte_holders(world.db, SECRET)
        assert not holders, f"erased text still on disk: {holders}"
        await world.restart()
        assert (await world.eng.verify_forget(secret_id, NS_ILSE))["clean"] is True
    if world.rederive:
        # FU-ERASE: the sleep stage re-derives (or drops) every queued dependant and purges its row
        queued_ids = {str(i["record_id"]) for i in world.eng.erasure_queue()}
        assert queued_ids, "nothing queued for the erasure_rederive stage"
        stats = await world.sleep()
        assert stats["erasure_rederive"]["status"] in ("ok", "partial"), stats["erasure_rederive"]
        assert not world.eng.erasure_queue(), world.eng.erasure_queue()
        storage = world.eng._require_started()  # the restart replaced the storage handle
        for rid in queued_ids:
            assert await storage.get_record(rid) is None, f"queued dependant {rid} kept its row"
        after = world.eng.erasure_status()
        # re-derived ones leave the hidden set; dropped ones stay (row gone, entry kept as dropped)
        expect_hidden = status["hidden"] - len(queued_ids) + after["dropped_no_rederive"]
        assert after["hidden"] == expect_hidden, after
        await clean("after-rederive")
        verdict2 = await world.eng.verify_forget(secret_id, NS_ILSE)
        assert verdict2["clean"] is True and verdict2["descendants_leaked"] == [], verdict2
        await world.eng.stop()
        holders = _byte_holders(world.db, SECRET)
        assert not holders, f"erased text still on disk after re-derivation: {holders}"
        await world.restart()
        assert (await world.eng.verify_forget(secret_id, NS_ILSE))["clean"] is True
    done(
        redact_hidden=world.redact,
        rederive=world.rederive,
        dependants_hidden=status["hidden"],
        queued=status["queued"],
        generation=status["generation"],
        purges=len(status["purges"]),  # type: ignore[arg-type]
    )


async def test_step5_replay_migrate(world: World, report: Report, tmp_path: Path) -> None:
    done = report.step("test_step5_replay_migrate")
    await world.sleep()
    secret_id = world.ids["secret_turn"]
    await world.ilse.forget(secret_id, hard=True)
    hidden_ids = sorted(_gone(world) - {secret_id})
    await world.eng.stop()

    target = tmp_path / "replayed.db"
    t0 = time.perf_counter()
    rep = await migrate_sqlite(str(world.db), str(target))
    seconds = time.perf_counter() - t0
    assert rep.events_written == rep.events_read > 0
    assert rep.limited_rebuild and rep.not_rebuilt  # the report names what it did not rebuild

    live = json.loads(await _dump(world.db))
    replay = json.loads(await _dump(target))
    # where the erased text still sits: the live store and the replay must agree, and the only
    # holders allowed are hidden dependants (retained for re-derivation, finding F2)
    hidden = set(hidden_ids)
    live_hold, replay_hold = _holders(live), _holders(replay)
    if world.redact:  # F2 on: no holder at all, in the live store or the replay
        assert not live_hold["events"] and not live_hold["records"], live_hold
        assert not replay_hold["events"] and not replay_hold["records"], replay_hold
    assert set(live_hold["events"]) <= hidden, f"erased text in live log: {live_hold['events']}"
    assert replay_hold["events"] == live_hold["events"], (
        "the replayed log differs from the live log"
    )
    assert set(live_hold["records"]) <= hidden, "a visible live record carries the erased text"
    assert set(replay_hold["records"]) <= hidden, (
        "a visible replayed record carries the erased text"
    )
    live_ids = {r["record_id"] for r in live["records"]}
    replay_ids = {r["record_id"] for r in replay["records"]}
    assert secret_id not in replay_ids
    missing = live_ids - replay_ids
    assert not missing, f"records lost in replay: {sorted(missing)}"
    # the rows a replay restores must match the live rows field for field; layers the report
    # lists as not rebuilt (L2/L4/L5 derived records, vectors, caches) are not compared
    logged = {r["record_id"]: r for r in replay["records"]}
    diffs = [rid for rid in sorted(live_ids) if _core(_by(live)[rid]) != _core(logged[rid])]
    assert not diffs, f"projected records differ after replay: {diffs[:5]}"

    # an engine opened on the replayed store: erased content stays erased through every read
    fresh = await memspine.open(dotenv_path=None, **_overrides(target))
    try:
        got = await fresh.scope(NS_ILSE, principal="ilse", viewer="ilse").recall(SECRET)
        assert all(SECRET not in r.content for r, _ in got)
        assert fresh.erasure_status()["erased"] == 1
    finally:
        await fresh.stop()
    done(
        events=rep.events_written,
        by_kind=dict(sorted(rep.by_kind.items())),
        projected=dict(sorted(rep.projected.items())),
        records_live=len(live_ids),
        records_replayed=len(replay_ids),
        hidden_dependants_holding_erased_text=len(live_hold["records"]),
        not_rebuilt=sorted(rep.not_rebuilt),
        migrate_seconds=round(seconds, 3),
    )


# --------------------------------------------------------------------------- replay helpers


async def _dump(db: Path) -> bytes:
    from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
    from memspine.services.storage.sqlite.engine import SQLiteStorage

    client = SQLiteClient(SQLiteSpec(str(db)))
    await client.open()
    try:
        storage = SQLiteStorage(client)
        await storage.start()
        return await dump_store(storage)
    finally:
        await client.close()


def _byte_holders(db: Path, needle: str) -> list[str]:
    """Files among the sqlite database and its sidecars whose bytes contain ``needle``."""
    raw = needle.encode()
    return [
        p.name
        for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
        if p.exists() and raw in p.read_bytes()
    ]


def _holders(dump: dict[str, Any]) -> dict[str, list[str]]:
    needle = SECRET
    return {
        "events": [
            str((e["payload"].get("record") or {}).get("record_id", f"{e['seq']}:{e['kind']}"))
            for e in dump["events"]
            if needle in json.dumps(e["payload"])
        ],
        "records": [r["record_id"] for r in dump["records"] if needle in json.dumps(r)],
    }


def _by(dump: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["record_id"]: r for r in dump["records"]}


def _core(rec: dict[str, Any]) -> dict[str, Any]:
    """Fields a replay must reproduce (no timestamps of projection, no access counters)."""
    keep = ("record_id", "namespace", "memory_type", "content", "status", "layer")
    return {k: rec.get(k) for k in keep}
