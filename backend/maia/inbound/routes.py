"""Inbound webhooks `/api/async/{platform}/{tenant}`: parse the platform payload, save the message, dispatch the reply (inline or ARQ)."""

import asyncio
import json
import logging
from os import getenv
from typing import Literal, Optional

from arq.connections import ArqRedis
from fastapi import APIRouter, File, Form, Path, Request, UploadFile
from fastapi.responses import PlainTextResponse
from starlette.requests import ClientDisconnect

from workers.pool import arq_pool_manager
from maia.inbound.reply import reply_to_message
from maia.inbound.save.email import save_email_message
from maia.inbound.save.waha import save_waha_input
from maia.inbound.save.web import save_web_input
from maia.models.tenants import TenantDocument

router = APIRouter()

logger = logging.getLogger("chat_routes")


HANDLER_PLATFORMS = {
    "email": save_email_message,
    "web": save_web_input,
    "waha": save_waha_input,
}


Platform = Literal["email", "web", "waha"]


@router.post("/{platform}/{tenant_name}", response_class=PlainTextResponse)
async def webhook_for_tenant(
    fast_request: Request,
    platform: Platform = Path(..., description="Messaging channel identifier."),
    tenant_name: str = Path(..., description="Tenant/tenant slug that owns the bot configuration."),
    ProfileName: Optional[str] = Form(
        default=None, description="Display name of the sender (optional form field)."
    ),
    Body: Optional[str] = Form(default=None, description="Plain-text content of the incoming message."),
    From: Optional[str] = Form(
        default=None, description="Sender address: phone number, email address, or platform-specific ID."
    ),
    To: Optional[str] = Form(default=None, description="Recipient address (the bot's phone number, email, etc.)."),
    agent: Optional[str] = Form(
        default=None,
        description="JSON-encoded agent override configuration. Allows selecting a specific agent at call time.",
    ),
    file: Optional[UploadFile] = File(None, description="Optional file attachment sent alongside the message."),
):
    """
    Receive an incoming message from a given platform and tenant, then asynchronously
    save and process it. Supports form-encoded payloads (the web channel) as well
    as JSON bodies (WAHA, email).

    Path parameters:
        platform   – Identifier of the messaging channel ("waha", "web", "email").
        tenant_name – Identifier of the tenant/tenant that owns the bot configuration.

    Optional form fields (platform-dependent):
        ProfileName – Display name of the sender (optional).
        Body        – Text content of the incoming message.
        From        – Sender address (phone number, email, etc.).
        To          – Recipient address.
        agent       – JSON-encoded agent override configuration.
        file        – Optional file attachment uploaded with the message.

    Always returns HTTP 200 to prevent webhook retry storms, even when an error occurs.
    """
    return await _handle_message(fast_request=fast_request, platform=platform, tenant_name=tenant_name, agent=agent)


@router.post("/{platform}", response_class=PlainTextResponse)
async def webhook_without_tenant(
    fast_request: Request,
    platform: Platform = Path(..., description="Messaging channel identifier."),
    ProfileName: Optional[str] = Form(
        default=None, description="Display name of the sender (optional form field)."
    ),
    Body: Optional[str] = Form(default=None, description="Plain-text content of the incoming message."),
    From: Optional[str] = Form(
        default=None, description="Sender address: phone number, email address, or platform-specific ID."
    ),
    To: Optional[str] = Form(default=None, description="Recipient address (the bot's phone number, email, etc.)."),
    agent: Optional[str] = Form(
        default=None,
        description="JSON-encoded agent override configuration. Allows selecting a specific agent at call time.",
    ),
    file: Optional[UploadFile] = File(None, description="Optional file attachment sent alongside the message."),
):
    """The same webhook without a tenant in the path (Gmail push posts here: the tenant is resolved from the mailbox).
    """
    return await _handle_message(fast_request=fast_request, platform=platform, tenant_name=None, agent=agent)


