"""Downloads a media URL and stores it in the tenant's bucket folder."""

from datetime import datetime, timezone
from os import getenv
import re

import requests
from slugify import slugify
from io import BytesIO
from maia.utils.google_cloud_storage.public import upload_to_gcs


def save_file_url_in_bucket(tenant_name: str, media_url: str, content_type: str) -> str:
    # Extract the main content type by splitting on ';' to handle cases like "audio/ogg; codecs=opus"
    main_content_type = content_type.split(";")[0].strip()

    media_file = None
    filename = None
    from maia.services.waha import get_known_waha_base_urls

    if any(media_url.startswith(f"{base}/") for base in get_known_waha_base_urls()):
        response = requests.get(media_url, headers={"X-Api-Key": getenv("WAHA_API_KEY", "")})
    else:
        response = requests.get(media_url)
    if response.status_code == 200:
        media_file = BytesIO(response.content)

    # Fall back to the response's own Content-Type when the caller passed nothing
    # (e.g. WAHA NOWEB payloads that omit the mimetype) so we can still name the file.
    if not main_content_type:
        main_content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        content_type = content_type or main_content_type

    cd = response.headers.get("content-disposition")

    if cd:
        fname = re.findall('filename="?([^"]+)"?', cd)
        if fname:
            filename = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f") + fname[0]
            filename = slugify(filename, regex_pattern=r"[^\w\.]+")

    if not filename:
        base_filename = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f")

        if main_content_type == "audio/ogg":
            filename = base_filename + ".ogg"
        elif main_content_type == "audio/mpeg":
            filename = base_filename + ".mp3"
        elif main_content_type == "audio/wav":
            filename = base_filename + ".wav"
        elif main_content_type == "audio":
            filename = base_filename + ".mp3"  # Default to mp3 for generic audio
        # oga
        elif main_content_type == "audio/flac":
            filename = base_filename + ".flac"
        elif main_content_type == "application/pdf":
            filename = base_filename + ".pdf"
        elif main_content_type == "image":
            filename = base_filename + ".jpg"
        elif main_content_type == "image/jpeg":
            filename = base_filename + ".jpg"
        elif main_content_type == "image/png":
            filename = base_filename + ".png"
        elif main_content_type == "image/gif":
            filename = base_filename + ".gif"
        elif main_content_type == "image/webp":
            filename = base_filename + ".webp"
        elif main_content_type == "video/mp4":
            filename = base_filename + ".mp4"
        elif main_content_type == "video/quicktime":
            filename = base_filename + ".mov"
        elif main_content_type == "video/x-ms-wmv":
            filename = base_filename + ".wmv"
        elif main_content_type == "video/x-msvideo":
            filename = base_filename + ".avi"
        # csv txt excel docs
        elif main_content_type == "text/csv":
            filename = base_filename + ".csv"
        elif main_content_type == "text/plain":
            filename = base_filename + ".txt"
        elif main_content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            filename = base_filename + ".xlsx"
        elif main_content_type == "application/vnd.ms-excel":
            filename = base_filename + ".xls"
        elif main_content_type == "application/msword":
            filename = base_filename + ".doc"
        elif main_content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            filename = base_filename + ".docx"
    if not filename:
        raise Exception(f"Filename not found for content type: {content_type}")
    if main_content_type == "audio" and not filename.endswith(".mp3"):
        filename = filename + ".mp3"
    if main_content_type == "image" and not filename.endswith(".jpg"):
        filename = filename + ".jpg"
    return upload_to_gcs(
        media_file, tenant_name, datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f") + filename, content_type
    )
