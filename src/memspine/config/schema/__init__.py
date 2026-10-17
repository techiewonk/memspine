"""Config tree (D-11). Constructed by the loader from merged layers.

``namespaces.<ns>.memories`` is RESERVED for v0.2 per-namespace type enablement
(D-14): the key is rejected today so configs written now stay forward-compatible.

F-07: one module per area (``storage``, ``llm``, ``read``, ``write``, ``firewall``,
``integrity``, ``privacy``, ``observability``, ``rest``, ``root``). This package is the real
``memspine.config.schema`` path; every config class is exported here.
"""

from __future__ import annotations

from memspine.config.schema.economy import BudgetConfig, EconomyConfig
from memspine.config.schema.firewall import FirewallConfig, FirewallSignalsConfig
from memspine.config.schema.integrity import IntegrityConfig
from memspine.config.schema.llm import (
    DecisionConfig,
    EmbeddingConfig,
    GpuDecl,
    LLMConfig,
    LLMRoleConfig,
    ModelDecl,
    ModelsConfig,
    PromptsConfig,
    StructuredConfig,
    WorkersConfig,
)
from memspine.config.schema.observability import ObservabilityConfig
from memspine.config.schema.offline import OfflineConfig
from memspine.config.schema.privacy import (
    AuditConfig,
    ConsentConfig,
    PrivacyConfig,
    RetentionClassConfig,
    RetentionConfig,
)
from memspine.config.schema.providers import (
    DiskCacheSettings,
    FastembedEmbeddingSettings,
    FastembedRerankerSettings,
    FlashrankRerankerSettings,
    HashEmbeddingSettings,
    JinaRerankerSettings,
    LadybugSettings,
    LanceDBSettings,
    LiteLLMEmbeddingSettings,
    LiteLLMRerankerSettings,
    MemoryCacheSettings,
    Neo4jSettings,
    PgvectorSettings,
    PostgresSettings,
    ProviderSettings,
    ProviderSettingsMap,
    Qwen3RerankerSettings,
    RemoteCacheSettings,
    SentenceTransformersEmbeddingSettings,
    SqliteAdjacencySettings,
    SqliteEncryptionSettings,
    SqliteSettings,
    StaticEmbeddingSettings,
    TantivySettings,
)
from memspine.config.schema.read import ReadConfig
from memspine.config.schema.remote_providers import OpenSearchSettings, WeaviateSettings
from memspine.config.schema.rest import (
    RestApiKeyConfig,
    RestAuthConfig,
    RestConfig,
    RestJwtConfig,
    RestRateLimitConfig,
)
from memspine.config.schema.root import MemoryTypeConfig, MemspineConfig, NamespaceConfig
from memspine.config.schema.runtime import (
    CacheSwitchConfig,
    EmbeddingSwitchConfig,
    ModelSwitchConfig,
    ProjectionSwitchConfig,
    ProviderServicesConfig,
    RuntimeConfig,
    ServiceSwitchesConfig,
    StorageSwitchConfig,
)
from memspine.config.schema.storage import (
    CacheConfig,
    EncryptionConfig,
    EventLogConfig,
    GraphConfig,
    GraphMirrorConfig,
    StorageConfig,
    VectorConfig,
)
from memspine.config.schema.write import DataShapeConfig, IngestConfig, WriteConfig
from memspine.providers.settings import LiteLLMRoleSettings, LlamaCppRoleSettings

__all__ = [
    "AuditConfig",
    "BudgetConfig",
    "CacheConfig",
    "CacheSwitchConfig",
    "ConsentConfig",
    "DataShapeConfig",
    "DecisionConfig",
    "DiskCacheSettings",
    "EconomyConfig",
    "EmbeddingConfig",
    "EmbeddingSwitchConfig",
    "EncryptionConfig",
    "EventLogConfig",
    "FastembedEmbeddingSettings",
    "FastembedRerankerSettings",
    "FirewallConfig",
    "FirewallSignalsConfig",
    "FlashrankRerankerSettings",
    "GpuDecl",
    "GraphConfig",
    "GraphMirrorConfig",
    "HashEmbeddingSettings",
    "IngestConfig",
    "IntegrityConfig",
    "JinaRerankerSettings",
    "LLMConfig",
    "LLMRoleConfig",
    "LadybugSettings",
    "LanceDBSettings",
    "LiteLLMEmbeddingSettings",
    "LiteLLMRerankerSettings",
    "LiteLLMRoleSettings",
    "LlamaCppRoleSettings",
    "MemoryCacheSettings",
    "MemoryTypeConfig",
    "MemspineConfig",
    "ModelDecl",
    "ModelSwitchConfig",
    "ModelsConfig",
    "NamespaceConfig",
    "Neo4jSettings",
    "ObservabilityConfig",
    "OfflineConfig",
    "OpenSearchSettings",
    "PgvectorSettings",
    "PostgresSettings",
    "PrivacyConfig",
    "ProjectionSwitchConfig",
    "PromptsConfig",
    "ProviderServicesConfig",
    "ProviderSettings",
    "ProviderSettingsMap",
    "Qwen3RerankerSettings",
    "ReadConfig",
    "RemoteCacheSettings",
    "RestApiKeyConfig",
    "RestAuthConfig",
    "RestConfig",
    "RestJwtConfig",
    "RestRateLimitConfig",
    "RetentionClassConfig",
    "RetentionConfig",
    "RuntimeConfig",
    "SentenceTransformersEmbeddingSettings",
    "ServiceSwitchesConfig",
    "SqliteAdjacencySettings",
    "SqliteEncryptionSettings",
    "SqliteSettings",
    "StaticEmbeddingSettings",
    "StorageConfig",
    "StorageSwitchConfig",
    "StructuredConfig",
    "TantivySettings",
    "VectorConfig",
    "WeaviateSettings",
    "WorkersConfig",
    "WriteConfig",
]
