"""Server-sent events for the inbox: a dashboard user subscribes to a tenant and receives conversation updates live."""

import asyncio
import json
import logging
from datetime import datetime

import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from maia.models.conversations import Conversation
from maia.models.messages import MessageDocument
from maia.config import ALGORITHM, JWT_SECRET

from maia.api.sse_manager import redis_sse_manager

logger = logging.getLogger(__name__)

sse_router = APIRouter(prefix="/sse", tags=["Server-Sent Events"])


# Custom JSON encoder for datetime objects
class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder for SSE payloads: datetimes as ISO strings, other objects through their dict or str."""
    def default(self, obj):
        """Serialize a value json cannot handle (datetime, objects, iterables)."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        # Handle other non-serializable objects
        elif hasattr(obj, "__dict__"):
            return obj.__dict__
        elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
            try:
                return list(obj)
            except (TypeError, ValueError):
                return str(obj)
        return str(obj)  # Fallback to string representation


class SSEConnection:
    """One browser subscription: an asyncio queue the generator drains, the filters of the inbox view, and the Redis
    registration.
    """
    def __init__(self, tenant_name: str, user_id: str, filters: dict = None):
        """Create the queue and a unique connection id (tenant:user:timestamp)."""
        self.tenant_name = tenant_name
        self.user_id = user_id
        self.filters = filters or {}
        self.queue = asyncio.Queue()
        self.active = True
        self.disconnect_task = None
        # Add timestamp to make connection_id unique for multiple connections per user
        import time

        timestamp = int(time.time() * 1000)  # milliseconds for uniqueness
        self.connection_id = f"{tenant_name}:{user_id}:{timestamp}"

    async def save_to_db(self):
        """Save connection in Redis and register locally"""
        try:
            # Register in Redis with filters
            await redis_sse_manager.register_connection(self.connection_id, self.tenant_name, self.user_id, self.filters)

            # Register locally for direct message delivery
            redis_sse_manager.local_connections[self.connection_id] = self

            # logger.info(
            #     f"Created new Redis SSE connection: {self.connection_id} with filters: {self.filters}"
            # )

        except Exception as e:
            logger.error(f"Error saving SSE connection: {e}")

    async def cleanup(self):
        """Clean up connection resources"""
        self.active = False

        # Cancel disconnect task if it exists
        if self.disconnect_task and not self.disconnect_task.done():
            self.disconnect_task.cancel()
            try:
                await self.disconnect_task
            except asyncio.CancelledError:
                pass

        # Clear the queue to prevent memory leaks
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                break

        # Unregister from Redis and local
        try:
            await redis_sse_manager.unregister_connection(self.connection_id)
            # logger.info(f"SSE connection {self.connection_id} cleaned up")
        except Exception as e:
            logger.error(f"Error cleaning up SSE connection: {e}")

    async def update_heartbeat(self):
        """Update heartbeat timestamp in Redis"""
        try:
            await redis_sse_manager.update_heartbeat(self.connection_id)
        except Exception as e:
            logger.error(f"Error updating heartbeat for connection {self.connection_id}: {e}")

    async def send_data(self, data: dict):
        """Queue a payload for the client and refresh the heartbeat; a failure deactivates the connection."""
        if self.active:
            try:
                await self.queue.put(data)
                await self.update_heartbeat()
            except Exception as e:
                logger.error(f"Error sending data to SSE connection: {e}")
                self.active = False

    async def generator(self):
        """Async generator of the SSE stream: yields the queued payloads as data: lines, and a heartbeat after 25 s of
        silence (reverse proxies drop idle streams, typically after about 100 s).
        """
        try:
            while self.active:
                try:
                    # Wait for data with timeout to send heartbeat
                    # Reverse proxies drop idle SSE connections (typically after ~100 s),
                    # so we send a heartbeat every 25s to stay well within that limit.
                    data = await asyncio.wait_for(self.queue.get(), timeout=25.0)
                    if self.active and data:  # Check again after getting data and validate data
                        try:
                            # Validate and clean data before JSON serialization
                            if isinstance(data, dict):
                                # Ensure all required fields exist and are valid
                                cleaned_data = self._clean_data_for_json(data)
                                json_data = json.dumps(
                                    cleaned_data,
                                    cls=DateTimeEncoder,
                                    ensure_ascii=False,
                                )

                                # Validate that the JSON is properly formed
                                json.loads(json_data)  # This will raise if invalid

                                yield f"data: {json_data}\n\n"
                            else:
                                logger.warning(f"Invalid data type for SSE: {type(data)}, data: {data}")
                        except (
                            TypeError,
                            ValueError,
                            json.JSONDecodeError,
                        ) as json_error:
                            logger.error(f"Error serializing SSE data: {json_error}, data type: {type(data)}")
                            # Send error notification instead of broken data
                            error_data = {
                                "type": "error",
                                "message": "Data serialization error",
                                "timestamp": datetime.now().isoformat(),
                            }
                            try:
                                json_data = json.dumps(error_data, cls=DateTimeEncoder)
                                yield f"data: {json_data}\n\n"
                            except Exception:
                                logger.error("Failed to send error notification via SSE")
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    if self.active:
                        try:
                            heartbeat_data = {
                                "type": "heartbeat",
                                "timestamp": datetime.now().isoformat(),
                            }
                            yield f"data: {json.dumps(heartbeat_data, cls=DateTimeEncoder)}\n\n"
                            await self.update_heartbeat()
                        except Exception as heartbeat_error:
                            logger.error(f"Error sending heartbeat: {heartbeat_error}")
                            # If we can't send heartbeat, connection might be broken
                            self.active = False
                            break
                except asyncio.CancelledError:
                    logger.info("SSE generator cancelled")
                    break
                except Exception as e:
                    logger.error(f"SSE generator error: {e}")
                    # For unexpected errors, mark connection as inactive
                    self.active = False
                    break
        except Exception as e:
            logger.error(f"SSE generator critical error: {e}")
        finally:
            # Ensure cleanup happens
            if self.active:
                await self.cleanup()

    def _clean_data_for_json(self, data: dict) -> dict:
        """Clean data to ensure JSON serialization compatibility"""
        if not isinstance(data, dict):
            return {"type": "error", "message": "Invalid data format"}

        cleaned = {}
        for key, value in data.items():
            try:
                # Test if the value can be JSON serialized
                json.dumps(value, cls=DateTimeEncoder)
                cleaned[key] = value
            except (TypeError, ValueError):
                # Replace non-serializable values with string representation
                cleaned[key] = str(value) if value is not None else None
                # logger.info(
                #     f"Replaced non-serializable value for key '{key}': {type(value)}"
                # )

        return cleaned


