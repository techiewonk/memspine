"""Documents capability port (ADR-081 ``storage.documents``): non-structured payloads by
namespace and content hash. The event log keeps references; this store keeps the bytes."""

from memspine.services.documents.base import (
    DocumentHead,
    DocumentNotFoundError,
    DocumentStore,
    DocumentTooLargeError,
)

__all__ = ["DocumentHead", "DocumentNotFoundError", "DocumentStore", "DocumentTooLargeError"]
