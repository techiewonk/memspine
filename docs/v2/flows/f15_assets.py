"""Flow 15: document ingest and asset references (chunks -> resource records, asset -> reference + description)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from flows._common import Ctx, Step

NAME = "assets"
TITLE = "Documents and assets: ingest (chunks), re-ingest idempotence, new version, ingest_asset (reference + description)"
CONFIG = {
    "memories": {"resource": {"enabled": True}},
    "write": {"asset_describer": "ingest"},
}


def _doc_text(tail: str) -> str:
    return (
        "Bram keeps two beehives behind the allotment shed.\n\n"
        + ("Honey notes for the spring harvest. " * 40)
        + "\n\n"
        + (tail + " ") * 40
    )


async def ingest_doc(ctx: Ctx) -> None:
    e = ctx.engine
    d = Path(tempfile.mkdtemp(prefix="flowcheck_"))
    ctx.state["dir"] = d
    doc = d / "hives.txt"
    doc.write_text(_doc_text("Queen notes for the second hive."), encoding="utf-8")
    ctx.state["doc"] = doc
    recs = await e.ingest_data(doc, namespace=ctx.ns)
    ctx.state["chunks"] = [r.record_id for r in recs]
    ctx.show("chunks", [(r.source.message_id, len(r.content), r.tags) for r in recs])
    ctx.check("a document longer than one chunk gives several records", len(recs) >= 2, len(recs))
    ctx.check("all chunks are resource records", all(r.memory_type == "resource" for r in recs))
    ctx.expect(
        "chunk indexes are sequential",
        [f"chunk:{i}" for i in range(len(recs))],
        [r.source.message_id for r in recs],
    )
    ctx.check(
        "every chunk points back to the document path",
        all(r.source.doc_path == str(doc) for r in recs),
    )
    ctx.check("every chunk carries the source version tag", all("srcver:1" in r.tags for r in recs))
    ctx.check(
        "chunk text covers the document (first chunk has the opening sentence)",
        "two beehives" in recs[0].content,
    )
    ctx.show("chunk lineage (source.parents)", [r.source.parents for r in recs])


async def search_chunks(ctx: Ctx) -> None:
    e = ctx.engine
    hits = await e.search("queen notes second hive", namespace=ctx.ns, top_k=3)
    ctx.show("search", [(r.memory_type, r.source.message_id, round(s, 3)) for r, s in hits])
    ctx.check(
        "a chunk is retrievable by its content",
        any(r.record_id in ctx.state["chunks"] for r, _ in hits),
    )
    ctx.check(
        "hit carries doc_path (citable)",
        any(r.source.doc_path == str(ctx.state["doc"]) for r, _ in hits),
    )


async def reingest_same(ctx: Ctx) -> None:
    e = ctx.engine
    again = await e.ingest_data(ctx.state["doc"], namespace=ctx.ns)
    ctx.expect("re-ingesting an unchanged document returns no new records", 0, len(again))
    live = [r for r in await e.list_records(ctx.ns, "resource") if r.status.value == "activated"]
    ctx.expect("record count unchanged", len(ctx.state["chunks"]), len(live))


async def reingest_changed(ctx: Ctx) -> None:
    e = ctx.engine
    ctx.state["doc"].write_text(_doc_text("Swarm notes for the third hive."), encoding="utf-8")
    new = await e.ingest_data(ctx.state["doc"], namespace=ctx.ns)
    ctx.show("new records", [(r.source.message_id, r.tags) for r in new])
    ctx.check("a changed chunk produces a new record", len(new) >= 1)
    ctx.check(
        "the new record carries srcver:2",
        all("srcver:2" in r.tags for r in new),
        [r.tags for r in new],
    )
    old_tail = await e.get_record(ctx.state["chunks"][-1], namespace=ctx.ns)
    ctx.expect(
        "the replaced chunk is retired (archived), not deleted", "archived", old_tail.status.value
    )
    hits = await e.search("queen notes second hive", namespace=ctx.ns, top_k=5)
    ctx.check(
        "the retired chunk is no longer served",
        all(r.record_id != ctx.state["chunks"][-1] for r, _ in hits),
    )
    ctx.expect(
        "the unchanged first chunk stays live",
        "activated",
        (await e.get_record(ctx.state["chunks"][0], namespace=ctx.ns)).status.value,
    )


async def ingest_empty_and_missing(ctx: Ctx) -> None:
    e = ctx.engine
    empty = ctx.state["dir"] / "empty.txt"
    empty.write_text("   \n\n  ", encoding="utf-8")
    ctx.expect("an empty document yields no records", [], await e.ingest_data(empty, namespace=ctx.ns))
    try:
        await e.ingest_data(ctx.state["dir"] / "absent.txt", namespace=ctx.ns)
        raised = False
    except Exception as exc:
        raised = True
        ctx.show("missing file", f"{type(exc).__name__}: {exc}")
    ctx.check("a missing file raises", raised)


async def asset(ctx: Ctx) -> None:
    e = ctx.engine
    img = ctx.state["dir"] / "hive.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
    a = await e.ingest_asset(img, namespace=ctx.ns, alt_text="two white beehives")
    ctx.state["asset"] = a.record_id
    ctx.show("asset record", {"content": a.content, "tags": a.tags, "doc_path": a.source.doc_path})
    ctx.check(
        "an asset record is stored (reference, not bytes)", a is not None and "asset_ref" in a.tags
    )
    ctx.check("the content hash is recorded", any(t.startswith("asset_hash:") for t in a.tags))
    ctx.check("mime is recorded", "mime:image/png" in a.tags)
    ctx.check("alt text is recorded", "alt:two white beehives" in a.tags)
    ctx.check("the file bytes are not stored in the record", "PNG" not in a.content)
    again = await e.ingest_asset(img, namespace=ctx.ns)
    ctx.expect("the same asset (same hash) is stored once", None, again)


async def asset_description(ctx: Ctx) -> None:
    e = ctx.engine
    kids = [
        r
        for r in await e.list_records(ctx.ns, "resource")
        if ctx.state["asset"] in r.source.parents
    ]
    ctx.show("description records", [(r.tags, r.content) for r in kids])
    ctx.expect("one derived description record", 1, len(kids))
    d = kids[0]
    ctx.check("its parent is the asset record (lineage)", d.source.parents == [ctx.state["asset"]])
    ctx.check("it is tagged as a description", "asset_desc" in d.tags)
    ctx.check(
        "metadata fallback describer used (no model): mentions mime and alt text",
        "image/png" in d.content and "two white beehives" in d.content,
    )
    hits = await e.search("white beehives", namespace=ctx.ns, top_k=3)
    ctx.check(
        "the asset is findable through its description",
        any(r.record_id in (d.record_id, ctx.state["asset"]) for r, _ in hits),
    )


async def asset_refused_when_off(ctx: Ctx) -> None:
    from flows._common import make_engine

    off = await make_engine({"memories": {"resource": {"enabled": True}}})
    try:
        img = ctx.state["dir"] / "hive.png"
        try:
            await off.ingest_asset(img, namespace=ctx.ns)
            raised = False
        except Exception as exc:
            raised = True
            ctx.show("asset_describer off", f"{type(exc).__name__}: {exc}")
        ctx.check("ingest_asset is refused while write.asset_describer is off", raised)
    finally:
        await off.stop()


STEPS = [
    Step(
        "F15.1",
        "ingest a text document: chunks become resource records",
        ingest_doc,
        "api/handle.py:ingest -> ingest/gateway.py:ingest, memories/resource/extraction.py",
    ),
    Step("F15.2", "chunks are searchable and citable", search_chunks, "api/engine.py:search"),
    Step(
        "F15.3",
        "re-ingest unchanged: no new records (idempotent)",
        reingest_same,
        "ingest/gateway.py (source versioning)",
    ),
    Step(
        "F15.4",
        "re-ingest changed: new version, replaced chunk retired",
        reingest_changed,
        "ingest/gateway.py, ingest versioner",
    ),
    Step(
        "F15.5",
        "empty document and missing file",
        ingest_empty_and_missing,
        "memories/resource/extraction.py",
    ),
    Step(
        "F15.6",
        "ingest_asset: reference record (hash, mime, alt), stored once",
        asset,
        "ingest/gateway.py:ingest_asset, _apply_asset",
    ),
    Step(
        "F15.7",
        "asset description record with parent = asset",
        asset_description,
        "ingest/asset_describer.py:describe_and_deposit",
    ),
    Step(
        "F15.8",
        "ingest_asset refused while the describer is off",
        asset_refused_when_off,
        "ingest/gateway.py:ingest_asset",
    ),
]
