"""Manual checks for the memory-layer fixes (PR #5), one per bug id.

CPU only, in-memory stores, hash embedder: no model, no network, no GPU. Each check prints what it
did, the value it expected and the value it got, then PASS or FAIL. Run one at a time:

    python docs/v2/layer_fix_checks.py --list
    python docs/v2/layer_fix_checks.py MF-01
    python docs/v2/layer_fix_checks.py MF-01 MF-03 MF-07
    python docs/v2/layer_fix_checks.py all

Run from the repo root with PYTHONPATH=src (and evals for MF-16). Invented names only.
Tutorial: docs/v2/LAYER_FIX_TESTING.md.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from memspine import Engine
from memspine.store.records import MemoryRecord, SourceInfo

RESULTS: list[tuple[str, bool]] = []

import logging

logging.disable(logging.INFO)  # keep engine info logs out of the check output
try:
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
except ImportError:
    pass


def check(bug: str, what: str, expected: object, got: object) -> None:
    ok = expected == got
    RESULTS.append((bug, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {what}: expected {expected!r}, got {got!r}")


def rec(
    channel: str, content: str = "Synthetic derived note about Noor", **kw: object
) -> MemoryRecord:
    return MemoryRecord(
        namespace="audit",
        memory_type="semantic",
        content=content,
        source=SourceInfo(role="assistant", channel=channel, parents=["p0"]),
        **kw,
    )


async def engine(lineage: bool = False) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "provider": "sqlite",
            "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"lineage_table": lineage},
    )
    await eng.start()
    return eng


# ---------------------------------------------------------------- write door (LF-A)
async def mf01() -> None:
    """Session-close episode and summary land on L2, lineage on and off, and survive rebuild."""
    for lineage in (False, True):
        eng = await engine(lineage)
        try:
            parent = await eng.write(
                "Noor talked about a pottery class.", namespace="audit", memory_type="episodic"
            )
            for kind in ("episode", "summary"):
                r = await eng._deposit_session_record(
                    "audit",
                    kind,
                    f"Noor talked about a pottery class ({kind}).",
                    [parent.record_id],
                    datetime(2025, 3, 1, tzinfo=UTC),
                    f"s-{kind}",
                )
                storage = eng._require_started()
                check(
                    "MF-01",
                    f"lineage={lineage} {kind}: stored layer",
                    "L2",
                    (await storage.get_record(r.record_id)).layer,
                )
                await eng.rebuild()
                check(
                    "MF-01",
                    f"lineage={lineage} {kind}: layer after rebuild",
                    "L2",
                    (await storage.get_record(r.record_id)).layer,
                )
        finally:
            await eng.stop()


async def mf02() -> None:
    """Offline summaries stamped with the route name 'summary' keep their channel's layer."""
    from memspine.derive.write import stamp_derived

    for channel, want in [
        ("consolidation", "L2"),
        ("entity_summary", "L3"),
        ("reorganize", "L3"),
        ("community_summary", "L3"),
        ("multires", "L2"),
        ("observation", "L4"),
        ("standing", "L5"),
    ]:
        # old positional call shape, as offline_door.stamp_summary used it
        check(
            "MF-02",
            f"stamp_derived(rec[{channel}], 'summary', site)",
            want,
            stamp_derived(rec(channel), "summary", channel).layer,
        )


async def mf03() -> None:
    """An invalid explicit layer raises instead of silently falling back."""
    from memspine.derive.write import stamp_derived

    for bad in ("L9", "episode", "summary", ""):
        try:
            stamp_derived(rec("consolidation"), layer=bad)
            got = "no error"
        except ValueError as exc:
            got = "ValueError" if repr(bad) in str(exc) else f"ValueError without value: {exc}"
        check("MF-03", f"layer={bad!r}", "ValueError", got)
    check("MF-03", "layer='L2' still accepted", "L2", stamp_derived(rec("x"), layer="L2").layer)


async def mf04() -> None:
    """An explicit layer survives the lineage-OFF door (was dropped to L1)."""
    from memspine.derive.write import write_derived

    eng = await engine(lineage=False)
    try:
        storage = eng._require_started()
        for channel, layer in [
            ("observation", "L4"),
            ("standing", "L5"),
            ("community_summary", "L3"),
        ]:
            async with eng._write_locks.setdefault("audit", asyncio.Lock()):
                w = await write_derived(
                    eng,
                    storage,
                    "audit",
                    rec(channel, f"Ilse view via {channel}"),
                    "summary",
                    "system",
                    layer=layer,
                    site=channel,
                )
            check(
                "MF-04",
                f"lineage off, {channel} with layer={layer}",
                layer,
                (await storage.get_record(w.record_id)).layer,
            )
    finally:
        await eng.stop()


