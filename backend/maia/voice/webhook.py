"""Twilio Voice webhooks (``/api/v1/telephone/...``).

Text generation lives in ``/api/async/...`` (``maia/inbound/routes.py`` → ``reply_to_message``).
Endpoints:

- ``POST /telephone/{tenant_name}``: Twilio Voice "A call comes in" webhook. ``connect_call_to_stream``
  answers with TwiML that connects the call to the realtime media stream (``maia/voice/media_stream.py``).
- ``POST /telephone/{tenant_name}/call-status``: Twilio status callback (call stats + ``call_auto_reset`` on_end).
"""

import logging
import traceback
from datetime import datetime, timezone
from logging import error

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse

from maia.voice.connect_stream import connect_call_to_stream
from maia.voice.normalize_call import normalize_call_data
from maia.models.messages import MessageDocument
from maia.utils.conversation.reset import reset_conversation

from maia.pipeline.context import BotInactive, TwimlReady, TenantNotFound, MessageContext

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/telephone/{tenant_name}", response_class=PlainTextResponse)
async def telephone_inbound(fast_request: Request, tenant_name: str):
    """Twilio Voice webhook ("A call comes in")."""
    try:
        form = await fast_request.form()
        try:
            data = await fast_request.json()
        except Exception:
            data = form
        args = normalize_call_data(data, tenant_name)
        return await answer_inbound_call(args)
    except (BotInactive, TenantNotFound):
        return PlainTextResponse("", status_code=200)
    except Exception as e:
        error(f"An error occurred in 'telephone_inbound': {type(e).__name__}: {e}")
        error("Traceback details:\n" + traceback.format_exc())
        return PlainTextResponse("", status_code=200)


async def answer_inbound_call(args: MessageContext):
    logger.info(" ".join(str(part) for part in ("\n", "#" * 100)))
    logger.info(" ".join(str(part) for part in ("tenant_name: ", args["tenant_name"], "platform:", args["platform"])))
    logger.info(" ".join(str(part) for part in ("from: ", args["sender_id"], "message: ", args["message_text"])))
    save_inbound_call_message(args)

    try:
        # connect_call_to_stream raises TwimlReady with the TwiML <Connect><Stream> of the
        # OpenAI Realtime media stream.
        await connect_call_to_stream(args)
        if args["tenant"].manual_mode:
            logger.info(f"Tenant '{args['tenant_name']}' is in manual mode")
            raise BotInactive()
        if not args["tenant"].is_bot_active_for_platform(args["platform"]):
            logger.info(f"Bot is not active for platform '{args['platform']}' in tenant '{args['tenant_name']}'")
            raise BotInactive()
        # No call settings for this tenant yet: nothing to connect to, end the call silently.
        logger.warning(f"No call settings for tenant '{args['tenant_name']}': ending call silently")
        return PlainTextResponse(content=str(VoiceResponse()), media_type="application/xml")
    except TwimlReady as twilio_response:
        return PlainTextResponse(str(twilio_response), media_type="application/xml")
    except BotInactive:
        return PlainTextResponse("Manual mode")
    except TenantNotFound:
        return PlainTextResponse("Tenant not found", status_code=404)
    except Exception as message:
        error(f"An error occurred in 'answer_inbound_call': {type(message).__name__}: {message}")
        error("Traceback details:\n" + traceback.format_exc())
        resp = VoiceResponse()
        resp.say("An error occurred in the system. Please check the logs.")
        return PlainTextResponse(str(resp), media_type="application/xml")


def save_inbound_call_message(request):
    message = MessageDocument(
        platform=request["platform"],
        tenant_name=request["tenant_name"],
        profile_name=(request["customer"].name if request["customer"].name else request["sender_name"]),
        phone_number=request["sender_id"],
        message=request["message_text"],
        role="user",
        sid=request["message_id"],
        date=datetime.now(timezone.utc),
        to=request["recipient_id"],
        msg_type=request["message_type"],
        media_url=request["media_url"],
    )
    message.save()
    message.reload()
    request["meta_data"]["request_message"] = message


@router.post("/telephone/{tenant_name}/call-status", response_class=PlainTextResponse)
async def telephone_status_callback(request: Request, tenant_name: str):
    form = await request.form()

    ts = datetime.now(timezone.utc).isoformat()
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    call_duration = form.get("CallDuration")
    answered_by = form.get("AnsweredBy")
    sip_response_code = form.get("SipResponseCode")

    logger.info(f"[call-status] tenant={tenant_name} ts={ts} CallStatus={call_status} "
        f"CallSid={call_sid} Duration={call_duration} "
        f"AnsweredBy={answered_by} SipResponseCode={sip_response_code} "
        f"From={form.get('From')} To={form.get('To')}")
    logger.info(f"[call-status] full payload: {dict(form)}")

    from maia.models.settings import CallSettingsDocument

    settings = CallSettingsDocument.objects(tenant_name=tenant_name).first()

    # Resolve the customer's number (the leg that is not our own caller line).
    from_number = form.get("From")
    from_number = from_number.replace("+", "") if from_number else None
    if settings and settings.phone_number == from_number:
        from_number = form.get("To").replace("+", "") if form.get("To") else None

    # Update per-conversation call stats for every terminal status.
    ended_statuses = {"completed", "busy", "no-answer", "failed", "canceled"}
    if call_status in ended_statuses and from_number:
        try:
            from maia.utils.calls.call_stats import record_call_finished

            answered = call_status == "completed"
            record_call_finished(
                tenant_name=tenant_name,
                phone_number=from_number,
                call_status=call_status,
                duration_seconds=int(call_duration) if (call_duration or "").isdigit() else 0,
                answered=answered,
                hung_up_by_customer=False,
            )
        except Exception as e:
            logger.error(f"[call-status] failed to record call stats: {e}")

    if call_status != "completed":
        logger.info(f"[call-status] SKIP reset (status={call_status}) — only 'completed' resets the conversation")
        return PlainTextResponse("")

    from maia.models.tenants import call_auto_reset_enabled

    if not call_auto_reset_enabled(tenant_name, "on_end"):
        logger.info(f"[call-status] SKIP reset — call_auto_reset on_end disabled for tenant={tenant_name}")
        return PlainTextResponse("")

    # Scope to "telephone": platforms are always kept in separate conversations, so the WhatsApp thread stays.
    await reset_conversation(tenant_name=tenant_name, phone_number=from_number, platform="telephone")
    return PlainTextResponse("")