async def _handle_message(fast_request: Request, platform: str, tenant_name: Optional[str], agent: Optional[str]):
    """Read the body (form for web, JSON otherwise, 5 s timeout), save and dispatch the message, and always answer 200
    (270 on body timeout / disconnect, 271 on error) so the platform never retries.
    """
    request_start_time = asyncio.get_event_loop().time()

    try:
        # Parse request body with timeout to prevent hanging connections
        try:
            if platform == "web":
                request_body = await asyncio.wait_for(fast_request.form(), timeout=5.0)
            else:
                request_body = await asyncio.wait_for(fast_request.json(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error(f"Timeout reading request body for {platform}/{tenant_name}")
            return PlainTextResponse("", status_code=270)
        except ClientDisconnect:
            logger.error(f"Client disconnected while reading body for {platform}/{tenant_name}")
            return PlainTextResponse("", status_code=270)

        # Save the message, then generate the reply (inline or through ARQ)
        result = await process_message(platform=platform, tenant_name=tenant_name, agent=agent, request_body=request_body)

        # Log response time for monitoring
        response_time = (asyncio.get_event_loop().time() - request_start_time) * 1000
        if response_time > 1000:  # Log if taking more than 1 second
            pass
            # logger.error(
            #     f"Slow webhook response: {platform}/{tenant_name} took {response_time:.2f}ms"
            # )

        # Return 200 immediately to acknowledge receipt
        # For web platform, result might contain data
        return PlainTextResponse(result or "", status_code=200)

    except ClientDisconnect:
        # Handle ClientDisconnect at the top level too
        logger.error(f"Client disconnected for {platform}/{tenant_name}")
        return PlainTextResponse("", status_code=270)

    except Exception as e:
        # Log error but always return 200 to prevent webhook retry storms
        logger.error(
            f"Error in webhook_for_tenant: "
            f"Platform: {platform}, Tenant: {tenant_name} — {type(e).__name__}: {e}",
            exc_info=True,
        )

        # Critical: Always return 200 even on error to prevent webhook queue buildup
        return PlainTextResponse("", status_code=271)


async def dispatch_reply(
    pool: ArqRedis,
    tenant_name: str,
    sender_id: str,
    agent_config: Optional[dict],
    delay: int,
    should_await: bool,
    task_message_id: Optional[str] = None,
    single_message_mode: bool = False,
    conversation_id: Optional[str] = None,
) -> Optional[str]:
    """
    Enqueue ARQ task or await its result.

    Args:
        pool: ARQ Redis pool
        tenant_name: Tenant name
        sender_id: Sender/user ID
        agent_config: Optional agent configuration
        delay: Delay in seconds before processing
        should_await: If True, await the task result. If False, fire and forget.
        task_message_id: Optional ID of the specific message that triggered this task.
        single_message_mode: When True use message id as Redis session_id (isolated message).
        conversation_id: Optional conversation ID. If provided, used directly instead of get_or_create.

    Returns:
        Task result if should_await is True, None otherwise
    """
    try:
        job = await pool.enqueue_job(
            "reply_job",
            tenant_name,
            sender_id,
            agent_config,
            task_message_id,
            single_message_mode,
            conversation_id,
            _defer_by=delay if delay > 0 else None,
            _job_id=f"gen:{task_message_id}" if task_message_id else None,
        )
    except (ConnectionError, TimeoutError, OSError) as exc:
        # Redis connection died — reset the pool and retry once
        logger.error(f"ARQ enqueue failed ({type(exc).__name__}), resetting pool and retrying")
        pool = await arq_pool_manager.reset_pool()
        job = await pool.enqueue_job(
            "reply_job",
            tenant_name,
            sender_id,
            agent_config,
            task_message_id,
            single_message_mode,
            conversation_id,
            _defer_by=delay if delay > 0 else None,
            _job_id=f"gen:{task_message_id}" if task_message_id else None,
        )

    if should_await:
        # Wait for the task to complete and return result
        result = await job.result()  # you can add 30 second timeout
        return result

    # Fire and forget - just return None
    return None


async def process_message(
    platform: str, request_body: any, tenant_name: Optional[str] = None, agent: Optional[dict] = None
):
    """Save the inbound message with the platform's handler, then generate the reply: nothing when there is no one to
    answer (outgoing, blocked, manual mode); through ARQ when USE_ARQ_WORKER is set, inline otherwise.
    """
    save_response = await HANDLER_PLATFORMS[platform](request_body, tenant_name)
    sender_id = save_response.user_id
    delay = save_response.delay
    tenant_name = save_response.tenant_name
    task_message_id = save_response.task_message_id
    single_message_mode = save_response.single_message_mode
    conversation_id = save_response.conversation_id
    if not sender_id:
        #  For example is outgoing message or the user is blocked or in agent mode
        return

    # Manual mode: message is saved but bot generation is skipped (human takes over)
    if tenant_name:
        tenant = TenantDocument.objects(name=tenant_name).only("manual_mode").first()
        if tenant and tenant.manual_mode:
            return

    # Parse agent config if provided
    agent_config = None
    if agent:
        try:
            agent_config = json.loads(agent)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON format for agent: {agent}")

    if getenv("USE_ARQ_WORKER", False):
        # Get ARQ pool (singleton, reused across all requests)
        pool = await arq_pool_manager.get_pool()

        # Determine if we should wait for the response or fire and forget
        should_await = should_send_sync_response(platform, delay)

        # Use ARQ for all platforms
        return await dispatch_reply(
            pool=pool,
            tenant_name=tenant_name,
            sender_id=sender_id,
            agent_config=agent_config,
            delay=delay,
            should_await=should_await,
            task_message_id=task_message_id,
            single_message_mode=single_message_mode,
            conversation_id=conversation_id,
        )
    else:
        return await reply_to_message(
            tenant_name,
            sender_id,
            agent_config=agent_config,
            task_message_id=task_message_id,
            single_message_mode=single_message_mode,
            conversation_id=conversation_id,
        )


def should_send_sync_response(platform, delay):
    #  if platform is whatsapp always false
    # email is always fire-and-forget regardless of delay
    """Whether the webhook waits for the reply: web (and telephone) yes, email never, others only without a delay."""
    if platform == "email":
        return False
    return platform in ["web", "telephone"] or delay == 0