async def new02() -> None:
    """The derivation fingerprint no longer depends on the layer label."""
    from memspine.derive.write import stamp_derived

    a = stamp_derived(rec("consolidation"), site="s", layer="L2").fingerprint
    b = stamp_derived(rec("consolidation"), site="s", layer="L3").fingerprint
    check("NEW-02", "same content, layer L2 vs L3: same fingerprint", True, a == b)


async def new03() -> None:
    """The second argument is a route name; the layer comes from record.memory_type + channel."""
    import inspect

    from memspine.derive.write import stamp_derived, write_derived

    check(
        "NEW-03",
        "stamp_derived 2nd param name",
        "route",
        list(inspect.signature(stamp_derived).parameters)[1],
    )
    check(
        "NEW-03",
        "write_derived 5th param name",
        "route",
        list(inspect.signature(write_derived).parameters)[4],
    )


# ---------------------------------------------------------------- persistence (LF-E)
async def mf05() -> None:
    """Layer is always in the event payload; the v2->v3 upcaster fixes producer records only."""
    from memspine.store.layer_classify import corrected_layer

    eng = await engine()
    try:
        r = await eng.write("Bram likes rye bread.", namespace="audit", memory_type="semantic")
        events = await eng._require_started().read_events(after_seq=0, limit=1000)
        payload = next(
            e.payload["record"]
            for e in events
            if (e.payload or {}).get("record", {}).get("record_id") == r.record_id
        )
        check("MF-05", "plain L1 fact: 'layer' present in WRITE payload", True, "layer" in payload)
    finally:
        await eng.stop()
    for stored, channel, want in [
        ("L0", "session_close", "L2"),
        ("L1", "consolidation", "L2"),
        (None, "entity_summary", "L3"),
        ("L1", "observation", "L4"),
        ("L0", "standing", "L5"),
        ("L0", "", None),
        ("L1", "chat", None),
        ("L2", "session_close", None),
    ]:
        check(
            "MF-05",
            f"upcast layer={stored!r} channel={channel!r}",
            want,
            corrected_layer(stored, channel),
        )


async def mf08() -> None:
    """Postgres backfill migration exists and chains onto the previous head (needs PG to run)."""
    import importlib

    m = importlib.import_module(
        "memspine.services.storage.postgres.alembic.versions.pg0011_layer_backfill"
    )
    check("MF-08", "revision", "pg_lf_e_layer_backfill", m.revision)
    check("MF-08", "down_revision", "pg_w202_stamping", m.down_revision)
    print("  (run the real backfill on a Postgres copy: see LAYER_FIX_TESTING.md step MF-08)")


async def mf09() -> None:
    """NULL-layer rows are counted by the same classifier hydration uses (not as L0)."""
    from memspine.store.layer_classify import classify_stored_layer

    check(
        "MF-09",
        "NULL layer, semantic/session_close",
        "L2",
        classify_stored_layer("semantic", {"channel": "session_close"}),
    )
    check("MF-09", "NULL layer, semantic/no channel", "L1", classify_stored_layer("semantic", {}))
    check("MF-09", "NULL layer, episodic", "L0", classify_stored_layer("episodic", None))
    print(
        "  (end-to-end on a real table: pytest tests/unit/contract/test_layer_contract.py -k layer_counts)"
    )


# ---------------------------------------------------------------- fact policy (LF-C)
async def mf10() -> None:
    """Derived views are never input facts, and both predicates agree."""
    from memspine.derive.pipeline.stages.envelope_adapter import is_fact
    from memspine.derive.pipeline.stages.offline_common import is_input_fact

    for channel in (
        "observation",
        "standing",
        "session_close",
        "entity_summary",
        "community_summary",
        "multires",
        "consolidation",
        "reorganize",
    ):
        c = rec(channel, entity="Noor", attribute="hobby")
        check(
            "MF-10",
            f"{channel}: is_fact / is_input_fact",
            (False, False),
            (is_fact(c), is_input_fact(c)),
        )
    fact = MemoryRecord(
        namespace="audit",
        memory_type="semantic",
        content="Noor plays chess",
        entity="Noor",
        attribute="hobby",
    )
    check(
        "MF-10",
        "a real keyed fact is still a fact",
        (True, True),
        (is_fact(fact), is_input_fact(fact)),
    )


