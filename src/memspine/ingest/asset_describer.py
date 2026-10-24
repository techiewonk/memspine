"""The asset describer (W2-11; W-23): one ``AssetDescriber`` port, two triggers, both off by default.

``write.asset_describer``:

* ``off`` (default): nothing here runs; an ``asset`` envelope is refused.
* ``ingest``: an asset envelope stores the asset REFERENCE (content hash + path/URI, never the
  bytes) as a ``resource`` record, then the describer writes a description as a derived record
  through ``derive/write.py:write_derived`` with the asset record as parent.
* ``on_read``: the asset record is stored the same way; a read that returns an asset record with no
  description yet describes it lazily, caches it (it is a record), and never waits longer than
  ``write.asset_describe_timeout_s``: past that the read returns without it and the work is queued.

The port has two implementations: ``LLMDescriber`` (the optional ``describe_asset`` LLM role) and
``MetadataDescriber`` (filename, mime, size, alt text, EXIF description; no model call), which is the
fallback whenever no model is bound. An ingest description wins: a read never re-describes an asset
that already has one, and no two descriptions share (asset hash, model, prompt hash).

Taps: ``write.asset.*``. Description text is model output, stored trust-capped (``DESCRIPTION_TRUST``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from memspine.derive.write import write_derived
from memspine.observability import tap
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo

__all__ = [
    "ASSET_TAG",
    "DESCRIPTION_CHANNEL",
    "DESCRIPTION_TRUST",
    "AssetDescriber",
    "AssetInfo",
    "Description",
    "LLMDescriber",
    "MetadataDescriber",
    "asset_info_from_file",
    "asset_info_of",
    "describe_and_deposit",
    "on_read",
    "resolve_describer",
    "shutdown",
]

ASSET_TAG = "asset_ref"
HASH_PREFIX = "asset_hash:"
MIME_PREFIX = "mime:"
SIZE_PREFIX = "size:"
ALT_PREFIX = "alt:"
DESC_TAG = "asset_desc"
KEY_PREFIX = "desc_key:"
DESCRIPTION_CHANNEL = "asset_description"
#: A model caption is derived, unverified text: stored at most this trusted (W-07).
DESCRIPTION_TRUST = 0.6
#: Largest local file inlined into a vision call.
INLINE_MAX_BYTES = 5_000_000
METADATA_KEY = "metadata"
DESCRIBE_ROLE = "describe_asset"


@dataclass(frozen=True, slots=True)
class AssetInfo:
    """What the describer sees: the reference and cheap metadata, never the bytes."""

    hash: str
    uri: str
    mime: str = "application/octet-stream"
    size: int | None = None
    alt_text: str | None = None
    meta: Mapping[str, str] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return Path(self.uri.split("?", 1)[0]).name or self.uri


@dataclass(frozen=True, slots=True)
class Description:
    text: str
    model: str  # "metadata" when no model ran
    prompt_hash: str

    @property
    def key(self) -> str:
        return METADATA_KEY if self.model == "metadata" else f"{self.model}:{self.prompt_hash}"


class AssetDescriber(Protocol):
    """The port (``intake.AssetDescriber``). ``key`` names the (model, prompt) pair for dedupe."""

    @property
    def key(self) -> str: ...

    async def describe(self, info: AssetInfo) -> Description: ...


# ------------------------------------------------------------------------------------- the asset
def _exif_description(path: Path) -> dict[str, str]:
    """Best-effort EXIF text fields (Pillow when installed); never raises."""
    try:
        from PIL import Image  # type: ignore[import-not-found,unused-ignore]

        with Image.open(path) as im:
            exif = im.getexif()
            out: dict[str, str] = {}
            for tag, name in ((0x010E, "image_description"), (0x0131, "software")):
                value = exif.get(tag)
                if isinstance(value, str) and value.strip():
                    out[name] = value.strip()[:200]
            return out
    except Exception:
        return {}


def asset_info_from_file(
    path_or_uri: str | Path,
    *,
    mime: str | None = None,
    size: int | None = None,
    content_hash: str | None = None,
    alt_text: str | None = None,
) -> AssetInfo:
    """Build the info for a path or URI. A local file is hashed (streamed) and sized; a remote URI
    needs the caller's ``content_hash`` or gets a hash of the URI itself."""
    uri = str(path_or_uri)
    local = Path(uri)
    is_file = "://" not in uri and local.is_file()
    if is_file:
        if size is None:
            size = local.stat().st_size
        if content_hash is None:
            digest = hashlib.sha256()
            with local.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            content_hash = digest.hexdigest()
    if content_hash is None:
        content_hash = hashlib.sha256(uri.encode()).hexdigest()
    guessed = mimetypes.guess_type(uri.split("?", 1)[0])[0]
    meta = (
        _exif_description(local) if is_file and (mime or guessed or "").startswith("image/") else {}
    )
    return AssetInfo(
        hash=content_hash,
        uri=uri,
        mime=mime or guessed or "application/octet-stream",
        size=size,
        alt_text=alt_text,
        meta=meta,
    )


