"""Helpers shared by the save handlers: chat settings, unsubscribe handling, voice-note transcription, image OCR, thread ids."""

import logging
import os
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

import requests

from maia.models.conversations import Conversation, build_online_conversation_filter
from maia.models.settings import ChatSettingsDocument
from maia.utils.read_csv import extract_text_from_csv
from maia.utils.read_image import extract_text_from_image
from maia.utils.read_txt import extract_text_from_txt
from maia.utils.read_word import extract_text_from_word
from maia.utils.read_xlsx import extract_text_from_xlsx
from maia.utils.transcribe_audio import transcribe_voice_note

logger = logging.getLogger(__name__)


@dataclass
class SaveResponse:
    """What a save handler returns to the dispatcher: who to answer (sender_id) and how; an empty response means no
    reply.
    """
    tenant_name: Optional[str] = None
    reply_delay_seconds: Optional[int] = None
    sender_id: Optional[str] = None
    triggering_message_id: Optional[str] = None
    conversation_id: Optional[str] = None


def resolve_human_like_delay_seconds(chat_settings: ChatSettingsDocument) -> int:
    """Seconds the bot waits before answering (0 when the human-like delay is off)."""
    return chat_settings.chatbot_human_like_time if chat_settings.chatbot_human_like_delay else 0


def ensure_tenant_chat_settings(tenant_name):
    """The tenant's chat settings, created with defaults when missing."""
    chat_settings_doc = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if not chat_settings_doc:
        chat_settings_doc = ChatSettingsDocument(tenant_name=tenant_name)
        chat_settings_doc.save()
    return chat_settings_doc


def _download_voice_note_to_temporary_file(attachment_url) -> Optional[str]:
    """Download the voice note to /tmp/<uuid4>.ogg or .mp3 (from the response content type or the url suffix) and
    return the file path, written and closed; None when the download does not answer 200. The caller deletes the file.
    """
    download_response = requests.get(attachment_url)
    if download_response.status_code != 200:
        return None
    response_content_type = download_response.headers.get("content-type", "")
    audio_file_extension = (
        ".ogg" if ("ogg" in response_content_type or attachment_url.lower().endswith(".ogg")) else ".mp3"
    )
    temporary_audio_path = f"/tmp/{uuid4().hex}{audio_file_extension}"
    with open(temporary_audio_path, "wb") as audio_file:
        audio_file.write(download_response.content)
    return temporary_audio_path


def download_and_transcribe_voice_note(attachment_url, attachment_kind, tenant_name) -> Optional[str]:
    """Download a voice note and transcribe it with OpenAI. Returns None when it is not audio."""
    if attachment_kind != "audio" and not (attachment_url and attachment_url.lower().endswith((".mp3", ".ogg"))):
        return None
    temporary_audio_path = _download_voice_note_to_temporary_file(attachment_url)
    if temporary_audio_path is None:
        return None
    try:
        return transcribe_voice_note(temporary_audio_path, tenant_name)
    finally:
        if os.path.exists(temporary_audio_path):
            os.remove(temporary_audio_path)


def extract_text_from_attachment(
    attachment_url: str,
    attachment_kind: str,
    attachment_content_type: str,
    chat_settings: ChatSettingsDocument,
) -> Optional[str]:
    """Text of an attached file for the model: DOCX, XLSX, CSV, TXT, and images through OCR when the image_to_text
    feature is on; a video only gets a note.
    """
    extracted_text = ""
    if (
        attachment_content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ) or attachment_kind == "docx":
        extracted_text = extract_text_from_word(attachment_url)
    elif (
        attachment_content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ) or attachment_kind == "xlsx":
        extracted_text = extract_text_from_xlsx(attachment_url)
    elif attachment_kind == "csv":
        extracted_text = extract_text_from_csv(attachment_url)
    elif attachment_content_type == "text/plain" or attachment_kind == "txt":
        extracted_text = extract_text_from_txt(attachment_url)
    elif attachment_content_type in ["image/jpeg", "image/png", "image/webp"] or attachment_kind == "image":
        if chat_settings.ensure_feature("image_to_text").enabled:
            extracted_text = extract_text_from_image(attachment_url)

    if attachment_content_type == "video/mp4" or attachment_kind == "mp4":
        extracted_text += " Video file include but not supported to read"
    return extracted_text


def _find_conversation_by_id(conversation_id: Optional[str]) -> Optional[Conversation]:
    """The conversation with this id; None when the id is empty, is not a valid ObjectId (checked before any query,
    so mongoengine never sees an invalid id) or matches no conversation.
    """
    if not conversation_id:
        return None
    from bson import ObjectId

    if not ObjectId.is_valid(conversation_id):
        return None
    return Conversation.objects(id=conversation_id).first()


def persist_conversation_thread_id(conversation_id: str, email_thread_id: str):
    """
    After a message is saved, make sure its conversation has thread_id stored in meta_data
    so future messages with the same thread_id can find it.
    """
    if not email_thread_id or not conversation_id:
        return
    try:
        conversation_doc = _find_conversation_by_id(conversation_id)
        if conversation_doc:
            if conversation_doc.meta_data is None:
                conversation_doc.meta_data = {}
            stored_thread_id = conversation_doc.meta_data.get("thread_id")
            if stored_thread_id != email_thread_id:
                conversation_doc.meta_data["thread_id"] = email_thread_id
                conversation_doc._mark_as_changed("meta_data")
                conversation_doc.save()
                logger.info(f"[thread] Stored thread_id={email_thread_id} on conversation {conversation_id}")
    except Exception as error:
        logger.error(f"Failed to store thread_id in conversation: {error}")


def _find_online_conversation_any_platform(tenant_name: str, sender_id: str) -> Optional[Conversation]:
    """The customer's online conversation in this tenant on ANY platform (deliberately no platform in the filter:
    a name learned on one channel also fills the conversation of the same key on another), or None.
    """
    return Conversation.objects(**build_online_conversation_filter(tenant_name, sender_id)).first()


def fill_missing_conversation_profile_name(
    tenant_name: str, sender_id: str, customer_profile_name: str, conversation_id: str = None
):
    """Update the profile_name on the conversation if not already set."""
    if not customer_profile_name:
        return
    conversation_doc = _find_conversation_by_id(conversation_id)
    if not conversation_doc:
        conversation_doc = _find_online_conversation_any_platform(tenant_name, sender_id)
    if conversation_doc and not conversation_doc.profile_name:
        conversation_doc.profile_name = customer_profile_name
        conversation_doc.save()
