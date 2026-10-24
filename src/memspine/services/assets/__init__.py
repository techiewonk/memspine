"""Asset storage ports and adapters for current records and documents."""

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
from memspine.services.assets.registry import (
    AssetRegistry,
    MemoryAssetRegistry,
)
from memspine.services.assets.sql_registry import SqlAssetRegistry

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
    "MemoryAssetRegistry",
    "SqlAssetRegistry",
    "asset_id_for",
    "needs_visual_detail",
    "parse_attachments",
    "sniff_mime",
]