@sse_router.get("/{tenant_name}/conversations")
async def conversations_sse(
    tenant_name: str,
    request: Request,
    token: str = Query(..., description="JWT token for authentication"),
    # Filter parameters
    platform: str = Query(None, description="Filter by platform"),
    conversation_status: str = Query(None, description="Filter by conversation status"),
    assigned_to: str = Query(None, description="Filter by assigned user"),
    read_by_manager: bool = Query(None, description="Filter by read status"),
    resolution_status: str = Query(None, description="Filter by resolution status"),
    agent_required: bool = Query(None, description="Filter by agent required"),
    phone_number: str = Query(None, description="Filter by phone number"),
    query: str = Query(None, description="Search query for filtering"),
    include_tools_call: bool = Query(False, description="Include tools_call messages or only user/assistant/manager"),
):
    """
    Server-Sent Events endpoint for real-time conversation updates.
    Sends conversation changes in real-time to connected clients.
    Now supports filtering conversations based on provided filter parameters.
    """

    # Authenticate user via token
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
    except Exception:
        # logger.error(f"SSE authentication failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")

    # Allow multiple connections per user - no need to cleanup existing ones
    # logger.info(
    #     f"Creating new SSE connection for user {user_id} in tenant {tenant_name} "
    #     f"(instance: {redis_sse_manager.instance_id})"
    # )

    # Collect filters from request parameters
    connection_filters = {}
    if platform:
        connection_filters["platform"] = platform
    if conversation_status:
        connection_filters["conversation_status"] = conversation_status
    if assigned_to:
        connection_filters["assigned_to"] = assigned_to
    if read_by_manager is not None:
        connection_filters["read_by_manager"] = read_by_manager
    if resolution_status:
        connection_filters["resolution_status"] = resolution_status
    if agent_required is not None:
        connection_filters["agent_required"] = agent_required
    if phone_number:
        connection_filters["phone_number"] = phone_number
    if query:
        connection_filters["query"] = query
    if include_tools_call is not None:
        connection_filters["include_tools_call"] = include_tools_call

    # Create SSE connection (allows multiple per user) with filters
    connection = SSEConnection(tenant_name, str(user_id), connection_filters)
    await connection.save_to_db()

    # Count existing connections for this user (for logging)
    try:
        connections = await redis_sse_manager.get_tenant_connections(tenant_name)
        logger.info(
            f"User {user_id} now has {len(connections)} active connections in tenant {tenant_name} "
            f"(instance: {redis_sse_manager.instance_id})"
        )
        # user_connections = [conn for conn in connections if conn["user_id"] == user_id]
        # logger.info(
        #     f"User {user_id} now has {len(user_connections)} active connections in tenant {tenant_name} "
        #     f"(instance: {redis_sse_manager.instance_id})"
        # )
    except Exception as e:
        logger.info(f"Could not count existing connections: {e}")

    # logger.info(
    #     f"SSE connection established for user {user_id} in tenant {tenant_name} "
    #     f"(connection_id: {connection.connection_id}, instance: {redis_sse_manager.instance_id})"
    # )

    # Send initial connection message
    await connection.send_data(
        {
            "type": "connected",
            "tenant_name": tenant_name,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            "connection_id": connection.connection_id,  # Include unique connection ID
            "instance_id": redis_sse_manager.instance_id,  # Include instance ID for debugging
            "filters": connection_filters,  # Include applied filters for debugging
        }
    )

    # Handle client disconnect
    async def check_disconnect():
        """Poll the request every 5 s; when the client is gone, deactivate the connection and unregister it from
        Redis.
        """
        try:
            while connection.active:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Disconnect checker error: {e}")
        finally:
            # Only do the actual resource cleanup here (single place, no recursion)
            if connection.active:
                connection.active = False
                # Don't call connection.cleanup() — it would try to cancel THIS task.
                # Instead, do the cleanup steps directly:
                while not connection.queue.empty():
                    try:
                        connection.queue.get_nowait()
                        connection.queue.task_done()
                    except (asyncio.QueueEmpty, ValueError):
                        break
                try:
                    await redis_sse_manager.unregister_connection(connection.connection_id)
                except Exception as e:
                    logger.error(f"Error cleaning up SSE connection: {e}")

    # Start disconnect checker and store reference
    connection.disconnect_task = asyncio.create_task(check_disconnect())

    try:
        return StreamingResponse(
            connection.generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Cache-Control",
            },
        )
    except Exception as e:
        logger.error(f"SSE endpoint error: {e}")
        if connection.active:
            connection.active = False
            while not connection.queue.empty():
                try:
                    connection.queue.get_nowait()
                    connection.queue.task_done()
                except (asyncio.QueueEmpty, ValueError):
                    break
            try:
                await redis_sse_manager.unregister_connection(connection.connection_id)
            except Exception as cleanup_err:
                logger.error(f"Error cleaning up SSE connection: {cleanup_err}")
            if connection.disconnect_task and not connection.disconnect_task.done():
                connection.disconnect_task.cancel()
        raise