def asset_info_of(record: MemoryRecord) -> AssetInfo | None:
    """Read the info back from an asset record's tags; None when it is not an asset record."""
    if ASSET_TAG not in record.tags:
        return None
    hash_ = mime = alt = None
    size: int | None = None
    for tag in record.tags:
        if tag.startswith(HASH_PREFIX):
            hash_ = tag[len(HASH_PREFIX) :]
        elif tag.startswith(MIME_PREFIX):
            mime = tag[len(MIME_PREFIX) :]
        elif tag.startswith(SIZE_PREFIX) and tag[len(SIZE_PREFIX) :].isdigit():
            size = int(tag[len(SIZE_PREFIX) :])
        elif tag.startswith(ALT_PREFIX):
            alt = tag[len(ALT_PREFIX) :]
    uri = record.source.doc_path
    if not hash_ or not uri:
        return None
    meta: dict[str, str] = {}
    local = Path(uri)
    if "://" not in uri and (mime or "").startswith("image/") and local.is_file():
        meta = _exif_description(local)
    return AssetInfo(hash_, uri, mime or "application/octet-stream", size, alt, meta)


def asset_tags(info: AssetInfo) -> list[str]:
    tags = [
        ASSET_TAG,
        f"{HASH_PREFIX}{info.hash}",
        f"{MIME_PREFIX}{info.mime}",
    ]
    if info.size is not None:
        tags.append(f"{SIZE_PREFIX}{info.size}")
    if info.alt_text:
        tags.append(f"{ALT_PREFIX}{info.alt_text[:200]}")
    return tags


def reference_text(info: AssetInfo) -> str:
    """The asset record's own content: a searchable reference line, no bytes."""
    return f"asset {info.filename} ({info.mime})"


# --------------------------------------------------------------------------------- the describers
class MetadataDescriber:
    """No model: filename, mime, size, alt text and EXIF description."""

    key = METADATA_KEY

    async def describe(self, info: AssetInfo) -> Description:
        parts = [f"File {info.filename}, type {info.mime}"]
        if info.size is not None:
            parts[0] += f", {info.size} bytes"
        if info.alt_text:
            parts.append(f"alt text: {info.alt_text}")
        for name, value in sorted(info.meta.items()):
            parts.append(f"{name.replace('_', ' ')}: {value}")
        return Description(". ".join(parts) + ".", "metadata", "")


class LLMDescriber:
    """The ``describe_asset`` role. ``llm`` is the role's provider (``chat(messages)``); ``prompt`` its
    registry prompt (``render(ctx) -> messages``). A vision-capable model sees the image as an
    ``image_url`` part when it is a remote URL or a local file within ``INLINE_MAX_BYTES``."""

    def __init__(self, llm: Any, prompt: Any, model: str) -> None:
        self._llm = llm
        self._prompt = prompt
        self._model = model or str(getattr(llm, "provider_id", "llm"))
        self._phash = hashlib.sha256(
            str(getattr(prompt, "prompt_version", "") or getattr(prompt, "id", "")).encode()
        ).hexdigest()[:12]

    @property
    def key(self) -> str:
        return f"{self._model}:{self._phash}"

    async def describe(self, info: AssetInfo) -> Description:
        messages = self._prompt.render(
            {
                "filename": info.filename,
                "mime": info.mime,
                "size": info.size if info.size is not None else "unknown",
                "alt_text": info.alt_text or "",
            }
        )
        image = await asyncio.to_thread(_image_part, info)
        if image is not None and messages:
            last = dict(messages[-1])
            messages = [
                *messages[:-1],
                {**last, "content": [{"type": "text", "text": last["content"]}, image]},
            ]
        text = str(await self._llm.chat(messages)).strip()
        if not text:  # an empty reply is no description; fall back to metadata
            return await MetadataDescriber().describe(info)
        return Description(text, self._model, self._phash)


