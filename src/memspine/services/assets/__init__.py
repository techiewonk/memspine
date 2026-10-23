"""E04: attachment (image) evidence. Identity at ingest and a controlled fetch; the vision/OCR
port is :mod:`memspine.services.vision` (RM-118).

Stdlib only. Nothing here runs unless ``ingest.assets`` / ``read.asset_evidence`` are on.
"""

from memspine.services.assets.adapter import AssetAdapter, AssetStats
from memspine.services.assets.cue import needs_visual_detail
from memspine.services.assets.fetch import (
    AssetFetcher,
    AssetFetchError,
    FetchedAsset,
    HttpAssetFetcher,
    sniff_mime,
)
from memspine.services.assets.models import (
    ASSET_TAG_PREFIX,
    AssetEntry,
    AssetRef,
    asset_id_for,
    parse_attachments,
)
from memspine.services.assets.registry import AssetRegistry

__all__ = [
    "ASSET_TAG_PREFIX",
    "AssetAdapter",
    "AssetEntry",
    "AssetFetchError",
    "AssetFetcher",
    "AssetRef",
    "AssetRegistry",
    "AssetStats",
    "FetchedAsset",
    "HttpAssetFetcher",
    "asset_id_for",
    "needs_visual_detail",
    "parse_attachments",
    "sniff_mime",
]
