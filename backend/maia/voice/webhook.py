"""Twilio Voice webhooks (``/api/v1/telephone/...``).

Text generation lives in ``/api/async/...`` (``maia/inbound/routes.py`` → ``run_reply_pipeline``).
Endpoints:

- ``POST /telephone/{tenant_name}``: Twilio Voice "A call comes in" webhook. ``connect_inbound_call_to_media_stream``
  answers with TwiML that connects the call to the realtime media stream (``maia/voice/media_stream.py``).
- ``POST /telephone/{tenant_name}/call-status``: Twilio status callback (``call_auto_reset`` on_end).
"""

import logging
import traceback
from datetime import datetime, timezone
from logging import error

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse

from maia.voice.connect_stream import connect_inbound_call_to_media_stream
from maia.voice.normalize_call import build_call_message_context
from maia.models.messages import MessageDocument
from maia.utils.conversation.reset import end_customer_conversation

from maia.pipeline.context import TwimlReady, TenantNotFound, MessageContext

logger = logging.getLogger(__name__)

router = APIRouter()


async def _read_call_webhook_payload(http_request: Request):
    """Return the request's JSON body when it carries one, otherwise its form (what Twilio posts).

    The form is read first; the ``json()`` attempt after it falls back to that form on any exception.
    """
    call_form = await http_request.form()
    try:
        return await http_request.json()
    except Exception:
        # Expected path for Twilio's form posts: form() consumed the stream, so json() raises RuntimeError.
        return call_form


@router.post("/telephone/{tenant_name}", response_class=PlainTextResponse)
async def receive_inbound_call_webhook(http_request: Request, tenant_name: str):
    """Twilio Voice webhook ("A call comes in")."""
    try:
        webhook_payload = await _read_call_webhook_payload(http_request)
        call_context = build_call_message_context(webhook_payload, tenant_name)
        return await respond_to_inbound_call(call_context)
    except TenantNotFound:
        return PlainTextResponse("", status_code=200)
    except Exception as exc:
        error(f"An error occurred in 'receive_inbound_call_webhook': {type(exc).__name__}: {exc}")
        error("Traceback details:\n" + traceback.format_exc())
        return PlainTextResponse("", status_code=200)


def _log_inbound_call(message_context: MessageContext):
    """Log a banner line, then the tenant / platform and the caller / message of the inbound call."""
    logger.info(" ".join(str(fragment) for fragment in ("\n", "#" * 100)))
    logger.info(" ".join(str(fragment) for fragment in ("tenant_name: ", message_context["tenant_name"], "platform:", message_context["platform"])))
    logger.info(" ".join(str(fragment) for fragment in ("from: ", message_context["sender_id"], "message: ", message_context["message_text"])))


def _render_call_error_response(exc: Exception) -> PlainTextResponse:
    """Log the error with its traceback and answer with TwiML that tells the caller an error occurred.

    Call it from inside the ``except`` clause: ``traceback.format_exc()`` reads the exception being handled.
    """
    error(f"An error occurred in 'respond_to_inbound_call': {type(exc).__name__}: {exc}")
    error("Traceback details:\n" + traceback.format_exc())
    error_voice_response = VoiceResponse()
    error_voice_response.say("An error occurred in the system. Please check the logs.")
    return PlainTextResponse(str(error_voice_response), media_type="application/xml")


async def respond_to_inbound_call(message_context: MessageContext):
    """Save the inbound call as a message, then answer with the TwiML that connects the call to the Realtime stream;
    missing call settings end the call silently.
    """
    _log_inbound_call(message_context)
    save_call_message(message_context)

    try:
        # connect_inbound_call_to_media_stream raises TwimlReady with the TwiML <Connect><Stream> of the
        # OpenAI Realtime media stream.
        await connect_inbound_call_to_media_stream(message_context)
        # No call settings for this tenant yet: nothing to connect to, end the call silently.
        logger.warning(f"No call settings for tenant '{message_context['tenant_name']}': ending call silently")
        return PlainTextResponse(content=str(VoiceResponse()), media_type="application/xml")
    except TwimlReady as twiml_ready:
        return PlainTextResponse(str(twiml_ready), media_type="application/xml")
    except TenantNotFound:
        return PlainTextResponse("Tenant not found", status_code=404)
    except Exception as exc:
        return _render_call_error_response(exc)


def save_call_message(message_context):
    """Save the inbound call as a user message of the telephone conversation and keep it in the context."""
    call_message_doc = MessageDocument(
        platform=message_context["platform"],
        tenant_name=message_context["tenant_name"],
        profile_name=(message_context["customer"].name if message_context["customer"].name else message_context["sender_name"]),
        phone_number=message_context["sender_id"],
        message=message_context["message_text"],
        role="user",
        sid=message_context["message_id"],
        date=datetime.now(timezone.utc),
        to=message_context["recipient_id"],
        msg_type=message_context["message_type"],
        media_url=message_context["media_url"],
    )
    call_message_doc.save()
    call_message_doc.reload()
    message_context["meta_data"]["request_message"] = call_message_doc


def _log_call_status(tenant_name: str, status_form):
    """Log Twilio's call status report: one summary line (with From / To), then the whole form."""
    received_at = datetime.now(timezone.utc).isoformat()
    call_sid = status_form.get("CallSid")
    call_status = status_form.get("CallStatus")
    call_duration = status_form.get("CallDuration")
    answered_by = status_form.get("AnsweredBy")
    sip_response_code = status_form.get("SipResponseCode")

    logger.info(f"[call-status] tenant={tenant_name} ts={received_at} CallStatus={call_status} "
        f"CallSid={call_sid} Duration={call_duration} "
        f"AnsweredBy={answered_by} SipResponseCode={sip_response_code} "
        f"From={status_form.get('From')} To={status_form.get('To')}")
    logger.info(f"[call-status] full payload: {dict(status_form)}")


async def _reset_telephone_conversation_if_completed(tenant_name: str, call_status: str | None, caller_number: str | None):
    """End the caller's telephone conversation when the call completed and the tenant has call_auto_reset on_end;
    otherwise log why the reset is skipped.
    """
    if call_status != "completed":
        logger.info(f"[call-status] SKIP reset (status={call_status}) — only 'completed' resets the conversation")
        return

    from maia.models.tenants import should_auto_reset_call_conversation

    if not should_auto_reset_call_conversation(tenant_name, "on_end"):
        logger.info(f"[call-status] SKIP reset — call_auto_reset on_end disabled for tenant={tenant_name}")
        return

    # Scope to "telephone": platforms are always kept in separate conversations, so the WhatsApp thread stays.
    await end_customer_conversation(tenant_name=tenant_name, sender_id=caller_number, platform="telephone")


@router.post("/telephone/{tenant_name}/call-status", response_class=PlainTextResponse)
async def receive_call_status_callback(http_request: Request, tenant_name: str):
    """Twilio status callback: log the call and reset the telephone conversation when a completed call ends
    (call_auto_reset on_end).
    """
    status_form = await http_request.form()
    _log_call_status(tenant_name, status_form)

    # Inbound calls only: the customer is the caller.
    raw_caller_number = status_form.get("From")
    caller_number = raw_caller_number.replace("+", "") if raw_caller_number else None

    call_status = status_form.get("CallStatus")
    await _reset_telephone_conversation_if_completed(tenant_name, call_status, caller_number)
    return PlainTextResponse("")
