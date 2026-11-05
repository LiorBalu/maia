"""Mongo connections: mongoengine, raw pymongo `pymongo_database` and the odmantic engine."""

import logging
from os import getenv
from typing import Optional
from pymongo import MongoClient
from maia.config import MONGO_CONNECTION_STRING
from odmantic import AIOEngine
from motor.motor_asyncio import AsyncIOMotorClient
from mongoengine import connect

logger = logging.getLogger(__name__)

MONGO_DATABASE_NAME = getenv("MAIA_DB_NAME", "maia")
motor_client: AsyncIOMotorClient = AsyncIOMotorClient(
    getenv("MONGO_URI", "mongodb://localhost:27017")
)
connect(MONGO_DATABASE_NAME, host=getenv("MONGO_URI", "mongodb://localhost:27017"))

odmantic_engine: AIOEngine = AIOEngine(
    client=motor_client, database=MONGO_DATABASE_NAME
)
pymongo_client: MongoClient = MongoClient(
    getenv("MONGO_URI", "mongodb://localhost:27017")
)
pymongo_database = pymongo_client[MONGO_DATABASE_NAME]


def _resolve_mongo_uri() -> str:
    """The connection string MongoService opens: MONGO_CONNECTION_STRING from maia.config, else the local default
    (with a warning). The module-level clients above read MONGO_URI themselves, with their own default.
    """
    connection_uri = MONGO_CONNECTION_STRING
    if not connection_uri:
        connection_uri = "mongodb://localhost:27017/"
        logger.warning("MONGO_URI environment variable not found. Using default value.")
    return connection_uri


class MongoService:
    """
    A class representing a MongoDB service.
    Args:
        db_name (str): The name of the database to connect to.
        client (MongoClient | None): An optional MongoClient instance.
        If not provided, a new MongoClient will be created.
    Attributes:
        client (MongoClient): The MongoClient instance used to connect to the MongoDB server.
        db (Database): The MongoDB database object.
    Raises:
        Exception: If an error occurs while connecting to MongoDB.
    """

    """
    Initializes a new instance of the MongoService class.
    Args:
        db_name (str): The name of the database to connect to.
        client (MongoClient | None): An optional MongoClient instance.
        If not provided, a new MongoClient will be created.
    """

    def __init__(self, db_name, client: Optional[MongoClient] = None):
        """Open a MongoClient on MONGO_CONNECTION_STRING (or use the given client) and select the database."""
        connection_uri = _resolve_mongo_uri()
        try:
            self.client: MongoClient = client or MongoClient(
                connection_uri, serverSelectionTimeoutMS=5000
            )
            self.db = self.client[db_name]
            logger.info(f"Connected to MongoDB database: {db_name}")

        except Exception as error:
            logger.error(f"An error occurred while connecting to MongoDB: {error}")
            raise
