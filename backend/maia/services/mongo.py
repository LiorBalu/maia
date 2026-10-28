"""Mongo connections: mongoengine, raw pymongo `db` and the odmantic engine."""

import logging
from os import getenv
from typing import Optional
from pymongo import MongoClient
from maia.config import MONGO_URI
from odmantic import AIOEngine
from motor.motor_asyncio import AsyncIOMotorClient
from mongoengine import connect

logger = logging.getLogger(__name__)

DB_NAME = getenv("MAIA_DB_NAME", "maia")
client_mongo: AsyncIOMotorClient = AsyncIOMotorClient(
    getenv("MONGO_URI", "mongodb://localhost:27017")
)
connect(DB_NAME, host=getenv("MONGO_URI", "mongodb://localhost:27017"))

manager_engine: AIOEngine = AIOEngine(
    client=client_mongo, database=DB_NAME
)
mongo_client: MongoClient = MongoClient(
    getenv("MONGO_URI", "mongodb://localhost:27017")
)
db = mongo_client[DB_NAME]


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
        """Open a MongoClient on MONGO_URI (or use the given client) and select the database."""
        mongo_str = MONGO_URI
        if not mongo_str:
            mongo_str = "mongodb://localhost:27017/"
            logger.warning("MONGO_URI environment variable not found. Using default value.")
        try:
            self.client: MongoClient = client or MongoClient(
                mongo_str, serverSelectionTimeoutMS=5000
            )
            self.db = self.client[db_name]
            logger.info(f"Connected to MongoDB database: {db_name}")

        except Exception as e:
            logger.error(f"An error occurred while connecting to MongoDB: {e}")
            raise