async def new01() -> None:
    """Near-duplicate merge (histories) needs a keyed fact; an unkeyed summary is excluded."""
    from memspine.derive.fact_policy import FactPurpose, is_evidence_fact

    unkeyed = MemoryRecord(namespace="audit", memory_type="semantic", content="Noor plays chess")
    keyed = MemoryRecord(
        namespace="audit", memory_type="semantic", content="Noor plays chess", entity="Noor"
    )
    check(
        "NEW-01",
        "unkeyed fact for HISTORIES_MERGE",
        False,
        is_evidence_fact(unkeyed, FactPurpose.HISTORIES_MERGE),
    )
    check(
        "NEW-01",
        "entity-keyed fact for HISTORIES_MERGE",
        True,
        is_evidence_fact(keyed, FactPurpose.HISTORIES_MERGE),
    )
    check(
        "NEW-01",
        "session_close view for HISTORIES_MERGE",
        False,
        is_evidence_fact(rec("session_close", entity="Noor"), FactPurpose.HISTORIES_MERGE),
    )


# ---------------------------------------------------------------- read legs (LF-B, LF-D)
async def mf06() -> None:
    """Episode lift follows only the episode's own support edges, not the stub it extends."""
    from memspine.read.pipeline.lift import _narrow_episode_lineage
    from memspine.schemas.durable.lineage import LineageEdge

    lineage = {
        "ep2": [
            LineageEdge(child="ep2", parent="t3", role="support"),
            LineageEdge(child="ep2", parent="ep1", role="version_of"),
        ],
        "ep1": [LineageEdge(child="ep1", parent="t1", role="support")],
    }
    out = _narrow_episode_lineage(lineage, ["ep2"])
    check(
        "MF-06",
        "turns reachable from ep2",
        ["t3"],
        sorted(e.parent for es in out.values() for e in es),
    )


async def mf07() -> None:
    """Graph leg works from L1-backed edges with no L3 records; empty reason is no_edges."""
    from memspine.read.legs.core import LegContext
    from memspine.read.legs.graph import HopEdge, OneHopConvergent
    from memspine.store.layer_registry import LayerRegistry, LayerTemplate

    fact = MemoryRecord(
        namespace="audit", memory_type="semantic", content="Tomasz and Ilse met in Gdansk"
    )
    reg = LayerRegistry.of("audit", LayerTemplate(), {"L1": 1})
    ctx = LegContext(
        query="where did they meet",
        namespace="audit",
        fetch_k=5,
        records=[fact],
        layer_registry=reg,
        graph_seeds=["tomasz", "ilse"],
        graph_edges=[
            HopEdge("tomasz", "gdansk", fact.record_id),
            HopEdge("ilse", "gdansk", fact.record_id),
        ],
    )
    out = await OneHopConvergent().run(ctx)
    check("MF-07", "L1-only namespace with live edges: hits", 1, len(out.hits))
    ctx.graph_edges = [HopEdge("tomasz", "gdansk", "deleted-record")]
    check(
        "MF-07",
        "edges whose record is gone: reason",
        "no_edges",
        (await OneHopConvergent().run(ctx)).empty_reason,
    )


def _l2(channel: str, content: str, tags: list[str] | None = None) -> MemoryRecord:
    return MemoryRecord(
        namespace="audit",
        memory_type="semantic",
        content=content,
        layer="L2",
        tags=tags or [],
        source=SourceInfo(role="assistant", channel=channel),
    )


async def mf11() -> None:
    """Episode leg searches episode kinds only; consolidation/anticipation are opt-in."""
    from memspine.read.legs.core import LegContext
    from memspine.read.legs.episode import EpisodeLeg

    only_digest = [_l2("consolidation", "Noor pottery digest")]
    ctx = LegContext(query="pottery", namespace="audit", fetch_k=5, records=only_digest)
    check(
        "MF-11",
        "only a consolidation digest: reason",
        "no_records:episode",
        (await EpisodeLeg().run(ctx)).empty_reason,
    )
    check(
        "MF-11",
        "kinds=('consolidation',) opts it in: hits",
        1,
        len((await EpisodeLeg(kinds=("consolidation",)).run(ctx)).hits),
    )
    ctx = LegContext(
        query="pottery",
        namespace="audit",
        fetch_k=5,
        records=[_l2("session_close", "Noor pottery session")],
    )
    check("MF-11", "session_close summary: hits", 1, len((await EpisodeLeg().run(ctx)).hits))
    try:
        EpisodeLeg(kinds=("bogus",))
        got = "no error"
    except ValueError:
        got = "ValueError"
    check("MF-11", "unknown kind", "ValueError", got)


