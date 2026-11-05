"""Configures the OpenAI Realtime (GA interface) session for a call: instructions from the prompts, voice settings, tools."""

# OpenAI Session Configuration
import logging
import datetime
import json
import os

import pytz

from tenants.hooks import prepare_context_for_tenant
from maia.pipeline.tool_calls import build_chat_completions_tool_schemas
from maia.pipeline.context import MessageContext
from maia.models.prompts import PromptDocument
from maia.models.settings import CallSettingsDocument

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1"

# Server VAD threshold for the whole call: high enough that phone-line noise is not taken for
# speech and does not cut the greeting.
SERVER_VAD_THRESHOLD = 0.8


def resolve_realtime_model_name() -> str:
    """The OpenAI Realtime model phone calls run on: ``MAIA_REALTIME_MODEL``, default ``gpt-realtime-2.1``."""
    return os.getenv("MAIA_REALTIME_MODEL") or DEFAULT_REALTIME_MODEL


def _compose_session_instructions(phone_prompt_text: str) -> str:
    """The session instructions: the current Jerusalem time and date (two Hebrew lines), then the phone prompt."""
    jerusalem_timezone = pytz.timezone("Asia/Jerusalem")
    current_time_text = datetime.datetime.now(jerusalem_timezone).strftime("%H:%M")
    current_date_text = datetime.datetime.now(jerusalem_timezone).strftime("%d/%m/%Y")

    # The 8-space indentation of the lines below is part of the text OpenAI receives: keep it as is.
    session_instructions = f"""
        השעה הנוכחית היא: {current_time_text}
        היום הוא: {current_date_text}

        {phone_prompt_text}
        """
    return session_instructions


def _build_audio_input_config(call_settings: CallSettingsDocument) -> dict:
    """The ``audio.input`` block of the session: mu-law in, server VAD, transcription, optional noise reduction."""
    noise_reduction_mode = call_settings.realtime_conf.noise_reduction_mode

    # GA Realtime session shape: audio settings live under ``audio.input`` / ``audio.output``,
    # Twilio's mu-law is ``audio/pcmu``, and the session has no ``temperature``.
    audio_input_config = {
        "format": {"type": "audio/pcmu"},
        "turn_detection": {
            "type": "server_vad",
            "threshold": SERVER_VAD_THRESHOLD,
            "prefix_padding_ms": call_settings.realtime_conf.prefix_padding_ms,
            "silence_duration_ms": call_settings.realtime_conf.silence_duration_ms,
            "create_response": True,
            "interrupt_response": True,
        },
        "transcription": {"model": call_settings.realtime_conf.audio_transcription_model},
    }
    if noise_reduction_mode in ("near_field", "far_field"):
        audio_input_config["noise_reduction"] = {"type": noise_reduction_mode}
    if call_settings.realtime_conf.audio_transcription_language:
        audio_input_config["transcription"]["language"] = call_settings.realtime_conf.audio_transcription_language
    return audio_input_config


def _build_session_config(session_instructions: str, call_settings: CallSettingsDocument) -> dict:
    """The session that ``session.update`` carries, tools not yet included: audio only, the instructions, the audio
    input block and the audio output (the tenant's voice, speed capped at 1.5)."""
    return {
        "type": "realtime",
        "output_modalities": ["audio"],
        "instructions": session_instructions,
        "audio": {
            "input": _build_audio_input_config(call_settings),
            "output": {
                "format": {"type": "audio/pcmu"},
                "voice": call_settings.gpt_voice,
                "speed": min(call_settings.openai_speed, 1.5),
            },
        },
    }


def _ensure_hangup_prompt(tenant_name: str) -> PromptDocument:
    """The tenant's ``hangup_call`` prompt; created with the default English description and saved when missing."""
    hangup_prompt_doc = PromptDocument.objects(tenant_name=tenant_name, name="hangup_call").first()
    if not hangup_prompt_doc:
        hangup_prompt_doc = PromptDocument(
            tenant_name=tenant_name,
            name="hangup_call",
            content="End the call with the caller. Use when conversation is complete or caller requests.",
        )
        hangup_prompt_doc.save()
    return hangup_prompt_doc