def _image_part(info: AssetInfo) -> dict[str, Any] | None:
    if not info.mime.startswith("image/"):
        return None
    if info.uri.startswith(("http://", "https://")):
        return {"type": "image_url", "image_url": {"url": info.uri}}
    path = Path(info.uri)
    try:
        if "://" in info.uri or not path.is_file() or path.stat().st_size > INLINE_MAX_BYTES:
            return None
        data = base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return None
    return {"type": "image_url", "image_url": {"url": f"data:{info.mime};base64,{data}"}}


def resolve_describer(eng: Any) -> AssetDescriber:
    """An injected ``eng._asset_describer`` (tests, hosts), else the ``describe_asset`` role when one
    is bound, else the metadata-only fallback (no model call)."""
    injected = getattr(eng, "_asset_describer", None)
    if injected is not None:
        return injected  # type: ignore[no-any-return]
    llm = getattr(eng, "_llm", None)
    prompts = getattr(eng, "_prompts", None)
    if llm is not None and prompts is not None and DESCRIBE_ROLE in llm.roles:
        role = eng._config().model.llm.get(DESCRIBE_ROLE)
        return LLMDescriber(
            llm.for_role(DESCRIBE_ROLE),
            prompts.for_role(DESCRIBE_ROLE),
            str(role.settings.get("model", "")) if role is not None else "",
        )
    return MetadataDescriber()


# ---------------------------------------------------------------------------------- the deposit
async def _descriptions_of(eng: Any, ns: str, asset_id: str) -> list[MemoryRecord]:
    storage = eng._require_started()
    return [
        r
        for r in await storage.list_children(ns, [asset_id])
        if DESC_TAG in r.tags and r.source.channel == DESCRIPTION_CHANNEL
    ]


def _key_of(record: MemoryRecord) -> str | None:
    for tag in record.tags:
        if tag.startswith(KEY_PREFIX):
            return tag[len(KEY_PREFIX) :]
    return None


async def describe_and_deposit(
    eng: Any, ns: str, asset: MemoryRecord, *, trigger: str
) -> MemoryRecord | None:
    """Describe ``asset`` and write the description as a derived record (parent: the asset record).
    Returns the new record, or None when the asset already has a description for the same key (the
    cache) or is gone. The model call runs outside the namespace lock; the dedupe check and write
    run inside it."""
    info = asset_info_of(asset)
    if info is None:
        return None
    live = await eng._require_started().get_record(asset.record_id)
    if live is None or live.status is not RecordStatus.ACTIVATED:  # erased/archived since the read
        tap.event("write.asset.skipped", asset=asset.record_id, reason="parent_gone")
        return None
    describer = resolve_describer(eng)
    existing = await _descriptions_of(eng, ns, asset.record_id)
    if trigger == "on_read" and existing:  # an ingest caption wins; a read never re-describes
        tap.event("write.asset.cached", asset=asset.record_id, trigger=trigger)
        return None
    if any(_key_of(r) == describer.key for r in existing):
        tap.event("write.asset.cached", asset=asset.record_id, trigger=trigger, key=describer.key)
        return None
    tap.event(
        "write.asset.describe",
        asset=asset.record_id,
        trigger=trigger,
        describer=type(describer).__name__,
        key=describer.key,
    )
    desc = await describer.describe(info)
    storage = eng._require_started()
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        fresh = await storage.get_record(asset.record_id)
        if (
            fresh is None or fresh.status is not RecordStatus.ACTIVATED
        ):  # erased or archived while we ran
            tap.event("write.asset.skipped", asset=asset.record_id, reason="parent_gone")
            return None
        if any(_key_of(r) == desc.key for r in await _descriptions_of(eng, ns, asset.record_id)):
            tap.event("write.asset.cached", asset=asset.record_id, trigger=trigger, key=desc.key)
            return None
        record = MemoryRecord(
            namespace=ns,
            memory_type="resource",
            content=desc.text,
            source=SourceInfo(
                role="assistant",
                channel=DESCRIPTION_CHANNEL,
                doc_path=info.uri,
                parents=[asset.record_id],
                prompt_version=desc.prompt_hash or None,
            ),
            tags=[DESC_TAG, f"{HASH_PREFIX}{info.hash}", f"{KEY_PREFIX}{desc.key}"],
        )
        cap = [asset.trust] if desc.model == "metadata" else [asset.trust, DESCRIPTION_TRUST]
        written = await write_derived(
            eng,
            storage,
            ns,
            record,
            "resource",
            "system",
            parents=[asset.record_id],
            roles={asset.record_id: "support"},
            trust_cap=cap,
            site="asset_describe",
        )
    tap.event(
        "write.asset.described",
        asset=asset.record_id,
        record=written.record_id,
        trigger=trigger,
        model=desc.model,
        chars=len(desc.text),
    )
    return written


