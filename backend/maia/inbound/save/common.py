"""Helpers shared by the save handlers: chat settings, unsubscribe handling, voice-note transcription, image OCR, thread ids."""

import logging
import os
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

import requests

from maia.models.customers import CustomerDocument
from maia.models.conversations import Conversation
from maia.models.settings import ChatSettingsDocument
from maia.utils.read_csv import read_csv
from maia.utils.read_image import read_image
from maia.utils.read_txt import read_txt
from maia.utils.read_word import read_word
from maia.utils.read_xlsx import read_xlsx
from maia.utils.transcribe_audio import transcribe_audio_file

logger = logging.getLogger(__name__)


@dataclass
class SaveResponse:
    tenant_name: Optional[str] = None
    delay: Optional[int] = None
    user_id: Optional[str] = None
    task_message_id: Optional[str] = None
    single_message_mode: bool = False
    conversation_id: Optional[str] = None


def retrieve_human_delay_setting(chat_settings: ChatSettingsDocument) -> int:
    return chat_settings.chatbot_human_like_time if chat_settings.chatbot_human_like_delay else 0


def get_or_create_chat_settings(tenant_name):
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if not chat_settings:
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name)
        chat_settings.save()
    return chat_settings


def update_customer_status_on_unsubscribe(message: Optional[str], customer: CustomerDocument):
    if message and message.lower() in [
        "remove me",
        "delete me",
        "unsubscribe",
        "הסר אותי",
        "מחק אותי",
        "הסר",
        "מחק",
        "stop",
        "delete",
        "remove",
    ]:
        customer.status = "לא מאושר דיוור"
        customer.save()
        return "Customer status updated"
    return None


def handle_audio_message(media_url, message_type, tenant_name) -> Optional[str]:
    """Download a voice note and transcribe it with OpenAI. Returns None when it is not audio."""
    if message_type != "audio" and not (media_url and media_url.lower().endswith((".mp3", ".ogg"))):
        return None
    response = requests.get(media_url)
    if response.status_code != 200:
        return None
    content_type = response.headers.get("content-type", "")
    extension = ".ogg" if ("ogg" in content_type or media_url.lower().endswith(".ogg")) else ".mp3"
    unique_filename = f"/tmp/{uuid4().hex}{extension}"
    with open(unique_filename, "wb") as f:
        f.write(response.content)
    try:
        return transcribe_audio_file(unique_filename, tenant_name)
    finally:
        if os.path.exists(unique_filename):
            os.remove(unique_filename)


def extract_text_from_media(
    media_url: str,
    message_type: str,
    message_content_type: str,
    chat_settings: ChatSettingsDocument,
) -> Optional[str]:
    file_text = ""
    if (
        message_content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ) or message_type == "docx":
        file_text = read_word(media_url)
    elif (
        message_content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ) or message_type == "xlsx":
        file_text = read_xlsx(media_url)
    elif message_type == "csv":
        file_text = read_csv(media_url)
    elif message_content_type == "text/plain" or message_type == "txt":
        file_text = read_txt(media_url)
    elif message_content_type in ["image/jpeg", "image/png", "image/webp"] or message_type == "image":
        if chat_settings.get_feature("image_to_text").enabled:
            file_text = read_image(media_url)

    if message_content_type == "video/mp4" or message_type == "mp4":
        file_text += " Video file include but not supported to read"
    return file_text


def get_or_create_conversation_for_thread_id(tenant_name: str, phone_number: str, platform: str, thread_id: str) -> str:
    """
    Find the conversation tagged with thread_id, or create a new dedicated one.
    This guarantees that each thread_id always routes to its own conversation,
    even when the thread_id changes mid-session.
    Returns the conversation_id (str).
    """
    from datetime import datetime, timezone

    if not thread_id:
        return None
    conv = Conversation.objects(
        tenant_name=tenant_name,
        phone_number=phone_number,
        meta_data__thread_id=thread_id,
    ).first()
    if conv:
        return str(conv.id)
    # No conversation yet for this thread_id — create one tagged with it.
    conv = Conversation(
        tenant_name=tenant_name,
        phone_number=phone_number,
        platform=platform,
        date=datetime.now(timezone.utc),
        conversation_status="online",
        meta_data={"thread_id": thread_id},
    )
    conv.save()
    return str(conv.id)


def ensure_thread_id_in_conversation(conversation_id: str, thread_id: str):
    """
    After a message is saved, make sure its conversation has thread_id stored in meta_data
    so future messages with the same thread_id can find it.
    """
    if not thread_id or not conversation_id:
        return
    try:
        from bson import ObjectId

        if not ObjectId.is_valid(conversation_id):
            return
        conv = Conversation.objects(id=conversation_id).first()
        if conv:
            if conv.meta_data is None:
                conv.meta_data = {}
            existing = conv.meta_data.get("thread_id")
            if existing != thread_id:
                conv.meta_data["thread_id"] = thread_id
                conv._mark_as_changed("meta_data")
                if thread_id not in [tg.name for tg in conv.tags]:
                    from maia.models.conversations import Tag

                    conv.tags.append(Tag(name=thread_id))
                conv.save()
                logger.info(f"[thread] Tagged conversation {conversation_id} with thread_id={thread_id}")
    except Exception as e:
        logger.error(f"Failed to store thread_id in conversation: {e}")


def update_conversation_profile_name(
    tenant_name: str, phone_number: str, profile_name: str, conversation_id: str = None
):
    """Update the profile_name on the conversation if not already set."""
    if not profile_name:
        return
    if conversation_id:
        from bson import ObjectId

        if ObjectId.is_valid(conversation_id):
            conv = Conversation.objects(id=conversation_id).first()
        else:
            conv = None
    else:
        conv = None
    if not conv:
        conv = Conversation.objects(
            tenant_name=tenant_name,
            phone_number=phone_number,
            conversation_status="online",
        ).first()
    if conv and not conv.profile_name:
        conv.profile_name = profile_name
        conv.save()
