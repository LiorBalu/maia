"""The phone call itself: Twilio media stream (mu-law audio) bridged to the OpenAI Realtime session, with tool calls."""

import logging
import asyncio
import base64
import datetime
import json

import websockets
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from maia.pipeline.context import MessageContext
from maia.services.registry import build_tenant_chatbot
from maia.models.customers import ensure_customer
from maia.models.messages import MessageDocument
from maia.models.prompts import PromptDocument
from maia.models.tenants import TenantDocument
from maia.models.settings import CallSettingsDocument, ChatSettingsDocument
from maia.utils.conversation.get_or_create import ensure_online_conversation
from maia.voice.call_control import EndCall, execute_realtime_tool_call
from maia.voice.session import configure_realtime_session, resolve_realtime_model_name
from maia.voice.logger import SimpleLogger

module_logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_chat_settings(tenant_name: str) -> ChatSettingsDocument:
    """The tenant's ChatSettingsDocument, created with the defaults when missing."""
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if not chat_settings:
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name).save()
    return chat_settings


def build_realtime_message_context(tenant_name, from_number, called_number, call_settings) -> MessageContext:
    """Build the pipeline's ``MessageContext`` for a Realtime call session (no inbound text yet)."""
    customer = ensure_customer(
        phone_or_email=from_number, tenant_name=tenant_name, customer_profile_name=from_number, platform="telephone"
    )
    # Prefer the stored customer name (e.g. passed via start-call) over the raw number.
    caller_display_name = customer.name or from_number
    tenant = TenantDocument.objects(name=tenant_name).first()
    chat_settings = _ensure_chat_settings(tenant_name)
    conversation = ensure_online_conversation(tenant_name, from_number, "telephone", caller_display_name)
    return MessageContext(
        tenant_name=tenant_name,
        platform="telephone",
        customer=customer,
        tenant=tenant,
        conversation=conversation,
        chat_settings=chat_settings,
        sender_id=from_number,
        recipient_id=called_number,
        sender_name=caller_display_name,
        message_text="",
        message_id="",
        message_type=None,
        media_url=None,
        media_content_type=None,
        meta_data={},
        tool_names=[],
        tool_schemas=[],
        tool_results={},
    )


def _ensure_call_settings(tenant_name: str) -> CallSettingsDocument:
    """The tenant's CallSettingsDocument, created with the defaults when missing."""
    call_settings = CallSettingsDocument.objects(tenant_name=tenant_name).first()
    if not call_settings:
        call_settings = CallSettingsDocument(tenant_name=tenant_name).save()
    return call_settings


def _load_realtime_instructions(tenant_name: str) -> str:
    """Text of the tenant's ``platform_realtime`` prompt (the phone prompt); an empty prompt is created when missing."""
    realtime_prompt_doc = PromptDocument.objects(tenant_name=tenant_name, name="platform_realtime").first()
    if not realtime_prompt_doc:
        realtime_prompt_doc = PromptDocument(tenant_name=tenant_name, name="platform_realtime", content="").save()
    return realtime_prompt_doc.prompt_text


async def _close_openai_socket(
    realtime_websocket, call_log: SimpleLogger, closed_event_text: str, failure_event_prefix: str
) -> None:
    """Close the OpenAI Realtime websocket if it is still open and record the outcome in the call log.

    A clean close records ``closed_event_text``; a failed one records ``"<failure_event_prefix>: <error>"``.
    """
    if realtime_websocket and realtime_websocket.open:
        try:
            await realtime_websocket.close()
            call_log.record_call_event("system", closed_event_text, True)
        except Exception as error:
            call_log.record_call_event("error", f"{failure_event_prefix}: {str(error)}", True)


