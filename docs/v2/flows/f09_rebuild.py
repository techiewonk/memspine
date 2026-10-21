"""Flow 9: rebuild the read model from the log, export, and migrate by replay (library and CLI)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from flows._common import SRC, Ctx, Step, make_engine

NAME = "rebuild"
TITLE = "Rebuild, export and migrate: the log is the truth, the read model is derived"


async def snapshot(storage: Any, ns: str) -> list[tuple[str, str, str]]:
    """(record_id, layer, status) for every row of the namespace, sorted."""
    return sorted((r.record_id, r.layer, r.status.value) for r in await storage.list_records(ns))


async def build_state(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    fact = await e.write(
        "Bram lives in Delft.",
        namespace=ns,
        memory_type="semantic",
        entity="Bram",
        attribute="city",
    )
    cello = await e.write(
        "Ilse teaches cello at a school in Leiden.",
        namespace=ns,
        memory_type="episodic",
        entity="Ilse",
    )
    bike = await e.write("Noor rides a green bike to work.", namespace=ns, memory_type="episodic")
    secret = await e.write("Tomasz's locker code is 5520.", namespace=ns, memory_type="episodic")
    moved = await e.correct(
        ("Bram", "city"), "Bram lives in Haarlem.", namespace=ns, reason="moved"
    )
    await e.forget(bike.record_id, namespace=ns)
    await e.forget(secret.record_id, namespace=ns, hard=True, reason="user asked")
    ctx.state.update(
        fact=fact.record_id,
        cello=cello.record_id,
        bike=bike.record_id,
        secret=secret.record_id,
        moved=moved.record_id,
    )
    snap = await snapshot(ctx.storage, ns)
    ctx.state["snap"] = snap
    ctx.show("rows (id, layer, status)", [(i[:6], lay, st) for i, lay, st in snap])
    ctx.expect("four rows: the hard-forgotten row is physically gone", 4, len(snap))
    statuses = sorted(st for _, _, st in snap)
    ctx.expect(
        "statuses: old city archived, bike soft-deleted, cello and new city active",
        ["activated", "activated", "archived", "deleted"],
        statuses,
    )
    ctx.check(
        "the hard-forgotten id has no row", ctx.state["secret"] not in {i for i, _, _ in snap}
    )
    hits = await e.search("Bram lives", namespace=ns, top_k=3)
    ctx.state["hits"] = [r.record_id for r, _ in hits]
    ctx.check(
        "search serves the corrected city, never the old one",
        ctx.state["moved"] in ctx.state["hits"] and ctx.state["fact"] not in ctx.state["hits"],
    )


async def do_rebuild(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    before = ctx.state["snap"]
    counts = await e.rebuild()
    after = await snapshot(ctx.storage, ns)
    ctx.show("rebuild() counts per projector", counts)
    ctx.show("rows after", [(i[:6], lay, st) for i, lay, st in after])
    ctx.check(
        "every projector replayed the log",
        bool(counts) and all(v > 0 for v in counts.values()),
        counts,
    )
    ctx.expect("ids, layers and statuses are identical after the rebuild", before, after)
    hits = await e.search("Bram lives", namespace=ns, top_k=3)
    ctx.expect("search gives the same answer", ctx.state["hits"], [r.record_id for r, _ in hits])
    gone = await e.search("locker code 5520", namespace=ns, top_k=5)
    ctx.check(
        "the hard-forgotten record did not come back",
        all(r.record_id != ctx.state["secret"] for r, _ in gone),
    )
    soft = await e.search("green bike to work", namespace=ns, top_k=5)
    ctx.check(
        "the soft-forgotten record stays hidden",
        all(r.record_id != ctx.state["bike"] for r, _ in soft),
    )


async def rebuild_twice(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    first = await e.rebuild()
    second = await e.rebuild()
    ctx.show("counts", {"first": first, "second": second})
    ctx.expect("a second rebuild replays the same number of events", first, second)
    ctx.expect("and changes nothing", ctx.state["snap"], await snapshot(ctx.storage, ns))


async def do_export(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    lines = await e.export(ns)
    parsed = [json.loads(x) for x in lines]
    head, recs = parsed[0], parsed[1:]
    ctx.show("header", head)
    ctx.show(
        "record lines",
        [
            (p["record"]["record_id"][:6], p["record"]["status"], p["record"]["content"])
            for p in recs
        ],
    )
    ctx.expect("line 1 is the export header", "export", head["type"])
    ctx.expect("header counts the record lines", len(recs), head["records"])
    ids = {p["record"]["record_id"] for p in recs}
    ctx.check(
        "soft-forgotten and hard-forgotten records are left out",
        ctx.state["bike"] not in ids and ctx.state["secret"] not in ids,
    )
    ctx.check(
        "live and archived versions are in (history kept)",
        {ctx.state["fact"], ctx.state["moved"], ctx.state["cello"]} == ids,
        len(ids),
    )
    ctx.check("hard-erased text appears nowhere in the export", "5520" not in "".join(lines))
    ctx.expect("same data, same bytes", lines, await e.export(ns))
    no_hist = await e.export(ns, include_history=False)
    with_h = [json.loads(x)["record"] for x in lines[1:]]
    without = [json.loads(x)["record"] for x in no_hist[1:]]
    ctx.expect(
        "include_history=False keeps the same three records (archived one included)",
        3,
        len(without),
    )
    ctx.check(
        "but drops each record's own `history` field",
        all("history" in r for r in with_h) and all("history" not in r for r in without),
    )


async def export_variants(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    sub = await e.export(ns, subject="Ilse")
    sub_ids = [json.loads(x)["record"]["record_id"] for x in sub[1:]]
    ctx.show("export(subject='Ilse') record ids", [i[:6] for i in sub_ids])
    ctx.expect("a subject export holds only that subject's records", [ctx.state["cello"]], sub_ids)
    ev = await e.export(ns, include_events=True)
    kinds = [json.loads(x).get("type") for x in ev]
    ctx.show("line types with include_events", sorted(set(kinds)))
    ctx.check(
        "log events are appended after the records",
        "event" in kinds and kinds.index("event") > kinds.index("record"),
    )
    ctx.check("hard-erased content is already redacted in the events", "5520" not in "".join(ev))


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, "PYTHONPATH": str(SRC.parent), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "memspine.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def make_source_db(ctx: Ctx) -> None:
    root = Path(tempfile.mkdtemp(prefix="memspine_f9_"))
    src = root / "source.db"
    ctx.state.update(root=root, src=src)
    eng = await make_engine({"storage": {"settings": {"path": str(src), "busy_timeout_s": 5.0}}})
    try:
        ns = ctx.ns
        a = await eng.write(
            "Bram lives in Delft.",
            namespace=ns,
            memory_type="semantic",
            entity="Bram",
            attribute="city",
        )
        await eng.write(
            "Ilse teaches cello at a school in Leiden.", namespace=ns, memory_type="episodic"
        )
        c = await eng.write(
            "Noor rides a green bike to work.", namespace=ns, memory_type="episodic"
        )
        await eng.correct(("Bram", "city"), "Bram lives in Haarlem.", namespace=ns)
        await eng.forget(c.record_id, namespace=ns)
        ctx.state["src_snap"] = await snapshot(eng._require_started(), ns)
        ctx.state["a_src"] = a.record_id
    finally:
        await eng.stop()
    ctx.show("temp dir", str(root))
    ctx.show(
        "source rows (id, layer, status)",
        [(i[:6], lay, st) for i, lay, st in ctx.state["src_snap"]],
    )
    ctx.check(
        "file-backed source store exists",
        src.exists() and src.stat().st_size > 0,
        f"{src.stat().st_size} bytes",
    )
    ctx.expect("four rows in the source", 4, len(ctx.state["src_snap"]))


async def migrate_function(ctx: Ctx) -> None:
    from memspine.store.migrate import migrate_sqlite

    src: Path = ctx.state["src"]
    dst: Path = ctx.state["root"] / "migrated_lib.db"
    before = _sha(src)
    rep = await migrate_sqlite(str(src), str(dst))
    d = rep.as_dict()
    ctx.show(
        "MigrationReport",
        {
            k: d[k]
            for k in (
                "events_read",
                "events_upcast",
                "events_written",
                "by_kind",
                "projected",
                "limited_rebuild",
            )
        },
    )
    ctx.show("not_rebuilt (advertised)", list(d["not_rebuilt"]))
    ctx.expect("every event was written to the target", d["events_read"], d["events_written"])
    ctx.check(
        "limited rebuild is advertised, not hidden",
        d["limited_rebuild"] and "vector index" in d["not_rebuilt"],
    )
    ctx.expect("the source file is byte-identical afterwards (never written)", before, _sha(src))
    eng = await make_engine({"storage": {"settings": {"path": str(dst), "busy_timeout_s": 5.0}}})
    try:
        got = await snapshot(eng._require_started(), ctx.ns)
        ctx.show("target rows (id, layer, status)", [(i[:6], lay, st) for i, lay, st in got])
        ctx.expect("ids, layers and statuses survive the migration", ctx.state["src_snap"], got)
        hits = await eng.search("where does Bram live", namespace=ctx.ns, top_k=3)
        ctx.show("search on the migrated store", [r.content for r, _ in hits])
        ctx.check(
            "the migrated store answers with the corrected city, not the old one",
            "Bram lives in Haarlem." in [r.content for r, _ in hits]
            and "Bram lives in Delft." not in [r.content for r, _ in hits],
        )
    finally:
        await eng.stop()
    again = ""
    try:
        await migrate_sqlite(str(src), str(dst))
    except Exception as exc:
        again = f"{type(exc).__name__}: {exc}"
    ctx.show("migrate into an existing target", again)
    ctx.check("an existing target is refused", "RebuildUnavailableError" in again)


async def migrate_cli(ctx: Ctx) -> None:
    import yaml

    src: Path = ctx.state["src"]
    dst: Path = ctx.state["root"] / "migrated_cli.db"
    cmd = f"{sys.executable} -m memspine.cli migrate {src} {dst}"
    ctx.show("command", cmd)
    done = _run_cli("migrate", str(src), str(dst))
    ctx.show("exit code", done.returncode)
    ctx.show("stdout", done.stdout)
    ctx.expect("CLI exits 0", 0, done.returncode)
    rep = yaml.safe_load(done.stdout)
    ctx.expect(
        "the report counts all six events (5 writes/corrections + 1 forget)",
        (6, 6),
        (rep["events_read"], rep["events_written"]),
    )
    eng = await make_engine({"storage": {"settings": {"path": str(dst), "busy_timeout_s": 5.0}}})
    try:
        got = await snapshot(eng._require_started(), ctx.ns)
        ctx.expect(
            "counts survive: same ids, layers and statuses as the source",
            ctx.state["src_snap"],
            got,
        )
        ctx.expect("record count survives", len(ctx.state["src_snap"]), len(got))
    finally:
        await eng.stop()
    again = _run_cli("migrate", str(src), str(dst))
    ctx.show("second run stderr", again.stderr.strip())
    ctx.expect("a second run into the same target exits 1", 1, again.returncode)
    ctx.check("and says why", "cannot migrate" in again.stderr)


STEPS = [
    Step(
        "F9.1",
        "build a state: keyed fact, correction, soft and hard forget; snapshot ids+layers+status",
        build_state,
        "api/engine.py:write, correct, forget",
    ),
    Step(
        "F9.2",
        "rebuild(): records and layers identical, search unchanged, erasures stay erased",
        do_rebuild,
        "api/engine.py:rebuild; store/replay.py:rebuild",
    ),
    Step("F9.3", "rebuild twice: idempotent", rebuild_twice, "api/engine.py:rebuild"),
    Step(
        "F9.4",
        "export(): header, records, history, redaction, deterministic bytes",
        do_export,
        "api/engine.py:export",
    ),
    Step("F9.5", "export(subject=...) and include_events", export_variants, "api/engine.py:export"),
    Step(
        "F9.6",
        "make a file-backed source store in a temp dir",
        make_source_db,
        "services/storage/sqlite",
    ),
    Step(
        "F9.7",
        "migrate_sqlite(): replay the log into a fresh store; counts survive; source untouched",
        migrate_function,
        "store/migrate.py:migrate_sqlite, ReplayBuilder",
    ),
    Step(
        "F9.8", "CLI `memspine migrate SRC DST` via subprocess", migrate_cli, "cli.py:migrate_cmd"
    ),
]
