"""Inbound webhooks `/api/async/{platform}/{tenant}`: parse the platform payload, save the message, dispatch the reply (inline or ARQ)."""

import asyncio
import logging
from os import getenv
from typing import Literal, Optional

from arq.connections import ArqRedis
from fastapi import APIRouter, File, Form, Path, Request, UploadFile
from fastapi.responses import PlainTextResponse
from starlette.requests import ClientDisconnect

from workers.pool import arq_redis_pool_manager
from maia.inbound.reply import run_reply_pipeline
from maia.inbound.save.common import SaveResponse
from maia.inbound.save.email import save_and_answer_email_messages
from maia.inbound.save.waha import save_waha_message
from maia.inbound.save.web import save_web_message
from maia.models.tenants import TenantDocument

router = APIRouter()

logger = logging.getLogger("chat_routes")


SAVE_HANDLERS_BY_PLATFORM = {
    "email": save_and_answer_email_messages,
    "web": save_web_message,
    "waha": save_waha_message,
}


InboundPlatformName = Literal["email", "web", "waha"]


@router.post("/{platform}/{tenant_name}", response_class=PlainTextResponse)
async def receive_inbound_message_for_tenant(
    http_request: Request,
    platform: InboundPlatformName = Path(..., description="Messaging channel identifier."),
    tenant_name: str = Path(..., description="Tenant/tenant slug that owns the bot configuration."),
    ProfileName: Optional[str] = Form(
        default=None, description="Display name of the sender (optional form field)."
    ),
    Body: Optional[str] = Form(default=None, description="Plain-text content of the incoming message."),
    From: Optional[str] = Form(
        default=None, description="Sender address: phone number, email address, or platform-specific ID."
    ),
    To: Optional[str] = Form(default=None, description="Recipient address (the bot's phone number, email, etc.)."),
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
        file        – Optional file attachment uploaded with the message.

    Always returns HTTP 200 to prevent webhook retry storms, even when an error occurs.
    """
    return await _serve_inbound_webhook(http_request=http_request, platform=platform, tenant_name=tenant_name)


@router.post("/{platform}", response_class=PlainTextResponse)
async def receive_inbound_message_without_tenant(
    http_request: Request,
    platform: InboundPlatformName = Path(..., description="Messaging channel identifier."),
    ProfileName: Optional[str] = Form(
        default=None, description="Display name of the sender (optional form field)."
    ),
    Body: Optional[str] = Form(default=None, description="Plain-text content of the incoming message."),
    From: Optional[str] = Form(
        default=None, description="Sender address: phone number, email address, or platform-specific ID."
    ),
    To: Optional[str] = Form(default=None, description="Recipient address (the bot's phone number, email, etc.)."),
    file: Optional[UploadFile] = File(None, description="Optional file attachment sent alongside the message."),
):
    """The same webhook without a tenant in the path (Gmail push posts here: the tenant is resolved from the mailbox).
    """
    return await _serve_inbound_webhook(http_request=http_request, platform=platform, tenant_name=None)


def _acknowledge_webhook_error(status_code: int, log_message: str, exc_info: bool = False) -> PlainTextResponse:
    """Log the error and answer the platform with an empty 27x response: a deliberate acknowledgement, so the platform
    never retries. Called inside an except clause, so exc_info=True logs the exception being handled.
    """
    logger.error(log_message, exc_info=exc_info)
    return PlainTextResponse("", status_code=status_code)


async def _read_webhook_payload(http_request: Request, platform: str):
    """The parsed webhook body: the form for the web platform, the JSON value otherwise, each awaited with a 5 s
    timeout. asyncio.TimeoutError and ClientDisconnect propagate to the caller, which answers 270.
    """
    if platform == "web":
        return await asyncio.wait_for(http_request.form(), timeout=5.0)
    return await asyncio.wait_for(http_request.json(), timeout=5.0)


async def _serve_inbound_webhook(http_request: Request, platform: str, tenant_name: Optional[str]):
    """Read the body (form for web, JSON otherwise, 5 s timeout), save and dispatch the message, and always answer 200
    (270 on body timeout / disconnect, 271 on error) so the platform never retries.
    """
    try:
        # Parse request body with timeout to prevent hanging connections
        try:
            webhook_payload = await _read_webhook_payload(http_request, platform)
        except asyncio.TimeoutError:
            return _acknowledge_webhook_error(270, f"Timeout reading request body for {platform}/{tenant_name}")
        except ClientDisconnect:
            return _acknowledge_webhook_error(
                270, f"Client disconnected while reading body for {platform}/{tenant_name}"
            )

        # Save the message, then generate the reply (inline or through ARQ)
        reply_payload = await save_message_and_dispatch_reply(
            platform=platform, tenant_name=tenant_name, webhook_payload=webhook_payload
        )

        # Return 200 immediately to acknowledge receipt
        # For web platform, result might contain data
        return PlainTextResponse(reply_payload or "", status_code=200)

    except ClientDisconnect:
        # Handle ClientDisconnect at the top level too
        return _acknowledge_webhook_error(270, f"Client disconnected for {platform}/{tenant_name}")

    except Exception as error:
        # Log error but always return 200 to prevent webhook retry storms
        # Critical: Always return 200 even on error to prevent webhook queue buildup
        return _acknowledge_webhook_error(
            271,
            f"Error in _serve_inbound_webhook: "
            f"Platform: {platform}, Tenant: {tenant_name} — {type(error).__name__}: {error}",
            exc_info=True,
        )


async def _enqueue_reply_job(
    arq_pool: ArqRedis,
    tenant_name: str,
    sender_id: str,
    reply_delay_seconds: int,
    triggering_message_id: Optional[str],
    conversation_id: Optional[str],
):
    """Enqueue run_reply_pipeline_job on the ARQ pool, deferred by the reply delay, with the job id
    gen:<triggering message id>; returns the arq Job (None when a job with that id already exists).
    """
    return await arq_pool.enqueue_job(
        "run_reply_pipeline_job",
        tenant_name,
        sender_id,
        None,  # agent_config_override: None = the tenant's AgentDocument
        triggering_message_id,
        conversation_id,
        _defer_by=reply_delay_seconds if reply_delay_seconds > 0 else None,
        _job_id=f"gen:{triggering_message_id}" if triggering_message_id else None,
    )


async def schedule_reply_job(
    arq_pool: ArqRedis,
    tenant_name: str,
    sender_id: str,
    reply_delay_seconds: int,
    should_wait_for_result: bool,
    triggering_message_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Optional[str]:
    """
    Enqueue ARQ task or await its result.

    Args:
        arq_pool: ARQ Redis pool
        tenant_name: Tenant name
        sender_id: Sender/user ID
        reply_delay_seconds: Delay in seconds before processing
        should_wait_for_result: If True, await the task result. If False, fire and forget.
        triggering_message_id: Optional ID of the specific message that triggered this task.
        conversation_id: Optional conversation ID. If provided, used directly instead of get_or_create.

    Returns:
        Task result if should_wait_for_result is True, None otherwise
    """
    try:
        queued_job = await _enqueue_reply_job(
            arq_pool, tenant_name, sender_id, reply_delay_seconds, triggering_message_id, conversation_id
        )
    except (ConnectionError, TimeoutError, OSError) as connection_error:
        # Redis connection died — reset the pool and retry once
        logger.error(f"ARQ enqueue failed ({type(connection_error).__name__}), resetting pool and retrying")
        arq_pool = await arq_redis_pool_manager.recreate_pool()
        queued_job = await _enqueue_reply_job(
            arq_pool, tenant_name, sender_id, reply_delay_seconds, triggering_message_id, conversation_id
        )

    if should_wait_for_result:
        # Wait for the task to complete and return result
        job_result = await queued_job.result()  # you can add 30 second timeout
        return job_result

    # Fire and forget - just return None
    return None


def _is_tenant_in_manual_mode(tenant_name: Optional[str]) -> bool:
    """Whether a human answers this tenant's customers (manual mode); False when the tenant name is empty or the
    tenant is not found.
    """
    if not tenant_name:
        return False
    tenant_doc = TenantDocument.objects(name=tenant_name).only("manual_mode").first()
    return bool(tenant_doc and tenant_doc.manual_mode)


def _is_arq_worker_enabled() -> bool:
    """Whether the reply is generated by the ARQ worker (USE_ARQ_WORKER=true) instead of inline."""
    return getenv("USE_ARQ_WORKER", "").lower() == "true"


async def _schedule_reply_through_arq(platform: str, save_result: SaveResponse) -> Optional[str]:
    """Enqueue the reply on the shared ARQ pool; returns the job result when the platform waits for it, else None."""
    # Get ARQ pool (singleton, reused across all requests)
    arq_pool = await arq_redis_pool_manager.ensure_pool()

    # Determine if we should wait for the response or fire and forget
    should_wait_for_result = should_wait_for_reply(platform, save_result.reply_delay_seconds)

    # Use ARQ for all platforms
    return await schedule_reply_job(
        arq_pool=arq_pool,
        tenant_name=save_result.tenant_name,
        sender_id=save_result.sender_id,
        reply_delay_seconds=save_result.reply_delay_seconds,
        should_wait_for_result=should_wait_for_result,
        triggering_message_id=save_result.triggering_message_id,
        conversation_id=save_result.conversation_id,
    )


async def save_message_and_dispatch_reply(platform: str, webhook_payload: any, tenant_name: Optional[str] = None):
    """Save the inbound message with the platform's handler, then generate the reply: nothing when there is no one to
    answer (outgoing, blocked, manual mode); through ARQ when USE_ARQ_WORKER is set, inline otherwise.
    """
    save_result = await SAVE_HANDLERS_BY_PLATFORM[platform](webhook_payload, tenant_name)
    sender_id = save_result.sender_id
    tenant_name = save_result.tenant_name
    triggering_message_id = save_result.triggering_message_id
    conversation_id = save_result.conversation_id
    if not sender_id:
        #  For example is outgoing message or the user is blocked or in agent mode
        return

    # Manual mode: message is saved but bot generation is skipped (human takes over)
    if _is_tenant_in_manual_mode(tenant_name):
        return

    if _is_arq_worker_enabled():
        return await _schedule_reply_through_arq(platform, save_result)
    else:
        return await run_reply_pipeline(
            tenant_name,
            sender_id,
            triggering_message_id=triggering_message_id,
            conversation_id=conversation_id,
        )


def should_wait_for_reply(platform, reply_delay_seconds):
    #  if platform is whatsapp always false
    # email is always fire-and-forget regardless of delay
    """Whether the webhook waits for the reply: web (and telephone) yes, email never, others only without a delay."""
    if platform == "email":
        return False
    return platform in ["web", "telephone"] or reply_delay_seconds == 0
