"""Inbound webhooks `/api/async/{platform}/{tenant}`: parse the platform payload, save the message, dispatch the reply (inline or ARQ)."""

import asyncio
import json
import logging
import time
from os import getenv
from typing import Dict, Literal, Optional, Tuple

import httpx
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

# ---------------------------------------------------------------------------
# Dev-mode forwarding helpers
# ---------------------------------------------------------------------------
# Simple in-memory TTL cache: { tenant_name: (dev_url | None, timestamp) }
# Avoids a MongoDB round-trip on every incoming webhook.
_dev_url_cache: Dict[str, Tuple[Optional[str], float]] = {}
_DEV_URL_CACHE_TTL = 60.0  # seconds

logger = logging.getLogger("chat_routes")


def _get_dev_url_for_tenant_sync(tenant_name: str) -> Optional[str]:
    """
    Return the dev_url if the tenant status is 'development', else None.
    Result is cached for _DEV_URL_CACHE_TTL seconds to minimise DB hits.
    """
    now = time.monotonic()
    cached = _dev_url_cache.get(tenant_name)
    if cached is not None:
        dev_url, ts = cached
        if now - ts < _DEV_URL_CACHE_TTL:
            return dev_url

    try:
        tenant = TenantDocument.objects(name=tenant_name).only("status", "dev_url").first()
        dev_url = tenant.dev_url if (tenant and tenant.status in ("development", "developer") and tenant.dev_url) else None
    except Exception as exc:
        logger.error(f"[DEV] Error fetching dev_url for tenant '{tenant_name}': {exc}")
        dev_url = None

    _dev_url_cache[tenant_name] = (dev_url, now)
    return dev_url


async def _forward_to_dev_server(
    dev_url: str, platform: str, tenant_name: str, body: bytes, content_type: str
) -> Optional[str]:
    """
    Forward the raw webhook request to the dev server at the same path.
    The dev server will save the message to its own DB and run generation
    with its own code, exactly like a normal incoming webhook.
    """
    target_url = f"{dev_url.rstrip('/')}/api/async/{platform}/{tenant_name}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(target_url, content=body, headers={"content-type": content_type})
            return response.text
    except httpx.ConnectError:
        logger.error(f"[DEV] Cannot connect to dev server at {target_url}")
    except httpx.TimeoutException:
        logger.error(f"[DEV] Timeout calling dev server at {target_url}")
    except Exception as exc:
        logger.error(f"[DEV] Unexpected error calling {target_url}: {exc}")
    return None


# ---------------------------------------------------------------------------


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
    threadId: Optional[str] = Form(
        default=None,
        description="Thread or conversation identifier used to continue an existing conversation (platform-dependent).",
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
        threadId    – Thread/conversation identifier (used by some platforms).
        file        – Optional file attachment uploaded with the message.

    Always returns HTTP 200 to prevent webhook retry storms, even when an error occurs.
    In production, if the target tenant is in development mode, the request is transparently
    forwarded to the configured dev server instead of being processed locally.
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
    return await _handle_message(fast_request=fast_request, platform=platform, tenant_name=None, agent=agent)


async def _handle_message(fast_request: Request, platform: str, tenant_name: Optional[str], agent: Optional[str]):
    request_start_time = asyncio.get_event_loop().time()

    try:
        # --------------------------------------------------------------
        # Dev-mode: forward the entire raw request to the dev server
        # so it saves the message and generates with its own code.
        # Only active in PRODUCTION environment.
        # --------------------------------------------------------------
        if tenant_name and getenv("MAIA_ENV", "DEVELOPMENT") == "PRODUCTION":
            dev_url = _get_dev_url_for_tenant_sync(tenant_name)
            if dev_url:
                body = await fast_request.body()
                content_type = fast_request.headers.get("content-type", "")
                should_await = platform in ("web", "telephone")
                if should_await:
                    result = await _forward_to_dev_server(dev_url, platform, tenant_name, body, content_type)
                    return PlainTextResponse(result or "", status_code=200)
                else:
                    asyncio.ensure_future(_forward_to_dev_server(dev_url, platform, tenant_name, body, content_type))
                    return PlainTextResponse("", status_code=200)

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

        # Process message — dev routing happens inside process_message, after save
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
    if platform == "email":
        return False
    return (
        platform in ["web", "telephone"] or delay == 0
        # or getenv("ENV", "DEVELOPMENT") != "PRODUCTION"
    )
