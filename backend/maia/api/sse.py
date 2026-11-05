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

from maia.api.sse_manager import sse_broadcast_manager
from maia.config import JWT_SIGNING_ALGORITHM, JWT_SIGNING_SECRET

logger = logging.getLogger(__name__)

sse_router = APIRouter(prefix="/sse", tags=["Server-Sent Events"])

HEARTBEAT_INTERVAL_SECONDS = 25  # reverse proxies drop idle streams (typically after ~100 s)


def _build_heartbeat_payload() -> dict:
    """The event sent when the queue stays silent for HEARTBEAT_INTERVAL_SECONDS."""
    return {"type": "heartbeat", "timestamp": datetime.now().isoformat()}


def _format_event_frame(event_payload: dict) -> str:
    """One event in the SSE wire format: a `data:` line with the JSON payload, then a blank line."""
    return f"data: {json.dumps(event_payload, default=str, ensure_ascii=False)}\n\n"


class SSEConnection:
    """One browser subscription: a queue that `stream_events` drains."""

    def __init__(self, tenant_name: str, user_id: str):
        """Create the queue and a unique connection id (tenant:user:milliseconds)."""
        self.tenant_name = tenant_name
        self.user_id = user_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.active = True
        self.watcher: asyncio.Task | None = None
        self.connection_id = f"{tenant_name}:{user_id}:{int(time.time() * 1000)}"

    async def enqueue_payload(self, sse_event: dict):
        """Queue a payload for the browser."""
        if self.active:
            await self.queue.put(sse_event)

    def tear_down(self):
        """Deactivate the connection and forget it."""
        self.active = False
        sse_broadcast_manager.local_connections.pop(self.connection_id, None)

    async def stream_events(self):
        """Yield the queued payloads as `data:` lines, and a heartbeat after HEARTBEAT_INTERVAL_SECONDS of silence."""
        try:
            while self.active:
                try:
                    event_payload = await asyncio.wait_for(self.queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    event_payload = _build_heartbeat_payload()
                if not self.active:
                    break
                yield _format_event_frame(event_payload)
        except asyncio.CancelledError:
            pass
        finally:
            self.tear_down()


def _authenticate_stream_token(token: str) -> str:
    """The dashboard user id of the stream's JWT (its `sub` claim as a string); 401 when the token does not decode."""
    try:
        token_claims = jwt.decode(token, JWT_SIGNING_SECRET, algorithms=[JWT_SIGNING_ALGORITHM])
        dashboard_user_id = str(token_claims.get("sub"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    return dashboard_user_id


def _register_browser_connection(tenant_name: str, dashboard_user_id: str) -> SSEConnection:
    """Create the browser's connection, add it to the open connections of this process and log the new count."""
    browser_connection = SSEConnection(tenant_name, dashboard_user_id)
    sse_broadcast_manager.local_connections[browser_connection.connection_id] = browser_connection
    logger.info(
        f"SSE connected: {browser_connection.connection_id} ({len(sse_broadcast_manager.local_connections)} open)"
    )
    return browser_connection


async def _watch_browser_disconnect(browser_connection: SSEConnection, http_request: Request):
    """Tear down the connection once the browser is gone (polled every 5 s)."""
    try:
        while browser_connection.active:
            if await http_request.is_disconnected():
                break
            await asyncio.sleep(5)
    finally:
        browser_connection.tear_down()
        logger.info(f"SSE closed: {browser_connection.connection_id}")


@sse_router.get("/{tenant_name}/conversations")
async def stream_conversation_updates(
    tenant_name: str,
    http_request: Request,
    token: str = Query(..., description="JWT of the dashboard user (EventSource cannot send headers)"),
):
    """Open the event stream of a tenant's inbox. Each event is `{"type": "message_update", "conversation": {...}}`."""
    dashboard_user_id = _authenticate_stream_token(token)

    browser_connection = _register_browser_connection(tenant_name, dashboard_user_id)
    browser_connection.watcher = asyncio.create_task(_watch_browser_disconnect(browser_connection, http_request))
    return StreamingResponse(
        browser_connection.stream_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def start_sse_broadcast_listener():
    """FastAPI startup: subscribe to the Redis broadcast channel."""
    await sse_broadcast_manager.start_broadcast_listener()


async def stop_sse_broadcast_listener():
    """FastAPI shutdown: stop the listener and close the connections."""
    await sse_broadcast_manager.stop_broadcast_listener()
