"""E04: attachment (image) evidence. Identity at ingest and a controlled fetch; the vision/OCR
port is :mod:`memspine.services.vision` (RM-118).

RM-113 item 18: the registry rows are tables of the records database
(:mod:`~memspine.services.assets.sql_registry`), the downloaded bytes go to the
``storage.documents`` slot; :mod:`~memspine.services.assets.legacy` imports a pre-item-18
``registry.sqlite`` + ``blobs/`` directory once. Nothing here runs unless ``ingest.assets`` /
``read.asset_evidence`` are on.
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
from memspine.services.assets.registry import (
    AssetImportResult,
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
    "AssetImportResult",
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
