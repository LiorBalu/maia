"""Configures the OpenAI Realtime (GA interface) session for a call: instructions from the prompts, voice settings, tools."""

# OpenAI Session Configuration
import logging
import datetime
import json
import os

import pytz

from tenants.hooks import run_tenant_hooks
from maia.pipeline.tool_calls import tool_schemas_for
from maia.pipeline.context import MessageContext
from maia.models.prompts import PromptDocument
from maia.models.settings import CallSettingsDocument

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_MODEL = "gpt-realtime-1.5"


def realtime_model() -> str:
    """The OpenAI Realtime model phone calls run on: ``MAIA_REALTIME_MODEL``, default ``gpt-realtime-1.5``."""
    return os.getenv("MAIA_REALTIME_MODEL") or DEFAULT_REALTIME_MODEL


async def initialize_session(
    openai_ws,
    instructions: str,
    call_settings: CallSettingsDocument,
    tenant_name: str,
    request: MessageContext,
    is_first_message: bool = True,
):
    """Control initial session with OpenAI."""
    try:
        logger.info("Initializing session...")
        await run_tenant_hooks(request, tenant_name)

        if request["additional_context"]:
            instructions += f"\n\n{request['additional_context']}"
        if request["customer"].meta_data.get("additional_context"):
            instructions += f"\n\n{request['customer'].meta_data.get('additional_context')}"
        if request["extra_prompt_name"]:
            instructions += PromptDocument.objects(tenant_name=tenant_name, name=request["extra_prompt_name"]).first().prompt
        if request["customer"].meta_data.get("custom_prompt"):
            pr = PromptDocument.objects(
                tenant_name=tenant_name, name=request["customer"].meta_data.get("custom_prompt")
            ).first()
            if pr:
                instructions += pr.prompt
        tz = pytz.timezone("Asia/Jerusalem")
        current_time = datetime.datetime.now(tz).strftime("%H:%M")
        current_date = datetime.datetime.now(tz).strftime("%d/%m/%Y")

        general_prompt = f"""
        השעה הנוכחית היא: {current_time}
        היום הוא: {current_date}

        {instructions}
        """
        noise_mode = call_settings.realtime_conf.noise_reduction_mode

        # The first turn uses a higher VAD threshold so line noise does not cut the greeting;
        # later turns use the configured threshold.
        threshold = 0.8 if is_first_message else call_settings.realtime_conf.threshold

        # GA Realtime session shape: audio settings live under ``audio.input`` / ``audio.output``,
        # Twilio's mu-law is ``audio/pcmu``, and the session has no ``temperature``.
        audio_input = {
            "format": {"type": "audio/pcmu"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": threshold,
                "prefix_padding_ms": call_settings.realtime_conf.prefix_padding_ms,
                "silence_duration_ms": call_settings.realtime_conf.silence_duration_ms,
                "create_response": True,
                "interrupt_response": True,
            },
            "transcription": {"model": call_settings.realtime_conf.audio_transcription_model},
        }
        if noise_mode in ("near_field", "far_field"):
            audio_input["noise_reduction"] = {"type": noise_mode}
        if call_settings.realtime_conf.audio_transcription_language:
            audio_input["transcription"]["language"] = call_settings.realtime_conf.audio_transcription_language
        json_query = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": general_prompt,
            "audio": {
                "input": audio_input,
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": call_settings.gpt_voice,
                    "speed": min(call_settings.openai_speed, 1.5),
                },
            },
        }
        if request["chat_settings"].agent_requested is True:
            request["tool_schemas"].extend(
                [
                    {
                        "name": "call_to_manager",
                        "description": "Initiates a call to the manager",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "call_reason": {"type": "string", "description": "The reason for initiating the call"}
                            },
                            "required": ["call_reason"],
                        },
                    }
                ]
            )

        # Add hangup call tool for all tenants
        hangup_prompt = PromptDocument.objects(tenant_name=tenant_name, name="hangup_call").first()
        if not hangup_prompt:
            hangup_prompt = PromptDocument(
                tenant_name=tenant_name,
                name="hangup_call",
                content="End the call with the caller. Use when conversation is complete or caller requests.",
            )
            hangup_prompt.save()
        request["tool_schemas"].append(
            {
                "name": "hangup_call",
                "description": hangup_prompt.prompt,
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
        )
        tools_c = request["tool_schemas"]
        # Add decorated function tools if specified in request
        if request["tool_names"]:
            decorated_tools = [
                fc.get("function")
                for fc in tool_schemas_for(request["tool_names"])
            ]
            tools_c.extend(decorated_tools)
        if tools_c:
            json_query["tools"] = []
            for tool in tools_c:
                # Copy the dict WITHOUT the strict key
                tool_clean = {k: v for k, v in tool.items() if k not in ["strict", "additionalProperties"]}
                # Force the type if missing
                if "type" not in tool_clean:
                    tool_clean["type"] = "function"
                json_query["tools"].append(tool_clean)
            json_query["tool_choice"] = "auto"
        config = json_query
        # Get the default config
        # config = get_session_config(voice, instructions, tenant_name)

        # Send session update with modified configuration
        session_update = {"type": "session.update", "session": config}
        # print(config)
        logger.info("Sending session update")
        await openai_ws.send(json.dumps(session_update))

        # Trigger the assistant to speak first by creating a response
        logger.info("Triggering assistant to speak first")
        await openai_ws.send(json.dumps({"type": "response.create"}))
    except Exception as e:
        logger.error(f"Error initializing session: {e}")
        raise e
