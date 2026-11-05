"""Bridge between the Redis channel `sse:broadcast` and the browsers subscribed to this process.

Every process that saves a conversation (FastAPI inline, the ARQ reply worker) publishes it on the channel from
`Conversation.save()`; this listener hands the payload to the open SSE connections of the same tenant.
"""

import asyncio
import json
import logging
from typing import Dict, Optional

import redis.asyncio as aioredis

from maia.services.redis_clients import build_async_redis_client

logger = logging.getLogger(__name__)

SSE_BROADCAST_CHANNEL = "sse:broadcast"


class RedisSSEManager:
    """Subscribes to the broadcast channel and fans each payload out to the local connections of its tenant."""

    def __init__(self):
        """Nothing is opened here; `start_broadcast_listener()` runs at FastAPI startup."""
        self.redis: Optional[aioredis.Redis] = None
        self.pubsub = None
        self.local_connections: Dict[str, object] = {}  # connection_id -> SSEConnection
        self._listener: Optional[asyncio.Task] = None

    async def _open_broadcast_subscription(self):
        """Open a new pubsub on the Redis client and subscribe it to the broadcast channel (sets `self.pubsub`)."""
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe(SSE_BROADCAST_CHANNEL)

    async def start_broadcast_listener(self):
        """Open the Redis connection, subscribe to the channel and start the listener task."""
        try:
            self.redis = build_async_redis_client(should_decode_responses=True, health_check_interval=30)
            await self.redis.ping()
            await self._open_broadcast_subscription()
            self._listener = asyncio.create_task(self._listen_for_broadcasts())
            logger.info(f"SSE manager subscribed to {SSE_BROADCAST_CHANNEL}")
        except Exception as startup_error:
            logger.error(f"Failed to initialize the SSE manager: {startup_error}")
            self.redis = None
            self.pubsub = None

    async def _decode_and_fan_out_broadcast(self, channel_message: dict):
        """Hand one message of the channel to its tenant's connections; skip non-data and malformed messages. Any other
        error propagates to `_listen_for_broadcasts`, which resubscribes.
        """
        if channel_message.get("type") != "message" or not channel_message.get("data"):
            return
        try:
            broadcast_envelope = json.loads(channel_message["data"])
        except json.JSONDecodeError as decode_error:
            logger.warning(f"Ignoring a malformed SSE broadcast: {decode_error}")
            return
        tenant_name, update_event = broadcast_envelope.get("tenant_name"), broadcast_envelope.get("data")
        if tenant_name and update_event:
            await self._fan_out_to_tenant_connections(tenant_name, update_event)

    async def _listen_for_broadcasts(self):
        """Deliver every broadcast to the local connections of its tenant; resubscribe after a Redis error."""
        while True:
            try:
                async for channel_message in self.pubsub.listen():
                    await self._decode_and_fan_out_broadcast(channel_message)
            except asyncio.CancelledError:
                raise
            except Exception as redis_error:
                logger.warning(f"SSE listener lost Redis ({redis_error}); resubscribing in 2 s")
                await asyncio.sleep(2)
                try:
                    await self._open_broadcast_subscription()
                except Exception as resubscribe_error:
                    logger.error(f"SSE listener could not resubscribe: {resubscribe_error}")

    async def _fan_out_to_tenant_connections(self, tenant_name: str, sse_event: dict):
        """Queue the payload on every active connection of the tenant."""
        for sse_connection in list(self.local_connections.values()):
            if sse_connection.tenant_name == tenant_name and sse_connection.active:
                await sse_connection.enqueue_payload(sse_event)

    async def _cancel_listener_task(self):
        """Cancel the listener task and await its end, swallowing CancelledError and errors; no-op without a task."""
        if self._listener:
            self._listener.cancel()
            try:
                await self._listener
            except (asyncio.CancelledError, Exception):
                pass
            self._listener = None

    async def _close_redis_connection(self):
        """Close the pubsub, then the Redis client (an error is only logged), and forget both."""
        try:
            if self.pubsub:
                await self.pubsub.close()
            if self.redis:
                await self.redis.close()
        except Exception as close_error:
            logger.warning(f"Error closing the SSE Redis connection: {close_error}")
        self.pubsub = None
        self.redis = None

    async def stop_broadcast_listener(self):
        """Stop the listener, deactivate the connections and close Redis."""
        await self._cancel_listener_task()
        for sse_connection in list(self.local_connections.values()):
            sse_connection.active = False
        self.local_connections.clear()
        await self._close_redis_connection()


sse_broadcast_manager = RedisSSEManager()
