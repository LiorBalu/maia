"""Builds the TwiML that connects an inbound Twilio call to the Realtime media stream (raised as TwimlReady)."""

import logging
from os import getenv

from twilio.twiml.voice_response import Connect, VoiceResponse

from maia.pipeline.context import TwimlReady, MessageContext
from maia.models.settings import CallSettingsDocument
from maia.utils.calls.call_routing import build_media_stream_url

logger = logging.getLogger(__name__)


def _resolve_media_stream_url(message_context: MessageContext, tts_provider: str) -> str:
    """The ``wss://`` media-stream URL of the inbound call, on the host of ``BACKEND_URL`` (read on every call)."""
    backend_host = getenv("BACKEND_URL").replace("https://", "").replace("http://", "")
    return build_media_stream_url(
        backend_host, message_context["tenant_name"], message_context["sender_id"], message_context["recipient_id"], tts_provider, call_direction="inbound"
    )


def _render_connect_stream_twiml(media_stream_url: str) -> VoiceResponse:
    """The TwiML ``<Response><Connect><Stream url=...>`` that connects the call to the media stream."""
    twiml_response = VoiceResponse()
    connect_verb = Connect()
    connect_verb.stream(url=media_stream_url)
    twiml_response.append(connect_verb)
    return twiml_response


async def connect_inbound_call_to_media_stream(message_context: MessageContext):
    """Answer an inbound Twilio Voice call by connecting it to the OpenAI Realtime media stream.

    Raises ``TwimlReady`` with the TwiML ``<Connect><Stream>``; returns ``None`` when the
    tenant has no call settings yet (the caller then ends the call silently).
    """
    if message_context["platform"] != "telephone":
        return
    call_settings_doc = CallSettingsDocument.objects(tenant_name=message_context["tenant_name"]).first()
    if not call_settings_doc:
        return
    media_stream_url = _resolve_media_stream_url(message_context, call_settings_doc.tts_provider)
    logger.info(f"Inbound {call_settings_doc.tts_provider} stream URL: {media_stream_url}")
    twiml_response = _render_connect_stream_twiml(media_stream_url)
    raise TwimlReady(twiml_response)
