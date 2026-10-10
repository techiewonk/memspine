"""E03: public-knowledge evidence for invited inference. Opt-in (``read.external_evidence``).

Only generic topic words (after :mod:`~memspine.services.external.privacy`) leave the process;
the result is cited as ``[public knowledge]``, never stored, and never proves a private fact.
"""

from memspine.services.external.broker import (
    PUBLIC_KNOWLEDGE_CLAUSE,
    PUBLIC_MARKER,
    EvidenceCache,
    ExternalBroker,
    ExternalResult,
    ExternalStats,
    format_public_block,
)
from memspine.services.external.privacy import (
    PrivacyLeakError,
    PublicQuery,
    assert_public,
    build_public_query,
    private_terms_from,
)
from memspine.services.external.provider import (
    ExternalProvider,
    ExternalSnippet,
    HttpSearchProvider,
    NoopProvider,
    ProviderError,
    ProviderUnavailableError,
    clean_snippet,
)
from memspine.services.external.trigger import invites_inference, is_private_fact_question

__all__ = [
    "PUBLIC_KNOWLEDGE_CLAUSE",
    "PUBLIC_MARKER",
    "EvidenceCache",
    "ExternalBroker",
    "ExternalProvider",
    "ExternalResult",
    "ExternalSnippet",
    "ExternalStats",
    "HttpSearchProvider",
    "NoopProvider",
    "PrivacyLeakError",
    "ProviderError",
    "ProviderUnavailableError",
    "PublicQuery",
    "assert_public",
    "build_public_query",
    "clean_snippet",
    "format_public_block",
    "invites_inference",
    "is_private_fact_question",
    "private_terms_from",
]
