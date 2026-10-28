"""Stores an uploaded FastAPI file in the tenant's bucket folder with a sanitized name."""

import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from fastapi import UploadFile

from maia.utils.google_cloud_storage.public import upload_to_gcs


def sanitize_filename(name: str) -> str:
    """Sanitize a filename while preserving unicode characters (e.g. Hebrew).
    Replaces spaces with hyphens and removes characters that are not
    word characters (unicode), dots, or hyphens."""
    name = name.strip().replace(" ", "-")
    name = re.sub(r"[^\w.\-]", "", name, flags=re.UNICODE)
    return name or str(uuid.uuid4())


async def save_fastapi_file_in_bucket(tenant_name: str, file: UploadFile, filename: Optional[str] = None) -> tuple:
    # Initialize message_type with a default value
    """Upload a FastAPI file to the bucket under the tenant's folder; returns (message_type, public URL). The type
    (image / docx / xlsx / csv / pdf / audio / txt...) comes from the content type.
    """
    message_type = "document"

    if file.content_type in ["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"]:
        message_type = "image"
    elif file.content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]:
        message_type = "docx"
    elif file.content_type in [
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ]:
        message_type = "xlsx"
    elif file.content_type in ["text/csv"]:
        message_type = "csv"
    elif file.content_type in ["application/pdf"]:
        message_type = "pdf"
    elif file.content_type in [
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
    elif file.content_type in ["application/zip"]:
        message_type = "zip"
    elif file.content_type in ["text/plain"]:
        message_type = "txt"

    data_of_file = await file.read()
    fake_file = BytesIO(data_of_file)
    fake_file.seek(0)
    if not filename:
        filename = file.filename
    if filename is None or "." not in filename:
        filename = (filename or str(uuid.uuid4())) + f".{message_type}"

    # Handle folder paths: split filename into path and name parts
    if "/" in filename:
        path_parts = filename.split("/")
        folder_path = "/".join(path_parts[:-1])  # All parts except the last one
        file_name = path_parts[-1]  # The actual filename

        # Sanitize only the filename part, preserve folder structure
        sanitized_name = sanitize_filename(file_name)
        # Don't add timestamp when uploading to a specific folder
        full_path = f"{folder_path}/{sanitized_name}"
    else:
        # No folder path, add timestamp to avoid conflicts
        sanitized_name = sanitize_filename(filename)
        full_path = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f") + sanitized_name

    media_url = upload_to_gcs(fake_file, tenant_name, full_path, file.content_type)

    return message_type, media_url
