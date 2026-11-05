"""
ARQ connection pool manager to avoid multiple Redis connections
across FastAPI instances (singleton pattern)
"""

import asyncio
from typing import Optional

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from maia.services.redis_clients import build_arq_redis_settings


class ARQPoolManager:
    """
    Singleton class to manage ARQ Redis pool connections.
    Ensures only one connection pool is created across all FastAPI worker instances.
    """

    _shared_instance: Optional["ARQPoolManager"] = None
    _redis_pool: Optional[ArqRedis] = None

    def __new__(cls):
        """Singleton: always return the same instance."""
        if cls._shared_instance is None:
            cls._shared_instance = super().__new__(cls)
        return cls._shared_instance

    def __init__(self):
        # Only initialize once
        """Initialize the pool slot and the lock once."""
        if not hasattr(self, "_initialized"):
            self._initialized = True
            self._redis_pool = None
            self._lock = asyncio.Lock()

    def _build_redis_settings_with_short_timeouts(self) -> RedisSettings:
        """ARQ RedisSettings from REDIS_URL, with short connection timeouts."""
        return build_arq_redis_settings(conn_timeout=10, conn_retries=3, conn_retry_delay=1)

    async def ensure_pool(self) -> ArqRedis:
        """
        Get or create the ARQ Redis pool.
        Uses asyncio Lock to prevent concurrent pool creation.
        """
        if self._redis_pool is None:
            async with self._lock:
                if self._redis_pool is None:
                    self._redis_pool = await create_pool(self._build_redis_settings_with_short_timeouts())
        return self._redis_pool

    async def recreate_pool(self) -> ArqRedis:
        """
        Force-close the current pool and create a fresh one.
        Called when the existing connection is detected as dead.
        """
        async with self._lock:
            if self._redis_pool is not None:
                try:
                    await self._redis_pool.close()
                except Exception:
                    pass
                self._redis_pool = None
            self._redis_pool = await create_pool(self._build_redis_settings_with_short_timeouts())
        return self._redis_pool

    async def tear_down(self):
        """Close the pool connection"""
        async with self._lock:
            if self._redis_pool is not None:
                await self._redis_pool.close()
                self._redis_pool = None


# Global instance
arq_redis_pool_manager = ARQPoolManager()
