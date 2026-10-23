"""Asset adapter and attachment registration for the write path (F-09)."""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from typing import Any

from memspine.observability import tap

#: ``model.vision.settings`` defaults (E04): the local Ollama vision model and endpoint.
DEFAULT_VISION_MODEL = "qwen2.5vl:3b"
DEFAULT_VISION_URL = "http://localhost:11434"


def _asset_adapter(eng: Any) -> Any:
    if eng._assets is None:
        from memspine.services.assets import AssetAdapter, AssetRegistry, HttpAssetFetcher
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
            AssetRegistry(cfg.asset_dir),
            HttpAssetFetcher(tuple(cfg.asset_allowed_mime), http),
            vision,
            cfg.asset_dir,
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


def _register_assets(
    eng: Any,
    refs: list[tuple[Any, str]],
    turn: Mapping[str, Any],
    index: int,
    session_id: str | None,
    record_id: str,
) -> None:
    from memspine.services.assets import AssetEntry

    source = str(turn.get("turn_id") or f"{session_id or 'nosession'}:{index}")
    adapter = eng._asset_adapter()
    tap.event("write.assets.registered", n=len(refs), id=record_id)
    for ref, aid in refs:
        adapter.registry.register(
            AssetEntry(
                asset_id=aid,
                source_turn_id=source,
                uri=ref.uri,
                kind=ref.kind,
                caption=ref.caption,
                search_hint=ref.search_hint,
                record_id=record_id,
            )
        )
