"""Turns a message's media URL into the LangChain multimodal content block (base64 image)."""

import base64
import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Maximum size in bytes (25MB for audio)
MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024


def convert_media_url_to_base64(url: str, media_type: str = "image", timeout: int = 10) -> Optional[str]:
    """
    Convert a media URL (image or audio) to a base64-encoded data URL.

    Args:
        url: The HTTP/HTTPS URL of the media file
        media_type: Type of media - "image" or "audio"
        timeout: Request timeout in seconds (default: 10)

    Returns:
        A base64-encoded data URL string (e.g., 'data:image/jpeg;base64,/9j/4AAQ...')
        or None if the conversion fails

    Note:
        For images, delegates to convert_image_url_to_base64 which handles resizing.
        For audio, downloads and encodes directly.
    """
    # For images, use the specialized function with compression
    if media_type == "image":
        from maia.utils.convert_image_url_to_base64 import convert_image_url_to_base64

        return convert_image_url_to_base64(url, timeout=timeout)

    # For audio, download and encode
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()

        # Get the content type from the response headers
        content_type = response.headers.get("Content-Type", "")

        # If content type is not set, try to infer from URL
        if not content_type:
            url_lower = url.lower()
            if url_lower.endswith(".mp3"):
                content_type = "audio/mpeg"
            elif url_lower.endswith(".wav"):
                content_type = "audio/wav"
            elif url_lower.endswith(".ogg"):
                content_type = "audio/ogg"
            elif url_lower.endswith(".m4a"):
                content_type = "audio/mp4"
            elif url_lower.endswith(".webm"):
                content_type = "audio/webm"
            else:
                content_type = "audio/mpeg"  # default fallback

        media_content = response.content

        # Check size limits
        if len(media_content) > MAX_AUDIO_SIZE_BYTES:
            logger.warning(f"Audio size {len(media_content)} bytes exceeds {MAX_AUDIO_SIZE_BYTES} bytes limit")
            logger.warning("Audio file is large, proceeding anyway...")

        # Encode the media content to base64
        base64_media = base64.b64encode(media_content).decode("utf-8")

        # Return the data URL
        return f"data:{content_type};base64,{base64_media}"

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download media from {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to convert media URL to base64: {e}")
        return None


def convert_media_to_langchain_format(url: str, media_type: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
    """
    Convert a media URL to LangChain standard format compatible with OpenAI API.

    Args:
        url: The HTTP/HTTPS URL of the media file
        media_type: Type of media - "image" or "audio"
        timeout: Request timeout in seconds (default: 10)

    Returns:
        Dict in LangChain/OpenAI format:
        - For images: {"type": "image", "base64": "...", "mime_type": "image/jpeg"}
        - For audio: None (audio is transcribed to text upstream via handle_audio_message;
          storing raw audio as input_audio in Redis history causes OpenAI Chat Completions
          API to return 400 since input_audio is only valid in the Realtime API)
        Returns None if conversion fails
    """
    if media_type == "audio":
        # Audio is already transcribed to text by handle_audio_message() before this point.
        # Do NOT store raw audio in Redis history — input_audio blocks are not supported
        # by the OpenAI Chat Completions API and cause 400 errors.
        return None

    base64_data_url = convert_media_url_to_base64(url, media_type=media_type, timeout=timeout)

    if not base64_data_url or not base64_data_url.startswith("data:"):
        logger.error(f"Failed to convert {media_type} to base64")
        return None

    # Extract mime_type and base64 data
    parts = base64_data_url.split(";base64,")
    if len(parts) != 2:
        logger.error("Invalid base64 data URL format")
        return None

    content_type = parts[0].replace("data:", "")
    base64_data = parts[1]

    if media_type == "image":
        return {
            "type": "image",
            "base64": base64_data,
            "mime_type": content_type,
        }

    return None
