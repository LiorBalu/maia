"""Redis-backed registry of SSE connections across API instances, so a broadcast reaches every subscribed browser."""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from os import getenv
from typing import Dict, List, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# Custom JSON encoder for datetime objects
class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class RedisSSEManager:
    """
    Redis-based SSE connection manager for stateless Docker deployments.
    Handles connection state and broadcasting using Redis.
    """

    def __init__(self, redis_url: str = None):
        # Use the same Redis URL as Celery
        self.redis_url = redis_url or getenv("REDIS_URL", "redis://localhost:6379/0")
        self.redis: Optional[aioredis.Redis] = None
        self.pubsub: Optional[aioredis.client.PubSub] = None
        self.local_connections: Dict[str, object] = {}

        # Generate a more stable instance ID using hostname and process ID
        import os
        import socket

        hostname = socket.gethostname()
        pid = os.getpid()
        self.instance_id = f"sse_{hostname}_{pid}_{int(time.time())}"

        # Redis keys
        self.connections_key = "sse:connections"
        self.heartbeat_key = "sse:heartbeats"
        self.broadcast_channel = "sse:broadcast"
        self.instance_connections_key = f"sse:instance:{self.instance_id}"

        # Circuit breaker to prevent infinite loops
        self._listener_active = False
        self._shutdown_requested = False

    async def _ensure_redis_connection(self):
        """Ensure Redis connection is established and healthy"""
        if self.redis is None:
            logger.warning("Redis connection not initialized, attempting to initialize...")
            await self.initialize()
        else:
            # Test existing connection
            try:
                await self.redis.ping()
            except Exception as e:
                logger.warning(f"Redis connection unhealthy, reinitializing: {e}")
                await self.initialize()
        return self.redis is not None

    async def is_healthy(self) -> bool:
        """Check if the Redis connection is healthy and ready to use"""
        try:
            if self.redis is None:
                return False
            await self.redis.ping()
            return True
        except Exception:
            return False

    async def initialize(self):
        """Initialize Redis connection and pubsub"""
        try:
            # Create Redis connection with basic configuration
            try:
                self.redis = aioredis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    retry_on_timeout=True,
                    health_check_interval=30,
                )
            except Exception as init_error:
                logger.warning(f"Failed with advanced config, trying basic: {init_error}")
                # Fallback to minimal configuration
                self.redis = aioredis.from_url(
                    self.redis_url,
                    decode_responses=True,
                )

            # Test connection
            await self.redis.ping()

            self.pubsub = self.redis.pubsub()
            await self.pubsub.subscribe(self.broadcast_channel)

            # Start broadcast listener with protection
            if not self._listener_active:
                self._listener_active = True
                asyncio.create_task(self._listen_for_broadcasts())

            logger.debug(f"Redis SSE Manager initialized - Instance: {self.instance_id}")

        except Exception as e:
            logger.error(f"Failed to initialize Redis SSE Manager: {e}")
            # Don't raise here, allow graceful degradation
            self.redis = None
            self.pubsub = None

    async def register_connection(self, connection_id: str, tenant_name: str, user_id: str, filters: dict = None):
        """Register a new SSE connection in Redis with filters"""
        try:
            if not await self._ensure_redis_connection():
                logger.error("Cannot register connection: Redis not available")
                return

            connection_data = {
                "tenant_name": tenant_name,
                "user_id": user_id,
                "instance_id": self.instance_id,
                "created_at": datetime.now().isoformat(),
                "last_heartbeat": datetime.now().isoformat(),
                "is_active": "true",  # Convert boolean to string for Redis
                "filters": json.dumps(filters or {}, cls=DateTimeEncoder),  # Store filters as JSON
            }

            # Store in Redis hash
            await self.redis.hset(f"{self.connections_key}:{connection_id}", mapping=connection_data)

            # Set TTL for auto-cleanup (10 minutes)
            await self.redis.expire(f"{self.connections_key}:{connection_id}", 600)

            # Add to tenant index for quick lookups
            await self.redis.sadd(f"sse:tenant:{tenant_name}", connection_id)
            await self.redis.expire(f"sse:tenant:{tenant_name}", 600)

            # Add to instance-specific tracking for cleanup
            await self.redis.sadd(self.instance_connections_key, connection_id)
            await self.redis.expire(self.instance_connections_key, 600)

            logger.debug(f"Registered SSE connection: {connection_id} on instance {self.instance_id}")

        except Exception as e:
            logger.error(f"Error registering connection {connection_id}: {e}")

    async def unregister_connection(self, connection_id: str):
        """Remove SSE connection from Redis"""
        try:
            if not await self._ensure_redis_connection():
                logger.error("Cannot unregister connection: Redis not available")
                return

            # Get connection data first
            conn_data = await self.redis.hgetall(f"{self.connections_key}:{connection_id}")

            if conn_data:
                # Only allow unregistering connections from the same instance
                if conn_data.get("instance_id") != self.instance_id:
                    logger.warning(
                        f"Attempted to unregister connection {connection_id} from different instance. "
                        f"Current: {self.instance_id}, Connection: {conn_data.get('instance_id')}"
                    )
                    return

                # Remove from tenant index
                if conn_data.get("tenant_name"):
                    await self.redis.srem(f"sse:tenant:{conn_data['tenant_name']}", connection_id)

            # Remove connection data
            await self.redis.delete(f"{self.connections_key}:{connection_id}")

            # Remove from instance tracking
            await self.redis.srem(self.instance_connections_key, connection_id)

            # Remove from local connections
            if connection_id in self.local_connections:
                del self.local_connections[connection_id]

            logger.debug(f"Unregistered SSE connection: {connection_id}")

        except Exception as e:
            logger.error(f"Error unregistering connection {connection_id}: {e}")

    async def update_heartbeat(self, connection_id: str):
        """Update connection heartbeat"""
        try:
            if not await self._ensure_redis_connection():
                return

            # Only update heartbeat for connections from this instance
            conn_data = await self.redis.hgetall(f"{self.connections_key}:{connection_id}")
            if conn_data and conn_data.get("instance_id") == self.instance_id:
                await self.redis.hset(
                    f"{self.connections_key}:{connection_id}",
                    "last_heartbeat",
                    datetime.now().isoformat(),
                )
                # Refresh TTL
                await self.redis.expire(f"{self.connections_key}:{connection_id}", 600)

                # Refresh instance tracking TTL
                await self.redis.expire(self.instance_connections_key, 600)

        except Exception as e:
            logger.error(f"Error updating heartbeat for {connection_id}: {e}")

    async def get_tenant_connections(self, tenant_name: str) -> List[Dict]:
        """Get all active connections for a tenant"""
        try:
            if not await self._ensure_redis_connection():
                logger.error(f"Cannot get tenant connections for {tenant_name}: Redis not available")
                return []

            connection_ids = await self.redis.smembers(f"sse:tenant:{tenant_name}")
            connections = []

            for conn_id in connection_ids:
                conn_data = await self.redis.hgetall(f"{self.connections_key}:{conn_id}")
                if conn_data and conn_data.get("is_active") == "true":  # Check string value
                    # Check if connection is recent (within 10 minutes)
                    last_heartbeat = datetime.fromisoformat(conn_data.get("last_heartbeat", "1970-01-01"))
                    if datetime.now() - last_heartbeat < timedelta(minutes=10):
                        connections.append(
                            {
                                "connection_id": conn_id,
                                "user_id": conn_data.get("user_id"),
                                "instance_id": conn_data.get("instance_id"),
                                "last_heartbeat": conn_data.get("last_heartbeat"),
                                "filters": json.loads(conn_data.get("filters", "{}")),
                            }
                        )
                    else:
                        # Clean up stale connection
                        await self.unregister_connection(conn_id)

            return connections

        except Exception as e:
            logger.error(f"Error getting tenant connections for {tenant_name}: {e}")
            return []

    async def broadcast_to_tenant(self, tenant_name: str, data: dict):
        """Broadcast data to all connections in a tenant with enhanced error handling"""
        max_retries = 3  # Increased from 2 for better resilience
        retry_count = 0

        while retry_count < max_retries:
            try:
                if not await self._ensure_redis_connection():
                    logger.error(
                        f"Cannot broadcast to tenant {tenant_name}: Redis not available "
                        f"(attempt {retry_count + 1}/{max_retries})"
                    )
                    if retry_count < max_retries - 1:
                        retry_count += 1
                        # Exponential backoff for Redis connection retries
                        await asyncio.sleep(1 * (2**retry_count))
                        continue
                    return

                # Validate data before broadcasting
                if not data:
                    logger.warning(f"Attempting to broadcast empty data to tenant {tenant_name}")
                    return

                broadcast_data = {
                    "tenant_name": tenant_name,
                    "data": data,
                    "timestamp": datetime.now().isoformat(),
                    "sender_instance": self.instance_id,
                }

                # Validate JSON serialization
                try:
                    json_data = json.dumps(broadcast_data, cls=DateTimeEncoder)
                except (TypeError, ValueError) as e:
                    logger.error(f"Error serializing broadcast data: {e}, data: {broadcast_data}")
                    return

                # Publish to Redis channel with timeout
                try:
                    await asyncio.wait_for(
                        self.redis.publish(self.broadcast_channel, json_data),
                        timeout=10.0,  # Increased timeout to handle network delays
                    )

                    logger.debug(f"Broadcasted to tenant {tenant_name}: {data.get('type', 'unknown')}")
                    return  # Success, exit retry loop

                except asyncio.TimeoutError:
                    logger.debug(
                        f"Redis publish timeout for tenant {tenant_name} (attempt {retry_count + 1}/{max_retries})"
                    )
                    if retry_count < max_retries - 1:
                        retry_count += 1
                        # Exponential backoff: 0.5s, 1s, 2s
                        await asyncio.sleep(0.5 * (2**retry_count))
                        continue
                    logger.warning(f"All Redis publish attempts failed for tenant {tenant_name}")
                    raise

            except Exception as e:
                logger.debug(f"Error broadcasting to tenant {tenant_name} (attempt {retry_count + 1}/{max_retries}): {e}")
                if retry_count < max_retries - 1:
                    retry_count += 1
                    # Exponential backoff: 0.5s, 1s, 2s
                    await asyncio.sleep(0.5 * (2**retry_count))
                    continue
                logger.error(f"Failed to broadcast to tenant {tenant_name} after {max_retries} attempts: {e}")
                raise

    async def _listen_for_broadcasts(self):
        """Listen for broadcast messages from Redis with improved error handling"""
        # Prevent multiple listeners from running
        if self._shutdown_requested:
            logger.debug("Shutdown requested, not starting broadcast listener")
            return

        # Create a unique listener lock key for this instance
        listener_lock_key = f"sse:listener_lock:{self.instance_id}"

        # Try to acquire listener lock with Redis
        try:
            lock_acquired = await self.redis.set(
                listener_lock_key,
                "locked",
                nx=True,  # Only set if key doesn't exist
                ex=60,  # Expire after 60 seconds
            )

            if not lock_acquired:
                logger.debug(f"Another listener already active for instance {self.instance_id}")
                return

            logger.debug(f"Acquired listener lock for instance {self.instance_id}")
        except Exception as e:
            logger.warning(f"Failed to acquire listener lock: {e}")
            return

        retry_count = 0
        max_retries = 3  # Reduced from 5
        consecutive_errors = 0
        max_consecutive_errors = 3

        logger.debug(f"Starting broadcast listener for instance {self.instance_id}")

        try:
            while retry_count < max_retries and not self._shutdown_requested:
                try:
                    if not self.pubsub:
                        logger.warning("PubSub not initialized, cannot listen for broadcasts")
                        return

                    # Reset consecutive errors on successful connection
                    consecutive_errors = 0

                    async for message in self.pubsub.listen():
                        if self._shutdown_requested:
                            logger.debug("Shutdown requested, stopping broadcast listener")
                            break

                        # Refresh lock every few messages to prevent expiry
                        try:
                            await self.redis.expire(listener_lock_key, 60)
                        except Exception:
                            pass  # Continue even if lock refresh fails

                        if message["type"] == "message":
                            try:
                                # Validate message data before parsing
                                if not message.get("data"):
                                    logger.warning("Received empty broadcast message")
                                    continue

                                broadcast_data = json.loads(message["data"])
                                tenant_name = broadcast_data.get("tenant_name")
                                data = broadcast_data.get("data")

                                # Prevent processing our own messages to avoid loops
                                # if sender_instance == self.instance_id:
                                #     logger.debug(
                                #         f"Ignoring own broadcast message from instance {self.instance_id}"
                                #     )
                                #     continue

                                if tenant_name and data:
                                    # Send to local connections for this tenant
                                    await self._send_to_local_connections(tenant_name, data)
                                else:
                                    logger.warning(f"Invalid broadcast data format: {broadcast_data}")

                            except json.JSONDecodeError as e:
                                consecutive_errors += 1
                                logger.warning(
                                    f"Error parsing broadcast message JSON: {e}, data: {message.get('data', 'None')}"
                                )
                                if consecutive_errors >= max_consecutive_errors:
                                    logger.warning("Too many consecutive JSON decode errors, breaking")
                                    break
                            except Exception as e:
                                consecutive_errors += 1
                                logger.warning(f"Error processing broadcast message: {e}")
                                if consecutive_errors >= max_consecutive_errors:
                                    logger.warning("Too many consecutive processing errors, breaking")
                                    break

                    # If we reach here, the loop ended normally, reset retry count
                    retry_count = 0

                except Exception as e:
                    if self._shutdown_requested:
                        logger.debug("Shutdown requested during error handling")
                        break

                    error_msg = str(e).lower()
                    consecutive_errors += 1

                    # Check for SSL/connection errors
                    if any(
                        ssl_err in error_msg
                        for ssl_err in [
                            "ssl:",
                            "application_data_after_close_notify",
                            "connection closed by server",
                            "connection lost",
                            "connection reset",
                            "broken pipe",
                            "timed out",
                        ]
                    ):
                        logger.debug(f"Connection error in broadcast listener: {e}")
                        retry_count += 1

                        if retry_count < max_retries and not self._shutdown_requested:
                            backoff_time = min(2**retry_count, 30)  # Increased max backoff
                            logger.debug(
                                f"Attempting to reconnect Redis (attempt {retry_count}/{max_retries}) "
                                f"in {backoff_time}s"
                            )
                            await asyncio.sleep(backoff_time)

                            try:
                                # Clean up current connection
                                if self.pubsub:
                                    await self.pubsub.close()
                                if self.redis:
                                    await self.redis.close()

                                # Reinitialize
                                self._listener_active = False  # Reset listener flag
                                await self.initialize()

                                if self.redis is None:
                                    logger.warning("Failed to reinitialize Redis connection")
                                    break
                                else:
                                    logger.debug("Successfully reconnected to Redis")
                                    consecutive_errors = 0  # Reset on successful reconnection
                                    continue

                            except Exception as reconnect_error:
                                logger.warning(f"Failed to reconnect Redis: {reconnect_error}")
                        else:
                            logger.warning("Max retries exceeded for Redis connection")
                            break
                    else:
                        # Non-connection error, log and exit
                        logger.warning(f"Non-recoverable error in broadcast listener: {e}")
                        break

        finally:
            # Always reset the listener flag when exiting
            self._listener_active = False
            # Release the listener lock
            try:
                await self.redis.delete(listener_lock_key)
                logger.debug(f"Released listener lock for instance {self.instance_id}")
            except Exception as e:
                logger.warning(f"Failed to release listener lock: {e}")
            logger.debug(f"Broadcast listener stopped for instance {self.instance_id}")

    async def _send_to_local_connections(self, tenant_name: str, data: dict):
        """Send data to local SSE connections for this instance only with filtering support"""
        local_tenant_connections = [
            conn
            for conn in self.local_connections.values()
            if hasattr(conn, "tenant_name") and hasattr(conn, "active") and conn.tenant_name == tenant_name and conn.active
        ]

        if not local_tenant_connections:
            logger.debug(f"No local connections found for tenant {tenant_name} on instance {self.instance_id}")
            return

        # Import here to avoid circular imports
        try:
            from maia.api.sse import _conversation_matches_filters, _filter_messages_by_role
        except ImportError:
            logger.error("Could not import filtering functions, sending without filtering")
            _conversation_matches_filters = None
            _filter_messages_by_role = None

        logger.debug(
            f"Sending to {len(local_tenant_connections)} local connections "
            f"in tenant {tenant_name} on instance {self.instance_id}"
        )

        for connection in local_tenant_connections:
            try:
                # Start with original data
                connection_data = data.copy()

                # Check if this is a conversation update that needs filtering
                should_send = True
                if (
                    data.get("type") in ["message_update", "conversation_update"]
                    and data.get("conversation")
                    and hasattr(connection, "filters")
                    and connection.filters
                ):
                    # Apply message role filtering first if needed
                    if _filter_messages_by_role and "include_tools_call" in connection.filters:
                        include_tools_call = connection.filters.get("include_tools_call", True)
                        if data.get("conversation"):
                            filtered_conversation = _filter_messages_by_role(data["conversation"], include_tools_call)
                            connection_data = connection_data.copy()
                            connection_data["conversation"] = filtered_conversation

                    # Apply conversation filters if available
                    if _conversation_matches_filters:
                        should_send = _conversation_matches_filters(connection_data["conversation"], connection.filters)

                        if not should_send:
                            conn_id = getattr(connection, "connection_id", "unknown")
                            logger.debug(f"Conversation filtered out for connection {conn_id}")
                            continue

                if should_send:
                    try:
                        await asyncio.wait_for(connection.send_data(connection_data), timeout=5.0)
                    except asyncio.TimeoutError:
                        conn_id = getattr(connection, "connection_id", "unknown")
                        logger.warning(f"Timeout sending data to connection {conn_id}, marking inactive")
                        if hasattr(connection, "active"):
                            connection.active = False

            except ConnectionError as e:
                conn_id = getattr(connection, "connection_id", "unknown")
                logger.debug(f"Connection error for SSE connection {conn_id} (client likely disconnected): {e}")
                if hasattr(connection, "active"):
                    connection.active = False
            except RuntimeError as e:
                # Handle SSL protocol errors gracefully
                error_msg = str(e).lower()
                if "unable to perform operation" in error_msg or "handler is closed" in error_msg or "ssl" in error_msg:
                    conn_id = getattr(connection, "connection_id", "unknown")
                    logger.debug(f"SSL/transport error for connection {conn_id} (client disconnected): {e}")
                    if hasattr(connection, "active"):
                        connection.active = False
                else:
                    conn_id = getattr(connection, "connection_id", "unknown")
                    logger.error(f"Runtime error sending to local connection {conn_id}: {e}")
                    if hasattr(connection, "active"):
                        connection.active = False
            except Exception as e:
                # Check if it's an SSL error in the exception message
                error_msg = str(e).lower()
                conn_id = getattr(connection, "connection_id", "unknown")

                if "ssl" in error_msg or "handler is closed" in error_msg or "transport" in error_msg:
                    logger.debug(f"Transport/SSL error for connection {conn_id} (non-critical): {e}")
                else:
                    logger.error(f"Error sending to local connection {conn_id}: {e}")

                # Mark connection as inactive if sending fails
                if hasattr(connection, "active"):
                    connection.active = False

    async def cleanup_inactive_connections(self, max_age_minutes: int = 10):
        """Clean up inactive connections only for this instance"""
        try:
            if not await self._ensure_redis_connection():
                logger.error("Cannot cleanup connections: Redis not available")
                return

            # Only clean up connections from this instance
            instance_connections = await self.redis.smembers(self.instance_connections_key)

            cleaned = 0
            for connection_id in instance_connections:
                try:
                    conn_data = await self.redis.hgetall(f"{self.connections_key}:{connection_id}")
                    if conn_data:
                        # Verify this connection belongs to our instance
                        if conn_data.get("instance_id") != self.instance_id:
                            # Remove from our instance tracking if it doesn't belong to us
                            await self.redis.srem(self.instance_connections_key, connection_id)
                            continue

                        last_heartbeat = datetime.fromisoformat(conn_data.get("last_heartbeat", "1970-01-01"))

                        # Remove connections older than specified minutes
                        if datetime.now() - last_heartbeat > timedelta(minutes=max_age_minutes):
                            await self.unregister_connection(connection_id)
                            cleaned += 1
                    else:
                        # Connection data doesn't exist, remove from tracking
                        await self.redis.srem(self.instance_connections_key, connection_id)
                        if connection_id in self.local_connections:
                            del self.local_connections[connection_id]
                        cleaned += 1

                except Exception as cleanup_error:
                    logger.warning(f"Error cleaning up connection {connection_id}: {cleanup_error}")
                    # Remove from tracking anyway
                    try:
                        await self.redis.srem(self.instance_connections_key, connection_id)
                        cleaned += 1
                    except Exception as delete_error:
                        logger.warning(
                            f"Error removing connection from instance tracking {connection_id}: {delete_error}"
                        )

            if cleaned > 0:
                logger.debug(
                    f"Instance {self.instance_id}: Cleaned up {cleaned} inactive SSE connections "
                    f"(older than {max_age_minutes} minutes)"
                )

        except Exception as e:
            logger.error(f"Error cleaning up inactive connections for instance {self.instance_id}: {e}")

    async def get_debug_info(self) -> dict:
        """Get debug information about connections"""
        try:
            # Get all tenants
            tenant_keys = await self.redis.keys("sse:tenant:*")
            debug_info = {
                "instance_id": self.instance_id,
                "total_tenants": len(tenant_keys),
                "local_connections": len(self.local_connections),
                "total_connections": 0,
                "tenants": {},
            }

            total_connections = 0
            for tenant_key in tenant_keys:
                tenant_name = tenant_key.split(":")[-1]
                connections = await self.get_tenant_connections(tenant_name)
                total_connections += len(connections)

                debug_info["tenants"][tenant_name] = {
                    "total_connections": len(connections),
                    "unique_users": len(set(conn["user_id"] for conn in connections)),
                    "connections": connections,
                }

            debug_info["total_connections"] = total_connections
            return debug_info

        except Exception as e:
            logger.error(f"Error getting debug info: {e}")
            return {"error": str(e), "total_connections": 0}

    async def shutdown(self):
        """Cleanup on shutdown with improved error handling"""
        try:
            # Set shutdown flag to stop broadcast listener
            self._shutdown_requested = True

            # Wait a bit for the listener to stop gracefully
            if self._listener_active:
                logger.debug("Waiting for broadcast listener to stop...")
                for _ in range(10):  # Wait up to 1 second
                    if not self._listener_active:
                        break
                    await asyncio.sleep(0.1)

            # Unregister only our instance's connections
            if await self._ensure_redis_connection():
                instance_connections = await self.redis.smembers(self.instance_connections_key)
                for connection_id in instance_connections:
                    try:
                        await self.unregister_connection(connection_id)
                    except Exception as unreg_error:
                        logger.warning(f"Error unregistering connection {connection_id}: {unreg_error}")

                # Clean up instance tracking
                try:
                    await self.redis.delete(self.instance_connections_key)
                except Exception as instance_cleanup_error:
                    logger.warning(f"Error cleaning up instance tracking: {instance_cleanup_error}")

            # Close PubSub first
            if self.pubsub:
                try:
                    await self.pubsub.close()
                    self.pubsub = None
                except Exception as pubsub_error:
                    logger.warning(f"Error closing pubsub: {pubsub_error}")

            # Close Redis connection
            if self.redis:
                try:
                    await self.redis.close()
                    self.redis = None
                except Exception as redis_error:
                    logger.warning(f"Error closing Redis connection: {redis_error}")

            # Clear local connections
            self.local_connections.clear()

            logger.debug(f"Redis SSE Manager shutdown complete for instance {self.instance_id}")

        except Exception as e:
            logger.error(f"Error during shutdown for instance {self.instance_id}: {e}")
        finally:
            # Ensure connections are cleared even if errors occurred
            self.redis = None
            self.pubsub = None
            self.local_connections.clear()
            self._listener_active = False
            self._shutdown_requested = True


# Global singleton instance - Lazy initialization pattern
# Each Docker instance will have its own redis_sse_manager
# This is shared across all SSE connections within the same process
_redis_sse_manager_instance = None


def get_redis_sse_manager() -> RedisSSEManager:
    """
    Get the global RedisSSEManager singleton instance.
    Uses lazy initialization to ensure it's created in the correct asyncio context.
    """
    global _redis_sse_manager_instance
    if _redis_sse_manager_instance is None:
        _redis_sse_manager_instance = RedisSSEManager()
    return _redis_sse_manager_instance


# Module-level singleton
redis_sse_manager = get_redis_sse_manager()
