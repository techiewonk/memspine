"""The MongoDB driver vocabulary the records adapter builds queries with (RM-112, RM-113).

Adapters never import transport packages (``services-no-transport``): the connector
package owns the driver, and hands the adapter the query-building names it needs (sort
orders, return-document modes, concerns, errors, the BSON id type, the GridFS missing-file
error, and typing aliases).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bson import ObjectId
from gridfs.errors import NoFile
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import BulkWriteError, CollectionInvalid, DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

if TYPE_CHECKING:
    from gridfs import AsyncGridFSBucket
    from pymongo.asynchronous.client_session import AsyncClientSession
    from pymongo.asynchronous.collection import AsyncCollection
    from pymongo.asynchronous.database import AsyncDatabase
    from pymongo.synchronous.collection import Collection
    from pymongo.synchronous.database import Database

__all__ = [
    "ASCENDING",
    "DESCENDING",
    "AsyncClientSession",
    "AsyncCollection",
    "AsyncDatabase",
    "AsyncGridFSBucket",
    "BulkWriteError",
    "Collection",
    "CollectionInvalid",
    "Database",
    "DuplicateKeyError",
    "NoFile",
    "ObjectId",
    "ReadConcern",
    "ReadPreference",
    "ReturnDocument",
    "WriteConcern",
]
