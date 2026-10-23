"""MongoDB connector package (RM-112, ``[mongodb]`` extra). pymongo is imported lazily.

:mod:`~memspine.clients.mongodb.launcher` is the opt-in managed local ``mongod`` (ADR-083)."""

from memspine.clients.mongodb.connector import MongoConnector, MongoSpec, StandaloneServerError
from memspine.clients.mongodb.launcher import MongodLauncher, MongodSpec

__all__ = ["MongoConnector", "MongoSpec", "MongodLauncher", "MongodSpec", "StandaloneServerError"]