def _build_hangup_tool_schema(tool_description: str) -> dict:
    """Schema (chat-function format) of the built-in ``hangup_call`` tool, which ends the call."""
    return {
        "name": "hangup_call",
        "description": tool_description,
        "parameters": {
            "type": "object",
            "properties": {
                "goodbye_message": {
                    "type": "string",
                    "description": "A polite goodbye message to say before ending the call",
                    "default": "Thank you for calling. Have a great day!",
                },
                "reason": {"type": "string", "description": "The reason for ending the call"},
                "follow_up": {"type": "boolean", "description": "Whether follow-up is needed after the call"},
            },
            "required": ["reason"],
        },
    }


def _collect_session_tool_schemas(message_context: MessageContext, tenant_name: str) -> list:
    """The tools of the session: hangup_call, then the schemas of the ``@llm_tool`` functions named in
    ``message_context["tool_names"]``.

    May write to the database: the tenant's hangup_call prompt is created and saved the first time it is needed.
    The schemas are appended to ``message_context["tool_schemas"]`` in place, and that same list is returned.
    """
    # Add hangup call tool for all tenants
    hangup_prompt_doc = _ensure_hangup_prompt(tenant_name)
    message_context["tool_schemas"].append(_build_hangup_tool_schema(hangup_prompt_doc.prompt_text))
    session_tools = message_context["tool_schemas"]
    # Add decorated function tools if specified in message_context
    if message_context["tool_names"]:
        bound_tool_schemas = [
            tool_entry.get("function")
            for tool_entry in build_chat_completions_tool_schemas(message_context["tool_names"])
        ]
        session_tools.extend(bound_tool_schemas)
    return session_tools


def _convert_to_realtime_tool_schema(tool_schema: dict) -> dict:
    """A copy of one tool schema in the shape the Realtime session accepts."""
    # Copy the dict WITHOUT the strict key
    realtime_tool_schema = {
        key: value for key, value in tool_schema.items() if key not in ["strict", "additionalProperties"]
    }
    # Force the type if missing
    if "type" not in realtime_tool_schema:
        realtime_tool_schema["type"] = "function"
    return realtime_tool_schema


def _attach_realtime_tools(session_config: dict, session_tools: list) -> None:
    """Add the tools (Realtime shape) and ``tool_choice: auto`` to the session config; no tools leaves both out."""
    if session_tools:
        session_config["tools"] = [_convert_to_realtime_tool_schema(tool_schema) for tool_schema in session_tools]
        session_config["tool_choice"] = "auto"


async def _send_session_update(realtime_websocket, session_config: dict) -> None:
    """Send the ``session.update`` event that carries the session config."""
    # Send session update with modified configuration
    session_update_event = {"type": "session.update", "session": session_config}
    logger.info("Sending session update")
    await realtime_websocket.send(json.dumps(session_update_event))


async def _trigger_first_assistant_response(realtime_websocket) -> None:
    """Ask the model for a response right away, so the secretary speaks the greeting first."""
    # Trigger the assistant to speak first by creating a response
    logger.info("Triggering assistant to speak first")
    await realtime_websocket.send(json.dumps({"type": "response.create"}))


async def configure_realtime_session(
    realtime_websocket,
    phone_prompt_text: str,
    call_settings: CallSettingsDocument,
    tenant_name: str,
    message_context: MessageContext,
):
    """Control initial session with OpenAI."""
    try:
        logger.info("Initializing session...")
        await prepare_context_for_tenant(message_context, tenant_name)

        session_instructions = _compose_session_instructions(phone_prompt_text)
        session_config = _build_session_config(session_instructions, call_settings)
        session_tools = _collect_session_tool_schemas(message_context, tenant_name)
        _attach_realtime_tools(session_config, session_tools)

        await _send_session_update(realtime_websocket, session_config)
        await _trigger_first_assistant_response(realtime_websocket)
    except Exception as error:
        logger.error(f"Error initializing session: {error}")
        raise error