async def broadcast_conversation_update(tenant_name: str, update_data: dict):
    """
    Broadcast conversation updates to all connected clients in a tenant using Redis.
    This function can be called from any context (main app or Celery workers).
    Multi-instance safe implementation using singleton pattern.
    """
    try:
        # Validate input data
        if not tenant_name or not update_data:
            logger.warning("Invalid broadcast parameters: tenant_name or update_data is empty")
            return

        # Use global singleton manager
        manager = redis_sse_manager

        # Try to use existing global manager first
        if await manager.is_healthy():
            pass  # Manager is healthy, continue
        else:
            # Try to reinitialize the global manager
            try:
                await manager.initialize()
                if not await manager.is_healthy():
                    logger.error("Failed to reinitialize global Redis SSE manager")
                    return
            except Exception as reinit_error:
                logger.warning(f"Failed to reinitialize global manager: {reinit_error}")

        # If global manager still unhealthy, log error and return
        # Don't create temporary managers - use singleton pattern
        if not await manager.is_healthy():
            logger.error("Cannot broadcast: Redis SSE manager unavailable after reinitialization attempt")
            return

        # Perform the broadcast with timeout handling
        try:
            await asyncio.wait_for(
                manager.broadcast_to_tenant(tenant_name, update_data),
                timeout=15.0,  # Overall timeout for the broadcast operation
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Broadcast to tenant {tenant_name} timed out after 15 seconds, continuing without blocking the operation"
            )
        except Exception as broadcast_error:
            logger.error(f"Failed to broadcast to tenant {tenant_name}: {broadcast_error}")
            # Don't re-raise - we want the operation to continue even if broadcast fails

        # Get connection count for logging (non-blocking and safe)
        try:
            connections = await manager.get_tenant_connections(tenant_name)
            unique_users = len(set(conn["user_id"] for conn in connections)) if connections else 0

            logger.info(
                f"SSE Broadcast (instance {manager.instance_id}): "
                f"tenant {tenant_name}, connections: {len(connections)} ({unique_users} unique users), "
                f"type: {update_data.get('type', 'unknown')}"
            )
        except Exception as conn_error:
            logger.warning(f"Could not get connection count for logging: {conn_error}")
            # logger.info(
            #     f"SSE Broadcast (instance {manager.instance_id}): "
            #     f"tenant {tenant_name}, type: {update_data.get('type', 'unknown')}"
            # )

    except Exception as e:
        logger.error(f"Error broadcasting conversation update: {e}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")


