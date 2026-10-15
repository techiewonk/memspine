"""E04 image pre-compute: download each attachment once, then describe it, ahead of the eval.

    python evals/precompute_assets.py --dataset locomo --data-path data/locomo10.json \
        --asset-dir evals/runs/_asset_cache --phase download
    python evals/precompute_assets.py ... --phase describe     # later, when the GPU is free
    python evals/precompute_assets.py ... --phase all

Generic over any dataset adapter whose turns carry ``meta["attachments"]`` (what
``MemspineSystem._message`` forwards when ``ingest.assets: on``). Asset ids are the engine's
(``asset_id_for(turn_id, uri)``), so an eval run with ``ingest.asset_dir`` pointing here and
``read.asset_evidence: cached`` finds the evidence and makes no network or vision call.

Phase ``download`` fetches only each turn's own URI (size/type checks, no off-site redirect),
caches the bytes by content hash and records failures explicitly (``availability: unavailable``
with the reason). Phase ``describe`` runs the vision backend (temperature 0, a fixed prompt) over
the cached bytes only. Both are idempotent and resumable: finished work is skipped; a failed
download stays failed unless ``--retry-failed``; a failed vision call is retried.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from memspine.services.assets import (  # noqa: E402
    AssetAdapter,
    AssetEntry,
    AssetRegistry,
    HttpAssetFetcher,
    NoopVision,
    OllamaVision,
    asset_id_for,
    parse_attachments,
)

DEFAULT_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp")


def iter_assets(items: Iterable[Any]) -> Iterator[AssetEntry]:
    """Every attachment of every turn of every item, with the engine's asset id."""
    for item in items:
        for turn in item.history:
            meta = getattr(turn, "meta", None) or {}
            for ref in parse_attachments(meta.get("attachments")):
                yield AssetEntry(
                    asset_id=asset_id_for(str(turn.turn_id), ref.uri),
                    source_turn_id=str(turn.turn_id),
                    uri=ref.uri,
                    kind=ref.kind,
                    caption=ref.caption,
                    search_hint=ref.search_hint,
                )


async def precompute(
    entries: Iterable[AssetEntry],
    adapter: AssetAdapter,
    *,
    phase: str = "all",
    concurrency: int = 4,
    retry_failed: bool = False,
    limit: int | None = None,
    log: Any = None,
) -> dict[str, Any]:
    """Register, then download and/or describe. Returns the counts and timing."""
    started = time.monotonic()
    ids: list[str] = []
    for entry in entries:
        if limit is not None and len(ids) >= limit:
            break
        adapter.registry.register(entry)
        ids.append(entry.asset_id)
    if retry_failed:
        for aid in ids:
            e = adapter.registry.get(aid)
            if e is not None and e.availability == "unavailable":
                e.availability, e.error = "unfetched", None
                adapter.registry.update(e)
    timing: dict[str, float] = {}
    if phase in ("download", "all"):
        t0 = time.monotonic()
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(aid: str) -> None:
            async with sem:
                await adapter.download(aid)

        await asyncio.gather(*(one(a) for a in ids))
        timing["download_s"] = round(time.monotonic() - t0, 2)
    if phase in ("describe", "all"):
        t0 = time.monotonic()
        for n, aid in enumerate(ids, 1):  # sequential: one vision call at a time
            await adapter.describe(aid)
            if log and n % 25 == 0:
                log(f"described {n}/{len(ids)}")
        timing["describe_s"] = round(time.monotonic() - t0, 2)
    rows = [adapter.registry.get(a) for a in ids]
    done = [r for r in rows if r is not None]
    hashes = {r.content_hash for r in done if r.content_hash}
    reasons: dict[str, int] = {}
    for r in done:
        if r.availability == "unavailable":
            key = (r.error or "unknown").split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "phase": phase,
        "images": len(done),
        "unique_uris": len({r.uri for r in done}),
        "unique_content": len(hashes),
        "downloaded": sum(1 for r in done if r.availability == "cached"),
        "failed": sum(1 for r in done if r.availability == "unavailable"),
        "failure_reasons": reasons,
        "unfetched": sum(1 for r in done if r.availability == "unfetched"),
        "described": sum(1 for r in done if r.evidence_status == "ok"),
        "described_failed": sum(1 for r in done if r.evidence_status == "failed"),
        "described_pending": sum(
            1 for r in done if r.availability == "cached" and r.evidence_status == "none"
        ),
        "timing_s": {**timing, "total_s": round(time.monotonic() - started, 2)},
    }


def build_adapter(args: argparse.Namespace) -> AssetAdapter:
    asset_dir = Path(args.asset_dir)
    vision: Any = (
        NoopVision()
        if args.phase == "download"
        else OllamaVision(args.vision_model, args.vision_url, timeout=args.vision_timeout)
    )
    return AssetAdapter(
        AssetRegistry(asset_dir),
        HttpAssetFetcher(DEFAULT_MIME),
        vision,
        asset_dir,
        max_bytes=args.max_bytes,
        timeout=args.timeout,
    )


def load_items(args: argparse.Namespace) -> Iterable[Any]:
    from memspine_evals.datasets import LoCoMoDataset

    if args.dataset != "locomo":
        raise SystemExit(f"dataset {args.dataset!r} not wired yet (add a loader here)")
    return LoCoMoDataset(args.data_path, revision_id="auto").items()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset", default="locomo")
    ap.add_argument("--data-path", required=True)
    ap.add_argument("--asset-dir", default=str(HERE / "runs" / "_asset_cache"))
    ap.add_argument("--phase", choices=("download", "describe", "all"), default="all")
    ap.add_argument("--vision-model", default="qwen2.5vl:3b")
    ap.add_argument("--vision-url", default="http://127.0.0.1:11434")
    ap.add_argument("--vision-timeout", type=float, default=120.0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-bytes", type=int, default=5_000_000)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="first N attachments only")
    args = ap.parse_args(argv)
    adapter = build_adapter(args)
    report = asyncio.run(
        precompute(
            iter_assets(load_items(args)),
            adapter,
            phase=args.phase,
            concurrency=args.concurrency,
            retry_failed=args.retry_failed,
            limit=args.limit,
            log=lambda m: print(m, file=sys.stderr),
        )
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())