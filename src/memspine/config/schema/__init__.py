"""Config tree (D-11). Constructed by the loader from merged layers.

``namespaces.<ns>.memories`` is RESERVED for v0.2 per-namespace type enablement
(D-14): the key is rejected today so configs written now stay forward-compatible.

F-07: one module per area (``storage``, ``llm``, ``read``, ``write``, ``firewall``,
``integrity``, ``privacy``, ``observability``, ``rest``, ``root``). This package is the real
``memspine.config.schema`` path; every config class is exported here.
"""

from __future__ import annotations

from memspine.config.schema.firewall import FirewallConfig, FirewallSignalsConfig
from memspine.config.schema.integrity import IntegrityConfig
from memspine.config.schema.llm import (
    DecisionConfig,
    EmbeddingConfig,
    LLMConfig,
    LLMRoleConfig,
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
from memspine.config.schema.read import ReadConfig
from memspine.config.schema.rest import (
    RestApiKeyConfig,
    RestAuthConfig,
    RestConfig,
    RestJwtConfig,
    RestRateLimitConfig,
)
from memspine.config.schema.root import MemoryTypeConfig, MemspineConfig, NamespaceConfig
from memspine.config.schema.storage import (
    CacheConfig,
    EncryptionConfig,
    EventLogConfig,
    GraphConfig,
    StorageConfig,
    VectorConfig,
)
from memspine.config.schema.write import DataShapeConfig, IngestConfig, WriteConfig

__all__ = [
    "AuditConfig",
    "CacheConfig",
    "ConsentConfig",
    "DataShapeConfig",
    "DecisionConfig",
    "EmbeddingConfig",
    "EncryptionConfig",
    "EventLogConfig",
    "FirewallConfig",
    "FirewallSignalsConfig",
    "GraphConfig",
    "IngestConfig",
    "IntegrityConfig",
    "LLMConfig",
    "LLMRoleConfig",
    "MemoryTypeConfig",
    "MemspineConfig",
    "NamespaceConfig",
    "ObservabilityConfig",
    "OfflineConfig",
    "PrivacyConfig",
    "PromptsConfig",
    "ReadConfig",
    "RestApiKeyConfig",
    "RestAuthConfig",
    "RestConfig",
    "RestJwtConfig",
    "RestRateLimitConfig",
    "RetentionClassConfig",
    "RetentionConfig",
    "StorageConfig",
    "StructuredConfig",
    "VectorConfig",
    "WorkersConfig",
    "WriteConfig",
]
