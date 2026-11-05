"""Stores an uploaded FastAPI file in the tenant's bucket folder with a sanitized name."""

import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from fastapi import UploadFile

from maia.utils.google_cloud_storage.public import upload_file_to_public_bucket


def normalize_blob_filename(raw_filename: str) -> str:
    """Sanitize a filename while preserving unicode characters (e.g. Hebrew).
    Replaces spaces with hyphens and removes characters that are not
    word characters (unicode), dots, or hyphens."""
    normalized_filename = raw_filename.strip().replace(" ", "-")
    normalized_filename = re.sub(r"[^\w.\-]", "", normalized_filename, flags=re.UNICODE)
    return normalized_filename or str(uuid.uuid4())


def _classify_message_type(content_type: Optional[str]) -> str:
    """Message type of an upload from its content type (image / docx / xlsx / csv / pdf / audio / zip / txt);
    "document" when the content type is unknown or missing."""
    # Initialize message_type with a default value
    message_type = "document"

    if content_type in ["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"]:
        message_type = "image"
    elif content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]:
        message_type = "docx"
    elif content_type in [
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ]:
        message_type = "xlsx"
    elif content_type in ["text/csv"]:
        message_type = "csv"
    elif content_type in ["application/pdf"]:
        message_type = "pdf"
    elif content_type in [
        "audio/mpeg",
        "audio/wav",
        "audio/mp3",
        "audio/webm",
        "audio/ogg",
        "audio/mp4",
        "audio/aac",
        "audio/amr",
    ]:
        message_type = "audio"
    elif content_type in ["application/zip"]:
        message_type = "zip"
    elif content_type in ["text/plain"]:
        message_type = "txt"
    return message_type


def _resolve_target_filename(
    target_filename: Optional[str], uploaded_filename: Optional[str], message_type: str
) -> str:
    """The name to store: the requested name, else the upload's own name, else a uuid; ".<message_type>" is
    appended when the name has no extension. It may carry sub-folders ("folder/name.ext")."""
    if not target_filename:
        target_filename = uploaded_filename
    if target_filename is None or "." not in target_filename:
        target_filename = (target_filename or str(uuid.uuid4())) + f".{message_type}"
    return target_filename


def _build_blob_name(resolved_filename: str) -> str:
    """Blob name inside the bucket folder: a name with sub-folders keeps them and only its last part is
    normalized; a bare name is normalized and prefixed with the UTC timestamp."""
    # Handle folder paths: split filename into path and name parts
    if "/" in resolved_filename:
        path_segments = resolved_filename.split("/")
        subfolder = "/".join(path_segments[:-1])  # All parts except the last one
        basename = path_segments[-1]  # The actual filename

        # Sanitize only the filename part, preserve folder structure
        normalized_filename = normalize_blob_filename(basename)
        # Don't add timestamp when uploading to a specific folder
        blob_name = f"{subfolder}/{normalized_filename}"
    else:
        # No folder path, add timestamp to avoid conflicts
        normalized_filename = normalize_blob_filename(resolved_filename)
        blob_name = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f") + normalized_filename
    return blob_name


async def upload_fastapi_file_to_bucket(bucket_folder: str, uploaded_file: UploadFile, target_filename: Optional[str] = None) -> tuple:
    """Upload a FastAPI file to the bucket under the tenant's folder; returns (message_type, public URL). The type
    (image / docx / xlsx / csv / pdf / audio / txt...) comes from the content type.
    """
    message_type = _classify_message_type(uploaded_file.content_type)

    file_bytes = await uploaded_file.read()
    file_buffer = BytesIO(file_bytes)
    file_buffer.seek(0)
    resolved_filename = _resolve_target_filename(target_filename, uploaded_file.filename, message_type)
    blob_name = _build_blob_name(resolved_filename)

    media_url = upload_file_to_public_bucket(file_buffer, bucket_folder, blob_name, uploaded_file.content_type)

    return message_type, media_url
