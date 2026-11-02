"""Bridge between the Redis channel `sse:broadcast` and the browsers subscribed to this process.

Every process that saves a conversation (FastAPI inline, the ARQ reply worker) publishes it on the channel from
`Conversation.save()`; this listener hands the payload to the open SSE connections of the same tenant.
"""

import asyncio
import json
import logging
from typing import Dict, Optional

import redis.asyncio as aioredis

from maia.services.redis_clients import get_async_redis

logger = logging.getLogger(__name__)

BROADCAST_CHANNEL = "sse:broadcast"


class RedisSSEManager:
    """Subscribes to the broadcast channel and fans each payload out to the local connections of its tenant."""

    def __init__(self):
        """Nothing is opened here; `initialize()` runs at FastAPI startup."""
        self.redis: Optional[aioredis.Redis] = None
        self.pubsub = None
        self.local_connections: Dict[str, object] = {}  # connection_id -> SSEConnection
        self._listener: Optional[asyncio.Task] = None

    async def initialize(self):
        """Open the Redis connection, subscribe to the channel and start the listener task."""
        try:
            self.redis = get_async_redis(decode_responses=True, health_check_interval=30)
            await self.redis.ping()
            self.pubsub = self.redis.pubsub()
            await self.pubsub.subscribe(BROADCAST_CHANNEL)
            self._listener = asyncio.create_task(self._listen())
            logger.info(f"SSE manager subscribed to {BROADCAST_CHANNEL}")
        except Exception as e:
            logger.error(f"Failed to initialize the SSE manager: {e}")
            self.redis = None
            self.pubsub = None

    async def _listen(self):
        """Deliver every broadcast to the local connections of its tenant; resubscribe after a Redis error."""
        while True:
            try:
                async for message in self.pubsub.listen():
                    if message.get("type") != "message" or not message.get("data"):
                        continue
                    try:
                        payload = json.loads(message["data"])
                    except json.JSONDecodeError as e:
                        logger.warning(f"Ignoring a malformed SSE broadcast: {e}")
                        continue
                    tenant_name, data = payload.get("tenant_name"), payload.get("data")
                    if tenant_name and data:
                        await self._send_to_local_connections(tenant_name, data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"SSE listener lost Redis ({e}); resubscribing in 2 s")
                await asyncio.sleep(2)
                try:
                    self.pubsub = self.redis.pubsub()
                    await self.pubsub.subscribe(BROADCAST_CHANNEL)
                except Exception as sub_error:
                    logger.error(f"SSE listener could not resubscribe: {sub_error}")

    async def _send_to_local_connections(self, tenant_name: str, data: dict):
        """Queue the payload on every active connection of the tenant."""
        for connection in list(self.local_connections.values()):
            if connection.tenant_name == tenant_name and connection.active:
                await connection.send_data(data)

    async def shutdown(self):
        """Stop the listener, deactivate the connections and close Redis."""
        if self._listener:
            self._listener.cancel()
            try:
                await self._listener
            except (asyncio.CancelledError, Exception):
                pass
            self._listener = None
        for connection in list(self.local_connections.values()):
            connection.active = False
        self.local_connections.clear()
        try:
            if self.pubsub:
                await self.pubsub.close()
            if self.redis:
                await self.redis.close()
        except Exception as e:
            logger.warning(f"Error closing the SSE Redis connection: {e}")
        self.pubsub = None
        self.redis = None


redis_sse_manager = RedisSSEManager()