# NOTE: Periodic cleanup is now handled by Celery Beat (background_tasks.cleanup_sse_connections_task)
# This reduces CPU usage by not having a permanent asyncio loop running


async def start_cleanup_task():
    """Initialize Redis SSE Manager (cleanup now handled by Celery Beat)"""
    await redis_sse_manager.initialize()


async def stop_cleanup_task():
    """Shutdown Redis SSE Manager"""
    await redis_sse_manager.shutdown()


def _safe_to_dict(obj, obj_type: str):
    """
    Safely convert an object to dict with error handling
    """
    try:
        if hasattr(obj, "to_dict_with_customer"):
            return obj.to_dict_with_customer()
        elif hasattr(obj, "to_dict"):
            return obj.to_dict()
        elif hasattr(obj, "__dict__"):
            # Convert object attributes to dict
            result = {}
            for key, value in obj.__dict__.items():
                if not key.startswith("_"):  # Skip private attributes
                    try:
                        # Test if value is JSON serializable
                        json.dumps(value, cls=DateTimeEncoder)
                        result[key] = value
                    except (TypeError, ValueError):
                        result[key] = str(value) if value is not None else None
            return result
        else:
            logger.warning(f"Object {obj_type} has no serialization method")
            return {"error": f"No serialization method for {obj_type}"}
    except Exception as e:
        logger.error(f"Error serializing {obj_type}: {e}")
        return {"error": f"Serialization failed for {obj_type}: {str(e)}"}


