"""S3-compatible object-store connector (``connector.py``) for the ``storage.documents`` s3
provider (RM-112, ``[aws]``)."""

from memspine.clients.s3.connector import (
    ObjectLimitError,
    PreconditionFailedError,
    S3Connector,
    S3Credentials,
    S3Spec,
    StagedObject,
)

__all__ = [
    "ObjectLimitError",
    "PreconditionFailedError",
    "S3Connector",
    "S3Credentials",
    "S3Spec",
    "StagedObject",
]