async def _shutdown_call_sockets(realtime_websocket, twilio_websocket: WebSocket, call_log: SimpleLogger) -> None:
    """End of the media-stream handler: close the OpenAI socket if still open, close the Twilio socket, record the end.

    ``realtime_websocket`` is the handler's own variable: None when the OpenAI connection was never opened.
    """
    # Ensure OpenAI WebSocket is always closed when the main handler ends
    await _close_openai_socket(
        realtime_websocket,
        call_log,
        "OpenAI WebSocket closed in main finally block",
        "❌ Error closing OpenAI WebSocket in finally",
    )

    # Ensure Twilio WebSocket is closed
    try:
        await twilio_websocket.close()
        call_log.record_call_event("system", "Twilio WebSocket closed in main finally block", True)
    except Exception as error:
        call_log.record_call_event("error", f"❌ Error closing Twilio WebSocket in finally: {str(error)}", True)

    call_log.record_call_event("system", "=== Ending WebSocket Connection ===", True)


class _RealtimeCallBridge:
    """One call's bridge between the Twilio media stream and the OpenAI Realtime session.

    ``run()`` runs the two pumps (Twilio audio -> OpenAI, OpenAI events -> Twilio) concurrently; they share the
    per-connection state kept on the instance.
    """

    def __init__(
        self,
        twilio_websocket: WebSocket,
        realtime_websocket,
        message_context: MessageContext,
        call_log: SimpleLogger,
        tenant_name: str,
        from_number: str,
        called_number: str,
    ):
        """Keep the two sockets, the call's context and log and the call's route identity; start with no stream."""
        self.twilio_websocket = twilio_websocket
        self.realtime_websocket = realtime_websocket
        self.message_context = message_context
        self.call_log = call_log
        self.tenant_name = tenant_name
        self.from_number = from_number
        self.called_number = called_number

        # Connection specific state
        self.twilio_stream_id = None
        self.latest_media_timestamp_ms = 0
        self.last_assistant_item_id = None
        self.pending_marks = []
        self.response_start_timestamp_ms = None
        self.is_greeting_in_progress = True

        # Transcript fragments of the caller's current turn and of the assistant's current response
        self.caller_transcript_parts = []
        self.assistant_transcript_parts = []

    def _start_stream(self, twilio_event: dict) -> None:
        """Twilio's ``start`` event: keep the stream id and reset the response tracking."""
        self.twilio_stream_id = twilio_event["start"]["streamSid"]
        self.call_log.record_call_event("system", f"✓ Stream started: {self.twilio_stream_id}", True)
        self.response_start_timestamp_ms = None
        self.latest_media_timestamp_ms = 0
        self.last_assistant_item_id = None

    async def pump_twilio_audio_to_openai(self) -> None:
        """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
        try:
            async for raw_twilio_message in self.twilio_websocket.iter_text():
                twilio_event = json.loads(raw_twilio_message)
                if twilio_event["event"] == "media" and self.realtime_websocket and self.realtime_websocket.open:
                    self.latest_media_timestamp_ms = int(twilio_event["media"]["timestamp"])
                    inbound_audio_base64 = twilio_event["media"]["payload"]
                    audio_append_event = {"type": "input_audio_buffer.append", "audio": inbound_audio_base64}

                    await self.realtime_websocket.send(json.dumps(audio_append_event))

                elif twilio_event["event"] == "start":
                    self._start_stream(twilio_event)
                elif twilio_event["event"] == "mark":
                    if self.pending_marks:
                        self.pending_marks.pop(0)
                elif twilio_event["event"] == "stop":
                    self.call_log.record_call_event("system", "Call ended by Twilio", True)
                    break

            # If we exit the loop normally (not by exception), it means Twilio disconnected
            self.call_log.record_call_event("system", "Twilio WebSocket disconnected normally", True)

        except WebSocketDisconnect:
            self.call_log.record_call_event("system", "Client disconnected (WebSocketDisconnect)", True)
        except Exception as error:
            self.call_log.record_call_event("error", f"❌ Error in receive_from_twilio: {str(error)}", True)
        finally:
            # Always close OpenAI WebSocket when Twilio disconnects
            await _close_openai_socket(
                self.realtime_websocket,
                self.call_log,
                "OpenAI WebSocket closed from receive_from_twilio",
                "❌ Error closing OpenAI WebSocket",
            )

    async def _dispatch_tool_call(self, openai_event: dict) -> None:
        """Run the tool the Realtime model called (``response.function_call_arguments.done``); EndCall propagates."""
        module_logger.info(f"Function call details: {json.dumps(openai_event, indent=2)}")
        tool_name = openai_event.get("name")
        tool_arguments = json.loads(openai_event.get("arguments", "{}"))

        module_logger.info("✓ Handling function call: " + tool_name)
        await execute_realtime_tool_call(
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            realtime_websocket=self.realtime_websocket,
            twilio_websocket=self.twilio_websocket,
            twilio_stream_id=self.twilio_stream_id,
            call_logger=self.call_log,
            tool_call_id=openai_event.get("call_id"),
            message_context=self.message_context,
        )

    def _persist_transcript_line(self, transcript_text: str, speaker_role="user"):
        """Save a transcript line (user or assistant) as a telephone message of the conversation."""
        transcript_message_doc = MessageDocument(
            tenant_name=self.tenant_name,
            message=transcript_text,
            phone_number=self.from_number,
            platform="telephone",
            role=speaker_role,
            profile_name=self.message_context["sender_name"] if speaker_role == "user" else "AI",
            date=datetime.datetime.now(datetime.timezone.utc),
            to=self.called_number,
        )
        transcript_message_doc.save()

    async def _truncate_interrupted_response(self) -> None:
        """Handle interruption when the caller's speech starts."""
        self.call_log.record_call_event("system", "Handling speech started event", True)
        if self.pending_marks and self.response_start_timestamp_ms is not None:
            elapsed_audio_ms = self.latest_media_timestamp_ms - self.response_start_timestamp_ms

            if self.last_assistant_item_id:
                truncate_item_event = {
                    "type": "conversation.item.truncate",
                    "item_id": self.last_assistant_item_id,
                    "content_index": 0,
                    "audio_end_ms": elapsed_audio_ms,
                }
                await self.realtime_websocket.send(json.dumps(truncate_item_event))

            await self.twilio_websocket.send_json({"event": "clear", "streamSid": self.twilio_stream_id})

            self.pending_marks.clear()
            self.last_assistant_item_id = None
            self.response_start_timestamp_ms = None

    async def _track_transcripts(self, openai_event: dict) -> None:
        """Speech and transcript events: log the caller's speech, cut the bot short when the caller interrupts it
        (once the greeting is over), and save every finished caller / assistant transcript line."""
        if openai_event.get("type") == "input_audio_buffer.speech_started":
            self.call_log.record_call_event("speech", "🎤 User started speaking", True)
            if self.last_assistant_item_id and not self.is_greeting_in_progress:
                await self._truncate_interrupted_response()

        elif openai_event.get("type") == "input_audio_buffer.speech_stopped":
            self.call_log.record_call_event("speech", "🎤 User finished speaking", True)
        # Handle user input transcription
        elif openai_event.get("type") == "conversation.item.input_audio_transcription.delta":
            if "delta" in openai_event:
                self.caller_transcript_parts.append(openai_event["delta"])

        elif openai_event.get("type") == "conversation.item.input_audio_transcription.completed":
            module_logger.info(f"Transcription event: {openai_event.get('type')}")
            if "transcript" in openai_event:  # Changed this condition
                caller_transcript = openai_event["transcript"]  # Use complete transcript
                self.call_log.record_call_event("transcript", f"👤 User: {caller_transcript}", True)
                self._persist_transcript_line(transcript_text=caller_transcript, speaker_role="user")
                self.caller_transcript_parts = []  # Reset for next input

        # Handle assistant response transcription
        elif openai_event.get("type") == "response.output_audio_transcript.delta":
            if "delta" in openai_event:
                self.assistant_transcript_parts.append(openai_event["delta"])

        elif openai_event.get("type") == "response.output_audio_transcript.done":
            if self.assistant_transcript_parts:
                assistant_transcript = "".join(self.assistant_transcript_parts)
                self.call_log.record_call_event("transcript", f"🤖 Assistant: {assistant_transcript}", True)
                self._persist_transcript_line(transcript_text=assistant_transcript, speaker_role="assistant")
                self.assistant_transcript_parts = []  # Reset for next response
                # The greeting is over: from now on the caller can interrupt the bot.
                if self.is_greeting_in_progress:
                    self.is_greeting_in_progress = False

    async def _send_response_mark(self) -> None:
        """Send mark event to track response parts."""
        if self.twilio_stream_id:
            response_mark_event = {
                "event": "mark",
                "streamSid": self.twilio_stream_id,
                "mark": {"name": "responsePart"},
            }
            await self.twilio_websocket.send_json(response_mark_event)
            self.pending_marks.append("responsePart")

    async def _forward_audio_delta(self, openai_event: dict) -> bool:
        """Send one ``response.output_audio.delta`` to Twilio and mark it; returns False when the pump must stop."""
        # Check if Twilio websocket is still open before sending.
        # Starlette's WebSocketState has no CLOSED member, so this test is always true and the else branch never
        # runs; what stops the pump is the failed send_json. Kept as it was (changing it is a separate decision).
        if self.twilio_websocket.client_state.name != "CLOSED":
            outbound_audio_base64 = base64.b64encode(base64.b64decode(openai_event["delta"])).decode("utf-8")
            media_event = {
                "event": "media",
                "streamSid": self.twilio_stream_id,
                "media": {"payload": outbound_audio_base64},
            }
            try:
                await self.twilio_websocket.send_json(media_event)
            except Exception as send_error:
                self.call_log.record_call_event("error", f"❌ Failed to send audio to Twilio: {str(send_error)}", True)
                return False  # Stop the pump if we can't send to Twilio

            if self.response_start_timestamp_ms is None:
                self.response_start_timestamp_ms = self.latest_media_timestamp_ms

            if openai_event.get("item_id"):
                self.last_assistant_item_id = openai_event["item_id"]

            try:
                await self._send_response_mark()
            except Exception as mark_error:
                self.call_log.record_call_event("error", f"❌ Failed to send mark to Twilio: {str(mark_error)}", True)
        else:
            self.call_log.record_call_event("system", "Twilio WebSocket closed, stopping audio transmission", True)
            return False  # Stop the pump if Twilio is closed
        return True

    async def _end_call_on_request(self, end_call_error: EndCall) -> None:
        """The hangup_call tool raised EndCall: close the OpenAI socket, send Twilio the ``end`` event, close Twilio."""
        module_logger.error(f"End call requested by user: {str(end_call_error)}")
        self.call_log.record_call_event("system", "End call requested by user", True)
        try:
            await self.realtime_websocket.close()
        except Exception as openai_close_error:
            self.call_log.record_call_event("error", f"❌ Error: {str(openai_close_error)}", True)
        try:
            await self.twilio_websocket.send_json({"event": "end", "streamSid": self.twilio_stream_id})
        except Exception as end_event_error:
            self.call_log.record_call_event("error", f"❌ Error: {str(end_event_error)}", True)
            module_logger.error(f"Error details: {str(end_event_error)}")
        try:
            await self.twilio_websocket.close()
        except Exception as twilio_close_error:
            self.call_log.record_call_event("error", f"❌ Error: {str(twilio_close_error)}", True)
            module_logger.error(f"Error details: {str(twilio_close_error)}")

    async def pump_openai_events_to_twilio(self) -> None:
        """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
        try:
            module_logger.info("Starting send_to_twilio handler")
            async for raw_openai_message in self.realtime_websocket:
                openai_event = json.loads(raw_openai_message)

                # Log all events except audio deltas
                if openai_event.get("type") not in ["response.output_audio.delta"]:
                    if openai_event.get("type") == "error":
                        module_logger.error(f"Error details: {json.dumps(openai_event, indent=2)}")

                # Handle function calls
                if openai_event.get("type") == "response.function_call_arguments.done":
                    await self._dispatch_tool_call(openai_event)

                # Log important events
                await self._track_transcripts(openai_event)

                # Print debug information for transcription events
                if openai_event.get("type") in [
                    "conversation.item.input_audio_transcription.delta",
                    "conversation.item.input_audio_transcription.completed",
                ]:
                    module_logger.info(f"Transcription event: {openai_event.get('type')}")
                    module_logger.info(f"Response content: {openai_event}")

                # Handle audio responses
                if openai_event.get("type") == "response.output_audio.delta" and "delta" in openai_event:
                    if not await self._forward_audio_delta(openai_event):
                        break  # Exit the loop: Twilio can no longer receive audio

            # If we exit the loop normally (not by exception), it means OpenAI disconnected
            self.call_log.record_call_event("system", "OpenAI WebSocket disconnected normally", True)

        except EndCall as end_call_error:
            await self._end_call_on_request(end_call_error)
        except Exception as error:
            self.call_log.record_call_event("error", f"❌ Error in send_to_twilio: {str(error)}", True)
            module_logger.error(f"Error details: {str(error)}")
        finally:
            # Always close OpenAI WebSocket when pump_openai_events_to_twilio ends
            await _close_openai_socket(
                self.realtime_websocket,
                self.call_log,
                "OpenAI WebSocket closed from send_to_twilio",
                "❌ Error closing OpenAI WebSocket",
            )

    async def run(self) -> None:
        """Run the two pumps concurrently until both have ended."""
        # Start both handlers
        await asyncio.gather(self.pump_twilio_audio_to_openai(), self.pump_openai_events_to_twilio())


@router.websocket("/media-stream/{tenant_name}/{from_number}/{called_number}")
async def bridge_twilio_stream_to_realtime(websocket: WebSocket, tenant_name: str, from_number: str, called_number: str):
    """Handle WebSocket connections between Twilio and OpenAI."""
    #  ######### Dynamic Initialization ##########
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        module_logger.error(f"ERROR: Tenant {tenant_name} not found")
        return
    call_settings = _ensure_call_settings(tenant_name)
    phone_prompt_text = _load_realtime_instructions(tenant_name)

    _customer = ensure_customer(from_number, tenant_name, customer_profile_name=from_number, platform="telephone")
    _caller_display_name = _customer.name or from_number
    ###########################################
    module_logger.info("=== New WebSocket Connection ===")
    await websocket.accept()
    call_log = SimpleLogger()
    realtime_websocket = None
    message_context = build_realtime_message_context(
        tenant_name=tenant_name, from_number=from_number, called_number=called_number, call_settings=call_settings
    )
    tenant_chatbot_service = build_tenant_chatbot(tenant_name)

    try:
        # GA Realtime endpoint (no ``OpenAI-Beta`` header).
        async with websockets.connect(
            tenant_chatbot_service.build_realtime_websocket_url(resolve_realtime_model_name()),
            extra_headers={"Authorization": f"Bearer {tenant_chatbot_service.api_key}"},
        ) as realtime_websocket:
            await configure_realtime_session(
                realtime_websocket, phone_prompt_text, call_settings, tenant_name, message_context
            )
            call_bridge = _RealtimeCallBridge(
                twilio_websocket=websocket,
                realtime_websocket=realtime_websocket,
                message_context=message_context,
                call_log=call_log,
                tenant_name=tenant_name,
                from_number=from_number,
                called_number=called_number,
            )
            await call_bridge.run()

    except Exception as error:
        call_log.record_call_event("error", f"❌ Main handler error: {str(error)}", True)
    finally:
        await _shutdown_call_sockets(realtime_websocket, websocket, call_log)