def _filter_messages_by_role(conversation_dict: dict, include_tools_call: bool) -> dict:
    """
    Filter messages in conversation based on include_tools_call parameter.
    If include_tools_call is False, only include messages with role: user, assistant, or manager.
    If include_tools_call is True, include all messages.
    """
    if include_tools_call or not conversation_dict.get("messages"):
        return conversation_dict

    # Allowed roles when include_tools_call is False
    allowed_roles = {"user", "assistant", "manager"}

    # Filter messages
    filtered_messages = []
    for message in conversation_dict["messages"]:
        if isinstance(message, dict):
            message_role = message.get("role", "").lower()
            if message_role in allowed_roles:
                filtered_messages.append(message)
        else:
            # If message is not a dict, include it (fallback)
            filtered_messages.append(message)

    # Update conversation with filtered messages
    filtered_conversation = conversation_dict.copy()
    filtered_conversation["messages"] = filtered_messages

    return filtered_conversation


def _conversation_matches_filters(conversation_dict: dict, filters: dict) -> bool:
    """
    Check if a conversation matches the provided filters.
    Returns True if the conversation should be sent to the client, False otherwise.
    """
    if not filters:
        return True  # No filters, send all conversations

    try:

        # Platform filter
        if filters.get("platform") and conversation_dict.get("platform") != filters["platform"]:
            # print(
            #     f"Conversation {conv_id} filtered out by platform: "
            #     f"{conversation_dict.get('platform')} != {filters['platform']}"
            # )
            return False

        # Conversation status filter
        conv_status = conversation_dict.get("conversation_status")
        if filters.get("conversation_status") and conv_status != filters["conversation_status"]:
            # print(
            #     f"Conversation {conv_id} filtered out by status: "
            #     f"{conv_status} != {filters['conversation_status']}"
            # )
            return False

        # Assigned to filter
        if filters.get("assigned_to"):
            # print(
            #     f"Conversation {conv_id} is assigned to: {conversation_dict.get('assigned_to')}"
            # )  # because if the conv is taked by another user, it should not be filtered out
            if filters["assigned_to"] == "unassigned":
                # Check if conversation is unassigned (null or empty)
                assigned_to = conversation_dict.get("assigned_to")
                if assigned_to and assigned_to.strip():
                    # print(f"Conversation {conv_id} filtered out by assigned_to: not unassigned")
                    return False
            else:
                # Check if conversation is assigned to specific user
                conv_assigned = conversation_dict.get("assigned_to")
                if conv_assigned != filters["assigned_to"]:
                    # print(f"Conversation {conv_id} filtered out by assigned_to: "
                    #              f"{conv_assigned} != {filters['assigned_to']}")
                    return False

        # Read by manager filter
        if filters.get("read_by_manager") is not None:
            conv_read = conversation_dict.get("read_by_manager")
            if conv_read != filters["read_by_manager"]:
                # print(
                #     f"Conversation {conv_id} filtered out by read_by_manager: "
                #     f"{conv_read} != {filters['read_by_manager']}"
                # )
                return False


        # Agent required filter
        if filters.get("agent_required") is not None:
            conv_agent_req = conversation_dict.get("agent_required")
            if conv_agent_req != filters["agent_required"]:
                # print(
                #     f"Conversation {conv_id} filtered out by agent_required: "
                #     f"{conv_agent_req} != {filters['agent_required']}"
                # )
                return False

        # Phone number filter
        if filters.get("phone_number"):
            phone_filter = filters["phone_number"].lower()
            conversation_phone = conversation_dict.get("phone_number", "").lower()
            if phone_filter not in conversation_phone:
                # print(
                #     f"Conversation {conv_id} filtered out by phone_number: "
                #     f"{conversation_phone} doesn't contain {phone_filter}"
                # )
                return False

        # Query filter (search in messages or phone numbers)
        if filters.get("query") and filters["query"].strip():
            query = filters["query"].lower()
            phone_match = query in conversation_dict.get("phone_number", "").lower()

            # Check customer name if available
            customer_name_match = False
            customer = conversation_dict.get("customer")
            if customer and customer.get("name"):
                customer_name_match = query in customer["name"].lower()

            # Check messages
            message_match = False
            if conversation_dict.get("messages"):
                for message in conversation_dict["messages"]:
                    if isinstance(message, dict) and message.get("message"):
                        if query in message["message"].lower():
                            message_match = True
                            break

            if not (phone_match or customer_name_match or message_match):
                # print(
                #     f"Conversation {conv_id} filtered out by query: '{query}' not found"
                # )
                return False

        # print(f"Conversation {conv_id} passed all filters")
        return True

    except Exception as e:
        logger.error(f"Error checking conversation filters: {e}")
        return True  # On error, allow the conversation through


