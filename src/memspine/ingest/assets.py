"""Asset adapter and attachment registration for the write path (F-09).

RM-113 item 18: the registry is a table of the records database (the storage's async engine),
the downloaded bytes go to ``storage.documents``. A records store without SQL keeps the rows in
process memory (logged once)."""

from __future__ import annotations

from collections.abc import (
    Mapping,
    Sequence,
)
from typing import Any

import structlog

from memspine.observability import tap

#: ``model.vision.settings`` defaults (E04): the local Ollama vision model and endpoint.
DEFAULT_VISION_MODEL = "qwen2.5vl:3b"
DEFAULT_VISION_URL = "http://localhost:11434"

_log = structlog.get_logger("memspine.assets")


def _asset_registry(eng: Any) -> Any:
    """The Engine's asset registry: SQL tables of the records database when the records store
    is SQL, else an in-process registry. An injected adapter (``set_asset_adapter``) brings
    its own."""
    if eng._assets is not None:
        return eng._assets.registry
    if eng._asset_rows is None:
        from memspine.services.assets import MemoryAssetRegistry, SqlAssetRegistry

        engine = getattr(eng._sql, "engine", None)
        if engine is not None:
            eng._asset_rows = SqlAssetRegistry(engine)
        else:
            if eng._config().ingest.assets == "on":
                _log.warning(
                    "asset.registry_in_memory",
                    detail="the records store has no SQL database: asset rows live in this process",
                )
            eng._asset_rows = MemoryAssetRegistry()
    return eng._asset_rows


def _asset_adapter(eng: Any) -> Any:
    if eng._assets is None:
        from memspine.services.assets import AssetAdapter, HttpAssetFetcher
        from memspine.services.vision import NoopVision, OllamaVision

        config = eng._config()
        cfg = config.ingest
        http = eng._http_connector()  # RM-113: the Engine's shared HTTP connector
        slot = config.model.vision  # ADR-082: null = no description (text only)
        vision: Any = NoopVision()
        if slot is not None:  # the only provider today is ``ollama``
            options = slot.settings
            vision = OllamaVision(
                str(options.get("model", DEFAULT_VISION_MODEL)),
                http,
                str(options.get("api_base", DEFAULT_VISION_URL)),
                timeout=slot.timeout_s,
            )
        eng._assets = AssetAdapter(
            _asset_registry(eng),
            HttpAssetFetcher(tuple(cfg.asset_allowed_mime), http),
            vision,
            eng._documents,  # RM-113 item 18: the bytes go to storage.documents (or nowhere)
            max_bytes=cfg.asset_max_bytes,
            timeout=cfg.asset_timeout_s,
        )
    return eng._assets


def _attachment_refs(
    eng: Any, turn: Mapping[str, Any], index: int, session_id: str | None
) -> list[tuple[Any, str]]:
    """``ingest.assets``: the turn's attachments with their asset ids (empty when off)."""
    if eng._config().ingest.assets != "on" or not isinstance(turn, Mapping):
        return []
    from memspine.services.assets import asset_id_for, parse_attachments

    source = str(turn.get("turn_id") or f"{session_id or 'nosession'}:{index}")
    refs = parse_attachments(turn.get("attachments"))
    if refs:
        tap.event("write.assets.attached", n=len(refs), turn=index)
    return [(ref, asset_id_for(source, ref.uri)) for ref in refs]


async def _register_assets(
    eng: Any,
    refs: list[tuple[Any, str]],
    turn: Mapping[str, Any],
    index: int,
    session_id: str | None,
    namespace: str,
    record_id: str,
) -> None:
    from memspine.services.assets import AssetEntry

    source = str(turn.get("turn_id") or f"{session_id or 'nosession'}:{index}")
    registry = eng._asset_adapter().registry
    tap.event("write.assets.registered", n=len(refs), id=record_id)
    for ref, aid in refs:
        await registry.register(
            AssetEntry(
                asset_id=aid,
                source_turn_id=source,
                uri=ref.uri,
                namespace=namespace,
                kind=ref.kind,
                caption=ref.caption,
                search_hint=ref.search_hint,
                record_id=record_id,
            )
        )


async def _erase_assets(eng: Any, namespace: str, record_ids: Sequence[str] | None) -> int:
    """Erasure hook (RM-113 item 18): drop the asset rows of ``record_ids`` (every row of the
    namespace when None) and the stored bytes no remaining row of the namespace references.
    Returns the number of rows removed. Runs even with ``ingest.assets`` off: rows written by
    an earlier configuration go too."""
    registry = _asset_registry(eng)
    if record_ids is None:
        gone = await registry.erase_namespace(namespace)
    else:
        gone = await registry.erase_records(namespace, record_ids)
    documents = eng._documents
    if documents is not None and record_ids is not None:
        for digest in sorted({e.content_hash for e in gone if e.content_hash}):
            if await registry.hash_in_use(namespace, digest):
                continue
            head = await documents.head(namespace, digest)
            if head is not None and not head.record_ids:  # another record still references it
                await documents.delete(namespace, digest)
    if gone:
        tap.event("write.assets.erased", n=len(gone))
    return len(gone)
