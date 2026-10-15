"""Asset adapter and attachment registration for the write path (F-09)."""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from typing import Any

from memspine.observability import tap


def _asset_adapter(eng: Any) -> Any:
    if eng._assets is None:
        from memspine.services.assets import (
            AssetAdapter,
            AssetRegistry,
            HttpAssetFetcher,
            NoopVision,
            OllamaVision,
        )

        cfg = eng._config().ingest
        vision: Any = (
            OllamaVision(
                cfg.asset_vision_model,
                cfg.asset_vision_url,
                timeout=cfg.asset_vision_timeout_s,
            )
            if cfg.asset_vision == "ollama"
            else NoopVision()
        )
        eng._assets = AssetAdapter(
            AssetRegistry(cfg.asset_dir),
            HttpAssetFetcher(tuple(cfg.asset_allowed_mime)),
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
