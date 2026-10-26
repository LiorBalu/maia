"""Builds the TwiML that connects an inbound Twilio call to the Realtime media stream (raised as TwimlReady)."""

import logging
from os import getenv

from twilio.twiml.voice_response import Connect, VoiceResponse

from maia.pipeline.context import TwimlReady, MessageContext
from maia.models.settings import CallSettingsDocument
from maia.utils.calls.call_routing import build_call_stream_url

logger = logging.getLogger(__name__)


async def connect_call_to_stream(args: MessageContext):
    """Answer an inbound Twilio Voice call by connecting it to the OpenAI Realtime media stream.

    Raises ``TwimlReady`` with the TwiML ``<Connect><Stream>``; returns ``None`` when the
    tenant has no call settings yet (the caller then ends the call silently).
    """
    if args["platform"] != "telephone":
        return
    call_settings = CallSettingsDocument.objects(tenant_name=args["tenant_name"]).first()
    if not call_settings:
        return
    host = getenv("BACKEND_URL").replace("https://", "").replace("http://", "")
    ws_url = build_call_stream_url(
        host, args["tenant_name"], args["sender_id"], args["recipient_id"], call_settings.tts_provider, direction="inbound"
    )
    logger.info(f"Inbound {call_settings.tts_provider} stream URL: {ws_url}")
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=ws_url)
    response.append(connect)
    raise TwimlReady(response)