async def mf12() -> None:
    """Vector search widens (x2 per round, cap 1024) until it holds fetch_k episode hits."""
    from types import SimpleNamespace

    from memspine.read.legs.core import LegContext
    from memspine.read.legs.episode import EpisodeLeg

    episodes = [_l2("session_close", f"Bram session {i}") for i in range(3)]
    noise = [SimpleNamespace(record_id=f"turn-{i}") for i in range(2000)]
    asked: list[int] = []

    async def vector_search(k: int):  # episodes ranked after 300 other-layer hits
        asked.append(k)
        ranked = (
            noise[:300] + [SimpleNamespace(record_id=e.record_id) for e in episodes] + noise[300:]
        )
        return ranked[:k]

    ctx = LegContext(
        query="Bram session",
        namespace="audit",
        fetch_k=3,
        records=episodes,
        vector_search=vector_search,
    )
    await EpisodeLeg().run(ctx)
    check("MF-12", "k asked per round", [50, 100, 200, 400], asked)


async def mf14() -> None:
    """Registry keeps configured / available / populated / selected apart."""
    from memspine.store.layer_registry import LayerRegistry, LayerTemplate

    reg = LayerRegistry.of("audit", LayerTemplate(), {"L1": 4}, selected=["L3"]).with_artifacts(
        "L3", 7
    )
    check("MF-14", "L3 populated (records)", False, reg.is_populated("L3"))
    check("MF-14", "L3 has artifacts (edges)", 7, reg.artifact_count("L3"))
    check("MF-14", "L3 available", True, reg.is_available("L3"))
    check("MF-14", "L3 selected", True, reg.is_selected("L3"))
    check("MF-14", "L2 available (nothing)", False, reg.is_available("L2"))


# ---------------------------------------------------------------- profile card, docs (LF-F, LF-G)
async def mf13() -> None:
    """read.profile_card_header exists (off) and requires offline.profile_card_v2."""
    from memspine.config.loader import load_config
    from memspine.config.requires import missing_needs

    base = load_config(template="core").config
    check("MF-13", "default read.profile_card_header", False, base.read.profile_card_header)
    from memspine.exceptions import ConfigError

    try:
        load_config(template="core", overrides={"read": {"profile_card_header": True}})
        got = "loaded (flag would be silently inert)"
    except ConfigError as exc:
        got = "ConfigError" if "offline.profile_card_v2" in str(exc) else f"ConfigError: {exc}"
    check("MF-13", "header on, producer off", "ConfigError", got)
    both = load_config(
        template="core",
        overrides={
            "read": {"profile_card_header": True},
            "offline": {"profile_card_v2": True, "session_close_v2": True},
        },
    ).config
    check(
        "MF-13",
        "header + profile_card_v2 + session_close_v2: missing needs",
        [],
        missing_needs(both, "read.profile_card_header"),
    )


async def mf15() -> None:
    """Contract tests for the whole layer path (pytest)."""
    print("  run: pytest tests/unit/contract/test_layer_contract.py -v   (17 tests)")


async def mf16() -> None:
    """Effective memory types / layers / legs / producers of an arm."""
    print(
        "  run: PYTHONPATH=src;evals python -m memspine_evals.memory_type_matrix evals/arms/v2-PICK3-j.json"
    )


CHECKS = {
    "MF-01": mf01,
    "MF-02": mf02,
    "MF-03": mf03,
    "MF-04": mf04,
    "NEW-02": new02,
    "NEW-03": new03,
    "MF-05": mf05,
    "MF-08": mf08,
    "MF-09": mf09,
    "MF-10": mf10,
    "NEW-01": new01,
    "MF-06": mf06,
    "MF-07": mf07,
    "MF-11": mf11,
    "MF-12": mf12,
    "MF-14": mf14,
    "MF-13": mf13,
    "MF-15": mf15,
    "MF-16": mf16,
}


async def main(ids: list[str]) -> int:
    for bug in ids:
        fn = CHECKS[bug]
        print(f"\n{bug}: {fn.__doc__.strip().splitlines()[0]}")
        try:
            await fn()
        except Exception as exc:  # report and continue, so one broken check does not hide the rest
            RESULTS.append((bug, False))
            print(f"  [ERROR] {type(exc).__name__}: {exc}")
    bad = sorted({b for b, ok in RESULTS if not ok})
    print(
        f"\n{sum(ok for _, ok in RESULTS)}/{len(RESULTS)} checks passed"
        + (f"; failing: {bad}" if bad else "")
    )
    return 1 if bad else 0


if __name__ == "__main__":
    args = [a.upper() for a in sys.argv[1:]]
    if not args or args == ["--LIST"]:
        for k, f in CHECKS.items():
            print(f"{k:7} {f.__doc__.strip().splitlines()[0]}")
        sys.exit(0)
    ids = list(CHECKS) if args == ["ALL"] else args
    unknown = [i for i in ids if i not in CHECKS]
    if unknown:
        sys.exit(f"unknown id(s) {unknown}; use --list")
    sys.exit(asyncio.run(main(ids)))