# ------------------------------------------------------------------------------------- on read
def _tasks(eng: Any) -> dict[str, asyncio.Task[Any]]:
    tasks: dict[str, asyncio.Task[Any]] = getattr(eng, "_asset_tasks", None) or {}
    eng._asset_tasks = tasks
    return tasks


async def on_read(
    eng: Any, ns: str, hits: Sequence[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """``write.asset_describer == "on_read"``: for each returned asset record with no description,
    describe it now, waiting at most ``write.asset_describe_timeout_s`` for all of them together.
    Work not done by then keeps running in the background (queued; one task per asset) and the read
    returns without it. A description made in time is placed right after its asset in the result."""
    out = list(hits)
    pending: dict[str, MemoryRecord] = {}
    for rec, _score in hits:
        if (
            ASSET_TAG in rec.tags
            and rec.namespace == ns
            and rec.record_id not in pending
            and not await _descriptions_of(eng, ns, rec.record_id)
        ):
            pending[rec.record_id] = rec
    if not pending:
        return out
    tasks = _tasks(eng)
    for rid, rec in pending.items():
        if rid not in tasks:
            task = asyncio.ensure_future(describe_and_deposit(eng, ns, rec, trigger="on_read"))
            tasks[rid] = task
            task.add_done_callback(lambda _t, rid=rid: tasks.pop(rid, None))  # type: ignore[misc]
    timeout = float(eng._config().write.asset_describe_timeout_s)
    mine = {rid: tasks[rid] for rid in pending if rid in tasks}
    waiting = list(mine.values())
    done, late = await asyncio.wait(waiting, timeout=timeout) if waiting else (set(), set())
    if late:
        tap.event("write.asset.queued", assets=len(late), timeout_s=timeout)
    made: dict[str, MemoryRecord] = {}
    for rid, task in mine.items():
        if task in done and not task.cancelled():
            if task.exception() is not None:
                tap.event("write.asset.failed", asset=rid, error=type(task.exception()).__name__)
            elif (res := task.result()) is not None:
                made[rid] = res
    if not made:
        return out
    placed: list[tuple[MemoryRecord, float]] = []
    seen = {r.record_id for r, _ in out}
    for rec, score in out:
        placed.append((rec, score))
        got = made.get(rec.record_id)
        if got is not None and got.record_id not in seen:
            seen.add(got.record_id)
            placed.append((got, score))
    return placed


def _records_in(result: Any) -> list[MemoryRecord]:
    """The records a read verb returns, whatever its shape: a record, a list of records or
    ``(record, score)`` pairs, an assembled context (``.records``) or a read result (``.context``)."""
    if isinstance(result, MemoryRecord):
        return [result]
    if isinstance(result, list):
        return [r for item in result if isinstance(r := _pair_record(item), MemoryRecord)]
    inner = getattr(result, "context", result)
    records = getattr(inner, "records", None)
    if isinstance(records, list):
        return [r for r in records if isinstance(r, MemoryRecord)]
    return []


def _pair_record(item: Any) -> Any:
    return item[0] if isinstance(item, tuple) and item else item


async def after_read(eng: Any, ns: str, result: Any) -> Any:
    """FU-ASSET: ``on_read`` for any read verb's output (called once per outermost verb, after the
    viewer filter, so only assets the caller can see are described). ``(record, score)`` lists get
    a made description placed right after its asset, as :func:`on_read` does; other shapes (a
    record list, an assembled context, a single record) are returned unchanged, the description
    being written for the next read."""
    records = _records_in(result)
    if not any(ASSET_TAG in r.tags for r in records):
        return result
    if isinstance(result, list) and result and all(isinstance(i, tuple) for i in result):
        return await on_read(eng, ns, result)
    await on_read(eng, ns, [(r, 0.0) for r in records])
    return result


async def shutdown(eng: Any) -> None:
    """Cancel queued description work (engine stop)."""
    tasks = list(getattr(eng, "_asset_tasks", {}).values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
