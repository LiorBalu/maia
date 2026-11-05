"""Downloads a media URL and stores it in the tenant's bucket folder."""

from datetime import datetime, timezone
from os import getenv
import re

import requests
from slugify import slugify
from io import BytesIO
from maia.utils.google_cloud_storage.public import upload_file_to_public_bucket

_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio": ".mp3",  # Default to mp3 for generic audio
    # oga
    "audio/flac": ".flac",
    "application/pdf": ".pdf",
    "image": ".jpg",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-ms-wmv": ".wmv",
    "video/x-msvideo": ".avi",
    # csv txt excel docs
    "text/csv": ".csv",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def _format_utc_timestamp() -> str:
    """The current UTC time as "%Y-%m-%d-%H-%M-%S-%f", used as a filename prefix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f")


def _strip_content_type_parameters(content_type_value: str) -> str:
    """The media type without its parameters."""
    # Extract the main content type by splitting on ';' to handle cases like "audio/ogg; codecs=opus"
    return content_type_value.split(";")[0].strip()


def _download_media(source_media_url: str) -> tuple[requests.Response, BytesIO | None]:
    """GET the media (with the WAHA API key when the URL is on the WAHA server); returns the response and its
    content as a buffer, the buffer being None when the status is not 200."""
    from maia.services.waha import resolve_waha_base_url

    media_buffer = None
    if source_media_url.startswith(f"{resolve_waha_base_url()}/"):
        download_response = requests.get(source_media_url, headers={"X-Api-Key": getenv("WAHA_API_KEY", "")})
    else:
        download_response = requests.get(source_media_url)
    if download_response.status_code == 200:
        media_buffer = BytesIO(download_response.content)
    return download_response, media_buffer


def _extract_filename_from_content_disposition(content_disposition: str | None) -> str | None:
    """The file name a Content-Disposition header carries, prefixed with the UTC timestamp and slugified; None when
    the header is missing or names no file."""
    if content_disposition:
        disposition_filenames = re.findall('filename="?([^"]+)"?', content_disposition)
        if disposition_filenames:
            timestamped_filename = _format_utc_timestamp() + disposition_filenames[0]
            return slugify(timestamped_filename, regex_pattern=r"[^\w\.]+")
    return None


def _build_timestamped_filename(bare_content_type: str) -> str | None:
    """A UTC-timestamp file name with the extension of the content type; None when the type is unknown."""
    timestamp_stem = _format_utc_timestamp()
    file_extension = _EXTENSION_BY_CONTENT_TYPE.get(bare_content_type)
    return timestamp_stem + file_extension if file_extension else None


def upload_media_from_url_to_bucket(tenant_name: str, media_url: str, content_type: str) -> str:
    """Download a media URL (with the WAHA API key for WAHA links) and upload it to the tenant's folder under a
    timestamped name whose extension matches the content type; returns the public URL.
    """
    bare_content_type = _strip_content_type_parameters(content_type)

    download_response, media_buffer = _download_media(media_url)

    # Fall back to the response's own Content-Type when the caller passed nothing
    # (e.g. WAHA NOWEB payloads that omit the mimetype) so we can still name the file.
    if not bare_content_type:
        bare_content_type = _strip_content_type_parameters(download_response.headers.get("content-type") or "")
        content_type = content_type or bare_content_type

    content_disposition = download_response.headers.get("content-disposition")
    media_filename = _extract_filename_from_content_disposition(content_disposition)

    if not media_filename:
        media_filename = _build_timestamped_filename(bare_content_type)
    if not media_filename:
        raise Exception(f"Filename not found for content type: {content_type}")
    if bare_content_type == "audio" and not media_filename.endswith(".mp3"):
        media_filename = media_filename + ".mp3"
    if bare_content_type == "image" and not media_filename.endswith(".jpg"):
        media_filename = media_filename + ".jpg"
    return upload_file_to_public_bucket(
        media_buffer, tenant_name, _format_utc_timestamp() + media_filename, content_type
    )
