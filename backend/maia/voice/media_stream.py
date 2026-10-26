"""The phone call itself: Twilio media stream (mu-law audio) bridged to the OpenAI Realtime session, with tool calls and hand-off."""

import logging
import asyncio
import base64

# from twilio.rest import Client  # Currently unused - commented out manager logic
import datetime
import json

import websockets
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from maia.pipeline.context import MessageContext
from maia.services.registry import chatbot_for
from maia.models.customers import fetch_or_create_customer
from maia.models.messages import MessageDocument
from maia.models.prompts import PromptDocument
from maia.models.tenants import TenantDocument
from maia.models.settings import CallSettingsDocument, ChatSettingsDocument
from maia.utils.conversation.get_or_create import get_or_create_online_conversation
from maia.voice.call_control import CallManager, EndCall, handle_function_called
from maia.voice.session import initialize_session, realtime_model
from maia.voice.audio_preprocessing import build_preprocessor_from_settings
from maia.voice.logger import SimpleLogger

log = logging.getLogger(__name__)

router = APIRouter()


def build_message_context(tenant_name, from_number, called_number, call_settings) -> MessageContext:
    """Build the pipeline's ``MessageContext`` for a Realtime call session (no inbound text yet)."""
    customer = fetch_or_create_customer(
        number_phone=from_number, tenant_name=tenant_name, profile_name=from_number, platform="telephone"
    )
    # Prefer the stored customer name (e.g. passed via start-call) over the raw number.
    sender_name = customer.name or from_number
    tenant = TenantDocument.objects(name=tenant_name).first()
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if not chat_settings:
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name).save()
    conversation = get_or_create_online_conversation(tenant_name, from_number, "telephone", sender_name)
    return MessageContext(
        tenant_name=tenant_name,
        platform="telephone",
        customer=customer,
        tenant=tenant,
        conversation=conversation,
        chat_settings=chat_settings,
        sender_id=from_number,
        recipient_id=called_number,
        sender_name=sender_name,
        message_text="",
        message_id="",
        message_type=None,
        media_url=None,
        media_content_type=None,
        extra_prompt_name=None,
        additional_context="",
        meta_data={},
        tool_names=[],
        tool_schemas=[],
        tool_results={},
    )