async def broadcast_new_message(message_doc: MessageDocument):
    """
    Broadcast new message to all connected clients in the tenant.
    Called when a new message is created.
    """
    try:
        if not message_doc or not message_doc.tenant_name:
            logger.warning("Invalid message document for SSE broadcast")
            return

        tenant_name = message_doc.tenant_name

        # Get conversation data with customer
        conversation = None
        try:
            conversation = Conversation.objects(id=message_doc.conversation_id).first()

            # If no conversation found, try to find any conversation for this phone number
            if not conversation:
                conversation = (
                    Conversation.objects(
                        tenant_name=tenant_name,
                        phone_number=message_doc.phone_number,
                    )
                    .order_by("-last_message_date")
                    .first()
                )

            if conversation:
                # Prepare update data with enhanced error handling
                try:
                    # Safely convert to dict with validation
                    conversation_dict = _safe_to_dict(conversation, "conversation")
                    message_dict = _safe_to_dict(message_doc, "message")

                    # Validate critical fields
                    if not conversation_dict or not message_dict:
                        raise ValueError("Failed to serialize conversation or message data")

                    update_data = {
                        "type": "message_update",
                        "conversation": conversation_dict,
                        "message": message_dict,
                        "timestamp": datetime.now().isoformat(),
                    }

                    # Test JSON serialization before broadcasting
                    test_json = json.dumps(update_data, cls=DateTimeEncoder)
                    json.loads(test_json)  # Validate it can be parsed back

                    await broadcast_conversation_update(tenant_name, update_data)
                    # logger.info(
                    #     f"SSE: Successfully broadcasted message update for tenant {tenant_name}, "
                    #     f"phone {message_doc.phone_number}"
                    # )
                except Exception as dict_error:
                    logger.error(f"Error converting objects to dict: {dict_error}")
                    # Fallback with minimal data
                    try:
                        # Create safe fallback data
                        conv_id = "unknown"
                        if hasattr(conversation, "id") and conversation.id:
                            conv_id = str(conversation.id)

                        msg_id = "unknown"
                        if hasattr(message_doc, "id") and message_doc.id:
                            msg_id = str(message_doc.id)

                        msg_text = getattr(message_doc, "message", "")
                        if len(msg_text) > 100:
                            msg_text = msg_text[:100] + "..."

                        update_data = {
                            "type": "message_update",
                            "conversation": {
                                "id": conv_id,
                                "phone_number": getattr(conversation, "phone_number", "unknown"),
                                "tenant_name": tenant_name,
                            },
                            "message": {
                                "id": msg_id,
                                "phone_number": getattr(message_doc, "phone_number", "unknown"),
                                "message": msg_text,
                                "role": getattr(message_doc, "role", "unknown"),
                            },
                            "timestamp": datetime.now().isoformat(),
                            "error": "Data serialization fallback used",
                        }

                        # Test fallback JSON
                        test_json = json.dumps(update_data, cls=DateTimeEncoder)
                        json.loads(test_json)

                        await broadcast_conversation_update(tenant_name, update_data)
                        logger.warning(f"SSE: Used fallback data for tenant {tenant_name}")
                    except Exception as fallback_error:
                        logger.error(f"Even fallback data failed: {fallback_error}")
            else:
                logger.info(f"SSE: No conversation found for tenant {tenant_name}, phone {message_doc.phone_number}")

        except Exception as conv_error:
            logger.error(f"Error processing conversation for SSE: {conv_error}")

    except Exception as e:
        logger.error(f"SSE: Error broadcasting message: {e}")
        import traceback

        logger.error(f"SSE broadcast error traceback: {traceback.format_exc()}")


