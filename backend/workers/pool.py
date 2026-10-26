"""
ARQ connection pool manager to avoid multiple Redis connections
across FastAPI instances (singleton pattern)
"""

import asyncio
from typing import Optional

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from maia.services.redis_clients import get_arq_redis_settings


class ARQPoolManager:
    """
    Singleton class to manage ARQ Redis pool connections.
    Ensures only one connection pool is created across all FastAPI worker instances.
    """

    _instance: Optional["ARQPoolManager"] = None
    _pool: Optional[ArqRedis] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # Only initialize once
        if not hasattr(self, "_initialized"):
            self._initialized = True
            self._pool = None
            self._lock = asyncio.Lock()

    def _get_redis_settings(self) -> RedisSettings:
        return get_arq_redis_settings(conn_timeout=10, conn_retries=3, conn_retry_delay=1)

    async def get_pool(self) -> ArqRedis:
        """
        Get or create the ARQ Redis pool.
        Uses asyncio Lock to prevent concurrent pool creation.
        """
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await create_pool(self._get_redis_settings())
        return self._pool

    async def reset_pool(self) -> ArqRedis:
        """
        Force-close the current pool and create a fresh one.
        Called when the existing connection is detected as dead.
        """
        async with self._lock:
            if self._pool is not None:
                try:
                    await self._pool.close()
                except Exception:
                    pass
                self._pool = None
            self._pool = await create_pool(self._get_redis_settings())
        return self._pool

    async def close(self):
        """Close the pool connection"""
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


# Global instance
arq_pool_manager = ARQPoolManager()
