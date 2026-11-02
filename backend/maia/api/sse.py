"""Server-sent events for the inbox: a dashboard user subscribes to a tenant and receives every saved conversation
live (new message, bot answer, manager reply, status change) as a `message_update` event.

The browser opens `GET /management/v1/sse/{tenant}/conversations?token=<JWT>` with `EventSource` (which cannot send
headers, hence the query parameter). The events themselves are published by `Conversation.save()` and delivered by
`maia.api.sse_manager`.
"""

import asyncio
import json
import logging
import time
from datetime import datetime

import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from maia.api.sse_manager import redis_sse_manager
from maia.config import ALGORITHM, JWT_SECRET

logger = logging.getLogger(__name__)

sse_router = APIRouter(prefix="/sse", tags=["Server-Sent Events"])

HEARTBEAT_SECONDS = 25  # reverse proxies drop idle streams (typically after ~100 s)


class SSEConnection:
    """One browser subscription: a queue that the stream generator drains."""

    def __init__(self, tenant_name: str, user_id: str):
        """Create the queue and a unique connection id (tenant:user:milliseconds)."""
        self.tenant_name = tenant_name
        self.user_id = user_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.active = True
        self.watcher: asyncio.Task | None = None
        self.connection_id = f"{tenant_name}:{user_id}:{int(time.time() * 1000)}"

    async def send_data(self, data: dict):
        """Queue a payload for the browser."""
        if self.active:
            await self.queue.put(data)

    def close(self):
        """Deactivate the connection and forget it."""
        self.active = False
        redis_sse_manager.local_connections.pop(self.connection_id, None)

    async def generator(self):
        """Yield the queued payloads as `data:` lines, and a heartbeat after HEARTBEAT_SECONDS of silence."""
        try:
            while self.active:
                try:
                    data = await asyncio.wait_for(self.queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    data = {"type": "heartbeat", "timestamp": datetime.now().isoformat()}
                if not self.active:
                    break
                yield f"data: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self.close()


@sse_router.get("/{tenant_name}/conversations")
async def conversations_sse(
    tenant_name: str,
    request: Request,
    token: str = Query(..., description="JWT of the dashboard user (EventSource cannot send headers)"),
):
    """Open the event stream of a tenant's inbox. Each event is `{"type": "message_update", "conversation": {...}}`."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id = str(payload.get("sub"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    connection = SSEConnection(tenant_name, user_id)
    redis_sse_manager.local_connections[connection.connection_id] = connection
    logger.info(f"SSE connected: {connection.connection_id} ({len(redis_sse_manager.local_connections)} open)")

    async def watch_disconnect():
        """Close the connection once the browser is gone (polled every 5 s)."""
        try:
            while connection.active:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(5)
        finally:
            connection.close()
            logger.info(f"SSE closed: {connection.connection_id}")

    connection.watcher = asyncio.create_task(watch_disconnect())
    return StreamingResponse(
        connection.generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def start_sse_manager():
    """FastAPI startup: subscribe to the Redis broadcast channel."""
    await redis_sse_manager.initialize()


async def stop_sse_manager():
    """FastAPI shutdown: stop the listener and close the connections."""
    await redis_sse_manager.shutdown()