async def broadcast_conversation_status_change(conversation: Conversation):
    """
    Broadcast conversation status changes (read, assigned, etc.).
    Normalized to work exactly like broadcast_new_message with full conversation reload.
    """
    try:
        if not conversation or not conversation.tenant_name:
            logger.warning("Invalid conversation for SSE broadcast")
            return

        tenant_name = conversation.tenant_name

        # Reload conversation from database to ensure we have the latest data with customer
        try:
            # Reload the conversation to get the most up-to-date data
            conversation = Conversation.objects(id=conversation.id).first()

            if conversation:
                # Prepare update data with enhanced error handling
                try:
                    # Safely convert to dict with validation
                    conversation_dict = _safe_to_dict(conversation, "conversation")

                    # Validate critical fields
                    if not conversation_dict:
                        raise ValueError("Failed to serialize conversation data")

                    update_data = {
                        "type": "message_update",  # Use message_update like broadcast_new_message
                        "conversation": conversation_dict,
                        "message": None,  # No specific message for status changes
                        "timestamp": datetime.now().isoformat(),
                        "status_change": True,  # Flag to indicate this is a status change
                    }

                    # Test JSON serialization before broadcasting
                    test_json = json.dumps(update_data, cls=DateTimeEncoder)
                    json.loads(test_json)  # Validate it can be parsed back

                    await broadcast_conversation_update(tenant_name, update_data)
                    # logger.info(
                    #     f"SSE: Successfully broadcasted status change for tenant {tenant_name}, "
                    #     f"conversation {conversation.id}"
                    # )
                except Exception as dict_error:
                    logger.error(f"Error converting conversation to dict: {dict_error}")
                    # Fallback with minimal data
                    try:
                        conv_id = str(conversation.id) if conversation.id else "unknown"
                        phone_number = getattr(conversation, "phone_number", "unknown")

                        update_data = {
                            "type": "message_update",
                            "conversation": {
                                "id": conv_id,
                                "phone_number": phone_number,
                                "tenant_name": tenant_name,
                            },
                            "message": None,
                            "timestamp": datetime.now().isoformat(),
                            "status_change": True,
                            "error": "Data serialization fallback used",
                        }

                        # Test fallback JSON
                        test_json = json.dumps(update_data, cls=DateTimeEncoder)
                        json.loads(test_json)

                        await broadcast_conversation_update(tenant_name, update_data)
                        logger.warning(f"SSE: Used fallback data for status change in tenant {tenant_name}")
                    except Exception as fallback_error:
                        logger.error(f"Even fallback data failed for status change: {fallback_error}")
            else:
                logger.warning("SSE: Could not reload conversation for status change broadcast")

        except Exception as conv_error:
            logger.error(f"Error processing conversation for SSE status change: {conv_error}")

    except Exception as e:
        logger.error(f"SSE: Error broadcasting conversation status change: {e}")
        import traceback

        logger.error(f"SSE status change broadcast error traceback: {traceback.format_exc()}")


async def cleanup_all_connections():
    """
    Clean up all active SSE connections. Called on application shutdown.
    """
    try:
        await redis_sse_manager.shutdown()
        # logger.info("All SSE connections cleaned up via Redis manager")
    except Exception as e:
        logger.error(f"Error cleaning up all connections during shutdown: {e}")
