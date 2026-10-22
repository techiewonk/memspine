"""Filesystem connector (``connector.py``) for the ``storage.documents`` filesystem provider."""

from memspine.clients.fs.connector import (
    FileLimitError,
    FilesystemConnector,
    FilesystemSpec,
    StagedFile,
)

__all__ = ["FileLimitError", "FilesystemConnector", "FilesystemSpec", "StagedFile"]