@router.websocket("/media-stream/{tenant_name}/{from_number}/{called_number}")
async def handle_media_stream(websocket: WebSocket, tenant_name: str, from_number: str, called_number: str):
    """Handle WebSocket connections between Twilio and OpenAI."""
    #  ######### Dynamic Initialization ##########
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        log.error(f"ERROR: Tenant {tenant_name} not found")
        return
    call_settings = CallSettingsDocument.objects(tenant_name=tenant_name).first()
    if not call_settings:
        call_settings = CallSettingsDocument(tenant_name=tenant_name).save()
    prompt_doc = PromptDocument.objects(tenant_name=tenant_name, name="platform_realtime").first()
    if not prompt_doc:
        prompt_doc = PromptDocument(tenant_name=tenant_name, name="platform_realtime", content="").save()
    initialize_session_instructions = prompt_doc.prompt

    _customer = fetch_or_create_customer(from_number, tenant_name, profile_name=from_number, platform="telephone")
    _customer_name = _customer.name or from_number
    ###########################################
    log.info("=== New WebSocket Connection ===")
    await websocket.accept()
    logger = SimpleLogger()
    openai_ws = None
    request = build_message_context(
        tenant_name=tenant_name, from_number=from_number, called_number=called_number, call_settings=call_settings
    )
    chatbot_service = chatbot_for(tenant_name)

    try:
        # GA Realtime endpoint (no ``OpenAI-Beta`` header).
        async with websockets.connect(
            chatbot_service.realtime_ws_url(realtime_model()),
            extra_headers={"Authorization": f"Bearer {chatbot_service.api_key}"},
        ) as openai_ws:
            await initialize_session(
                openai_ws, initialize_session_instructions, call_settings, tenant_name, request, is_first_message=True
            )

            # Connection specific state
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None
            is_first_assistant_response = True

            # Per-call audio preprocessor (noise suppression / AGC / VAD gate).
            audio_preprocessor = build_preprocessor_from_settings(call_settings)

            current_time = None

            async def receive_from_twilio():
                """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                nonlocal stream_sid, latest_media_timestamp
                nonlocal current_time, response_start_timestamp_twilio, last_assistant_item
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data["event"] == "media" and openai_ws and openai_ws.open:
                            latest_media_timestamp = int(data["media"]["timestamp"])
                            payload_b64 = data["media"]["payload"]
                            if audio_preprocessor is not None:
                                payload_b64 = audio_preprocessor.process(payload_b64)
                            audio_append = {"type": "input_audio_buffer.append", "audio": payload_b64}

                            await openai_ws.send(json.dumps(audio_append))


                        elif data["event"] == "start":
                            stream_sid = data["start"]["streamSid"]
                            if audio_preprocessor is not None:
                                log.info(f"NOISE_SUPPRESSION_STREAM_ATTACHED provider=openai_realtime stream_sid={stream_sid} "
                                    f"tenant={tenant_name}")
                            logger.log_event("system", f"✓ Stream started: {stream_sid}", True)
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                        elif data["event"] == "mark":
                            if mark_queue:
                                mark_queue.pop(0)
                        elif data["event"] == "stop":
                            logger.log_event("system", "Call ended by Twilio", True)
                            break

                    # If we exit the loop normally (not by exception), it means Twilio disconnected
                    logger.log_event("system", "Twilio WebSocket disconnected normally", True)

                except WebSocketDisconnect:
                    logger.log_event("system", "Client disconnected (WebSocketDisconnect)", True)
                except Exception as e:
                    logger.log_event("error", f"❌ Error in receive_from_twilio: {str(e)}", True)
                finally:
                    if audio_preprocessor is not None:
                        summary = audio_preprocessor.stats_summary()
                        log.info(f"NOISE_SUPPRESSION_SUMMARY provider=openai_realtime tenant={tenant_name} "
                            f"stream_sid={stream_sid or '-'} {summary}")
                    # Always close OpenAI WebSocket when Twilio disconnects
                    if openai_ws and openai_ws.open:
                        try:
                            await openai_ws.close()
                            logger.log_event("system", "OpenAI WebSocket closed from receive_from_twilio", True)
                        except Exception as e:
                            logger.log_event("error", f"❌ Error closing OpenAI WebSocket: {str(e)}", True)

            async def send_to_twilio():
                """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
                nonlocal stream_sid, current_time
                nonlocal last_assistant_item, response_start_timestamp_twilio, is_first_assistant_response
                current_response = []
                current_user_input = []
                try:
                    log.info("Starting send_to_twilio handler")
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)

                        # Log all events except audio deltas
                        if response.get("type") not in ["response.output_audio.delta"]:
                            if response.get("type") == "error":
                                log.error(f"Error details: {json.dumps(response, indent=2)}")

                        # Handle function calls
                        if response.get("type") == "response.function_call_arguments.done":
                            log.info(f"Function call details: {json.dumps(response, indent=2)}")
                            function_name = response.get("name")
                            arguments = json.loads(response.get("arguments", "{}"))

                            log.info("✓ Handling function call: " + function_name)
                            await handle_function_called(
                                function_name=function_name,
                                arguments=arguments,
                                openai_ws=openai_ws,
                                websocket=websocket,
                                stream_sid=stream_sid,
                                logger=logger,
                                call_id=response.get("call_id"),
                                request=request,
                            )

                        # Log important events
                        if response.get("type") == "input_audio_buffer.speech_started":
                            logger.log_event("speech", "🎤 User started speaking", True)
                            if last_assistant_item and not is_first_assistant_response:
                                await handle_speech_started_event()

                        elif response.get("type") == "input_audio_buffer.speech_stopped":
                            logger.log_event("speech", "🎤 User finished speaking", True)
                        # Handle user input transcription
                        elif response.get("type") == "conversation.item.input_audio_transcription.delta":
                            if "delta" in response:
                                current_user_input.append(response["delta"])

                        elif response.get("type") == "conversation.item.input_audio_transcription.completed":
                            log.info(f"Transcription event: {response.get('type')}")
                            if "transcript" in response:  # Changed this condition
                                full_user_input = response["transcript"]  # Use complete transcript
                                logger.log_event("transcript", f"👤 User: {full_user_input}", True)
                                record_user_message(text=full_user_input, role="user")
                                current_user_input = []  # Reset for next input

                        # Handle assistant response transcription
                        elif response.get("type") == "response.output_audio_transcript.delta":
                            if "delta" in response:
                                current_response.append(response["delta"])

                        elif response.get("type") == "response.output_audio_transcript.done":
                            if current_response:
                                full_response = "".join(current_response)
                                logger.log_event("transcript", f"🤖 Assistant: {full_response}", True)
                                record_user_message(text=full_response, role="assistant")
                                current_response = []  # Reset for next response
                                # Update session configuration after first response
                                if is_first_assistant_response:
                                    is_first_assistant_response = False

                                    # # Lower threshold after first message
                                    # await initialize_session(
                                    #     openai_ws,
                                    #     initialize_session_instructions,
                                    #     call_settings,
                                    #     tenant_name,
                                    #     request,
                                    #     is_first_message=False,
                                    # )

                        # Print debug information for transcription events
                        if response.get("type") in [
                            "conversation.item.input_audio_transcription.delta",
                            "conversation.item.input_audio_transcription.completed",
                        ]:
                            log.info(f"Transcription event: {response.get('type')}")
                            log.info(f"Response content: {response}")

                        # Handle audio responses
                        if response.get("type") == "response.output_audio.delta" and "delta" in response:
                            # Check if Twilio websocket is still open before sending
                            if websocket.client_state.name != "CLOSED":
                                audio_payload = base64.b64encode(base64.b64decode(response["delta"])).decode("utf-8")
                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": audio_payload},
                                }
                                try:
                                    await websocket.send_json(audio_delta)
                                except Exception as e:
                                    logger.log_event("error", f"❌ Failed to send audio to Twilio: {str(e)}", True)
                                    break  # Exit the loop if we can't send to Twilio

                                if response_start_timestamp_twilio is None:
                                    response_start_timestamp_twilio = latest_media_timestamp

                                if response.get("item_id"):
                                    last_assistant_item = response["item_id"]

                                try:
                                    await send_mark(websocket, stream_sid)
                                except Exception as e:
                                    logger.log_event("error", f"❌ Failed to send mark to Twilio: {str(e)}", True)
                            else:
                                logger.log_event("system", "Twilio WebSocket closed, stopping audio transmission", True)
                                break  # Exit the loop if Twilio is closed

                    # If we exit the loop normally (not by exception), it means OpenAI disconnected
                    logger.log_event("system", "OpenAI WebSocket disconnected normally", True)

                except CallManager:
                    # i want stop the websocket stream and call to manager
                    log.error("CallManager exception triggered: handoff logic invoked.")
                    logger.log_event("system", "CallManager exception: hand to manager", True)
                    try:
                        await websocket.send_json({"event": "end", "streamSid": stream_sid})
                    except Exception as ex:
                        logger.log_event("error", f"❌ Sending 'end' event failed: {str(ex)}", True)
                    try:
                        if openai_ws.open:
                            await openai_ws.close()
                    except Exception as ex:
                        logger.log_event("error", f"❌ Error closing openai_ws: {str(ex)}", True)
                    try:
                        await websocket.close()
                    except Exception as ex:
                        logger.log_event("error", f"❌ Error closing Twilio websocket: {str(ex)}", True)

                    logger.log_event("system", "Call handed off to manager", True)
                    return

                except EndCall as e:
                    log.error(f"End call requested by user: {str(e)}")
                    logger.log_event("system", "End call requested by user", True)
                    try:
                        await openai_ws.close()
                    except Exception as e:
                        logger.log_event("error", f"❌ Error: {str(e)}", True)
                    try:
                        await websocket.send_json({"event": "end", "streamSid": stream_sid})
                    except Exception as e:
                        logger.log_event("error", f"❌ Error: {str(e)}", True)
                        log.error(f"Error details: {str(e)}")
                    try:
                        await websocket.close()
                    except Exception as e:
                        logger.log_event("error", f"❌ Error: {str(e)}", True)
                        log.error(f"Error details: {str(e)}")
                except Exception as e:
                    logger.log_event("error", f"❌ Error in send_to_twilio: {str(e)}", True)
                    log.error(f"Error details: {str(e)}")
                finally:
                    # Always close OpenAI WebSocket when send_to_twilio ends
                    if openai_ws and openai_ws.open:
                        try:
                            await openai_ws.close()
                            logger.log_event("system", "OpenAI WebSocket closed from send_to_twilio", True)
                        except Exception as e:
                            logger.log_event("error", f"❌ Error closing OpenAI WebSocket: {str(e)}", True)

            def record_user_message(text: str, role="user"):
                msg = MessageDocument(
                    tenant_name=tenant_name,
                    message=text,
                    phone_number=from_number,
                    platform="telephone",
                    role=role,
                    profile_name=request["sender_name"] if role == "user" else "AI",
                    date=datetime.datetime.now(datetime.timezone.utc),
                    to=called_number,
                )
                msg.save()

            async def handle_speech_started_event():
                """Handle interruption when the caller's speech starts."""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                logger.log_event("system", "Handling speech started event", True)
                if mark_queue and response_start_timestamp_twilio is not None:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio

                    if last_assistant_item:
                        truncate_event = {
                            "type": "conversation.item.truncate",
                            "item_id": last_assistant_item,
                            "content_index": 0,
                            "audio_end_ms": elapsed_time,
                        }
                        await openai_ws.send(json.dumps(truncate_event))

                    await websocket.send_json({"event": "clear", "streamSid": stream_sid})

                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None

            async def send_mark(connection, stream_sid):
                """Send mark event to track response parts."""
                if stream_sid:
                    mark_event = {"event": "mark", "streamSid": stream_sid, "mark": {"name": "responsePart"}}
                    await connection.send_json(mark_event)
                    mark_queue.append("responsePart")

            # Start both handlers
            handlers = [receive_from_twilio(), send_to_twilio()]
            await asyncio.gather(*handlers)

    except Exception as e:
        logger.log_event("error", f"❌ Main handler error: {str(e)}", True)
    finally:
        # Ensure OpenAI WebSocket is always closed when the main handler ends
        if openai_ws and openai_ws.open:
            try:
                await openai_ws.close()
                logger.log_event("system", "OpenAI WebSocket closed in main finally block", True)
            except Exception as e:
                logger.log_event("error", f"❌ Error closing OpenAI WebSocket in finally: {str(e)}", True)

        # Ensure Twilio WebSocket is closed
        try:
            await websocket.close()
            logger.log_event("system", "Twilio WebSocket closed in main finally block", True)
        except Exception as e:
            logger.log_event("error", f"❌ Error closing Twilio WebSocket in finally: {str(e)}", True)

        logger.log_event("system", "=== Ending WebSocket Connection ===", True)
