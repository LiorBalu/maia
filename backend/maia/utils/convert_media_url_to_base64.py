"""Turns a message's media URL into the LangChain multimodal content block (base64 image)."""

import base64
import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Maximum size in bytes (25MB for audio)
AUDIO_SIZE_WARNING_THRESHOLD_BYTES = 25 * 1024 * 1024


def _infer_audio_mime_type(media_url: str) -> str:
    """Guess the MIME type from the URL's extension, for a download that came without a Content-Type.

    Returns audio/mpeg, audio/wav, audio/ogg, audio/mp4 or audio/webm; audio/mpeg when nothing matches.
    """
    lowercase_media_url = media_url.lower()
    if lowercase_media_url.endswith(".mp3"):
        audio_mime_type = "audio/mpeg"
    elif lowercase_media_url.endswith(".wav"):
        audio_mime_type = "audio/wav"
    elif lowercase_media_url.endswith(".ogg"):
        audio_mime_type = "audio/ogg"
    elif lowercase_media_url.endswith(".m4a"):
        audio_mime_type = "audio/mp4"
    elif lowercase_media_url.endswith(".webm"):
        audio_mime_type = "audio/webm"
    else:
        audio_mime_type = "audio/mpeg"  # default fallback
    return audio_mime_type


def _warn_if_audio_too_large(audio_bytes: bytes) -> None:
    """Log a warning when the audio is above AUDIO_SIZE_WARNING_THRESHOLD_BYTES; it is never rejected."""
    # Check size limits
    if len(audio_bytes) > AUDIO_SIZE_WARNING_THRESHOLD_BYTES:
        logger.warning(f"Audio size {len(audio_bytes)} bytes exceeds {AUDIO_SIZE_WARNING_THRESHOLD_BYTES} bytes limit")
        logger.warning("Audio file is large, proceeding anyway...")


def _fetch_audio_data_url(media_url: str, timeout_seconds: int) -> str:
    """Download the media and return it as a 'data:<mime>;base64,<payload>' URL.

    Every non-image kind passed to fetch_media_as_data_url lands here (video and documents too,
    not only audio), and it is labelled audio/* when the server sends no Content-Type.
    Exceptions propagate to fetch_media_as_data_url, which logs them and returns None.
    """
    download_response = requests.get(media_url, timeout=timeout_seconds)
    download_response.raise_for_status()

    # Get the content type from the response headers
    audio_mime_type = download_response.headers.get("Content-Type", "")

    # If content type is not set, try to infer from URL
    if not audio_mime_type:
        audio_mime_type = _infer_audio_mime_type(media_url)

    audio_bytes = download_response.content

    _warn_if_audio_too_large(audio_bytes)

    # Encode the media content to base64
    audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")

    # Return the data URL
    return f"data:{audio_mime_type};base64,{audio_base64}"


def fetch_media_as_data_url(media_url: str, media_kind: str = "image", timeout_seconds: int = 10) -> Optional[str]:
    """
    Convert a media URL (image or audio) to a base64-encoded data URL.

    Args:
        media_url: The HTTP/HTTPS URL of the media file
        media_kind: Type of media - "image" or "audio"
        timeout_seconds: Request timeout in seconds (default: 10)

    Returns:
        A base64-encoded data URL string (e.g., 'data:image/jpeg;base64,/9j/4AAQ...')
        or None if the conversion fails

    Note:
        For images, delegates to fetch_image_as_data_url which handles resizing.
        For audio, downloads and encodes directly.
    """
    # For images, use the specialized function with compression
    if media_kind == "image":
        # lazy: PIL is only needed for images
        from maia.utils.convert_image_url_to_base64 import fetch_image_as_data_url

        return fetch_image_as_data_url(media_url, timeout_seconds=timeout_seconds)

    # For audio, download and encode
    try:
        return _fetch_audio_data_url(media_url, timeout_seconds)
    except requests.exceptions.RequestException as download_error:
        logger.error(f"Failed to download media from {media_url}: {download_error}")
        return None
    except Exception as error:
        logger.error(f"Failed to convert media URL to base64: {error}")
        return None


def _split_data_url(data_url: str) -> Optional[tuple[str, str]]:
    """Split a 'data:<mime>;base64,<payload>' URL into (mime_type, base64_payload).

    Returns None, after logging the error, when the URL does not split into exactly two parts.
    """
    # Extract mime_type and base64 data
    data_url_parts = data_url.split(";base64,")
    if len(data_url_parts) != 2:
        logger.error("Invalid base64 data URL format")
        return None

    mime_type = data_url_parts[0].replace("data:", "")
    base64_payload = data_url_parts[1]
    return mime_type, base64_payload


def build_media_content_block(media_url: str, media_kind: str, timeout_seconds: int = 10) -> Optional[Dict[str, Any]]:
    """
    Convert a media URL to LangChain standard format compatible with OpenAI API.

    Args:
        media_url: The HTTP/HTTPS URL of the media file
        media_kind: Type of media - "image" or "audio"
        timeout_seconds: Request timeout in seconds (default: 10)

    Returns:
        Dict in LangChain/OpenAI format:
        - For images: {"type": "image", "base64": "...", "mime_type": "image/jpeg"}
        - For audio: None (audio is transcribed to text upstream via download_and_transcribe_voice_note;
          storing raw audio as input_audio in Redis history causes OpenAI Chat Completions
          API to return 400 since input_audio is only valid in the Realtime API)
        Returns None if conversion fails
    """
    if media_kind == "audio":
        # Audio is already transcribed to text by download_and_transcribe_voice_note() before this point.
        # Do NOT store raw audio in Redis history — input_audio blocks are not supported
        # by the OpenAI Chat Completions API and cause 400 errors.
        return None

    media_data_url = fetch_media_as_data_url(media_url, media_kind=media_kind, timeout_seconds=timeout_seconds)

    if not media_data_url or not media_data_url.startswith("data:"):
        logger.error(f"Failed to convert {media_kind} to base64")
        return None

    parsed_data_url = _split_data_url(media_data_url)
    if parsed_data_url is None:
        return None
    mime_type, base64_payload = parsed_data_url

    if media_kind == "image":
        return {
            "type": "image",
            "base64": base64_payload,
            "mime_type": mime_type,
        }

    return None
