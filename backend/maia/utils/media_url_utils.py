"""Shared utilities for extracting and handling media URLs in bot responses.

Used by WahaService and other platform services to detect media links (Google Drive,
direct file URLs) in AI-generated text, convert sharing URLs to direct-download URLs,
and clean the message text after extraction.
"""

import logging
import re
from urllib.parse import quote, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Known media file extensions
SENDABLE_FILE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".heic",
    ".heif",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".mkv",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".m4a",
    ".aac",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".csv",
    ".txt",
    ".zip",
    ".rar",
}

# Content-Type prefixes that indicate a sendable file
SENDABLE_CONTENT_TYPE_PREFIXES = (
    "image/",
    "video/",
    "audio/",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/zip",
    "application/x-rar",
    "application/octet-stream",
    "text/csv",
)

# Patterns for cloud storage sharing URLs that need conversion
_GOOGLE_DRIVE_FILE_LINK_PATTERN = r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)"
_GOOGLE_DRIVE_OPEN_LINK_PATTERN = r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"

# Match markdown links [text](url) and bare http(s) URLs
_MESSAGE_LINK_PATTERN = r"(?:\[[^\]]*\]\()?(?P<url>https?://[^\s\)\]]+)(?:\))?"


# ---------------------------------------------------------------------------
# URL conversion helpers
# ---------------------------------------------------------------------------


# Characters that must stay literal in a percent-encoded path / query.
# '%' is kept safe so the function is idempotent on already-encoded URLs.
_URL_PATH_SAFE_CHARACTERS = "/%:@&=+$,;~-._!*'()"
_URL_QUERY_SAFE_CHARACTERS = "/%:@&=+$,;?~-._!*'()"


def percent_encode_url_path_and_query(url_to_encode: str) -> str:
    """Percent-encode non-ASCII characters in a URL's path and query.

    External fetchers (Twilio, Meta Graph API) reject media URLs containing raw
    non-ASCII bytes — e.g. a GCS object named with Hebrew characters. Newly
    uploaded files are encoded at upload time, but URLs stored before that fix,
    or produced by other sources, still arrive raw. Encoding is idempotent:
    '%' is treated as a safe character so an already-encoded URL is unchanged.
    """
    if not url_to_encode:
        return url_to_encode
    try:
        url_components = urlsplit(url_to_encode)
    except ValueError:
        return url_to_encode
    if not url_components.scheme:
        return url_to_encode
    return urlunsplit(
        (
            url_components.scheme,
            url_components.netloc,
            quote(url_components.path, safe=_URL_PATH_SAFE_CHARACTERS),
            quote(url_components.query, safe=_URL_QUERY_SAFE_CHARACTERS),
            url_components.fragment,
        )
    )


def _find_google_drive_sharing_file_id(candidate_url: str) -> str | None:
    """Return the file id of a Google Drive sharing link (/file/d/ID or /open?id=ID), or None.

    Stricter than _extract_file_id_from_any_google_drive_url: only these two drive.google.com forms match.
    """
    drive_link_match = re.search(_GOOGLE_DRIVE_FILE_LINK_PATTERN, candidate_url)
    if not drive_link_match:
        drive_link_match = re.search(_GOOGLE_DRIVE_OPEN_LINK_PATTERN, candidate_url)
    return drive_link_match.group(1) if drive_link_match else None


def convert_to_direct_download_url(source_url: str) -> str:
    """Convert cloud-storage sharing URLs to direct-download equivalents.

    Supported service: Google Drive (/file/d/ID/view  or  /open?id=ID).

    Returns the original URL unchanged if no conversion applies.
    """
    # --- Google Drive ---
    file_id = _find_google_drive_sharing_file_id(source_url)
    if file_id is not None:
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    return source_url


def is_google_drive_sharing_url(candidate_url: str) -> bool:
    """Return True if the URL is a known cloud-storage sharing link."""
    return _find_google_drive_sharing_file_id(candidate_url) is not None


def _send_probe_request(probe_url: str) -> requests.Response:
    """Send a HEAD request, following redirects; when it answers with status >= 400, retry as a streamed GET.

    Returns the last response (up to two HTTP requests are made).
    """
    probe_response = requests.head(probe_url, timeout=5, allow_redirects=True)
    if probe_response.status_code >= 400:
        probe_response = requests.get(probe_url, timeout=5, stream=True, allow_redirects=True)
    return probe_response


def _has_downloadable_file_headers(response_headers: requests.structures.CaseInsensitiveDict) -> bool:
    """Return True if Content-Type is a sendable file type or Content-Disposition names an attachment/filename."""
    content_type = response_headers.get("Content-Type", "").lower()
    for content_type_prefix in SENDABLE_CONTENT_TYPE_PREFIXES:
        if content_type.startswith(content_type_prefix):
            return True

    # Content-Disposition with attachment/filename means a downloadable file
    content_disposition = response_headers.get("Content-Disposition", "")
    if "attachment" in content_disposition or "filename" in content_disposition:
        return True

    return False


def is_downloadable_file_url(candidate_url: str) -> bool:
    """Return True if *candidate_url* points to a downloadable media / document file.

    Performs a lightweight HEAD request first; falls back to a streamed GET
    for servers that do not support HEAD.
    """
    try:
        # Convert cloud URLs before probing
        probe_url = convert_to_direct_download_url(candidate_url)

        probe_response = _send_probe_request(probe_url)
        return _has_downloadable_file_headers(probe_response.headers)
    except Exception as error:
        logger.error(f"Warning: Could not check URL type for {candidate_url}: {error}")
        return False


# ---------------------------------------------------------------------------
# Message-level extraction / cleaning
# ---------------------------------------------------------------------------


def _has_sendable_file_extension(candidate_url: str) -> bool:
    """Return True if the URL path (lowercased, without query and fragment) ends with a known file extension."""
    url_without_query_or_fragment = candidate_url.lower().split("?")[0].split("#")[0]
    return any(url_without_query_or_fragment.endswith(extension) for extension in SENDABLE_FILE_EXTENSIONS)


def _is_sendable_file_url(candidate_url: str) -> bool:
    """Return True if the URL should be sent as a file, trying the three passes in order.

    Pass 1: known cloud-storage sharing links. Pass 2: the URL path has a known media extension.
    Pass 3: probe with HEAD to check Content-Type; the only pass with an HTTP call, so it runs last.
    """
    return (
        is_google_drive_sharing_url(candidate_url)
        or _has_sendable_file_extension(candidate_url)
        or is_downloadable_file_url(candidate_url)
    )


def collect_media_urls_from_message(message_text: str) -> list:
    """Extract media URLs from message (images, videos, documents).

    Detection works in three passes:
    1. Known cloud-storage sharing URLs (Google Drive)
       are accepted immediately — they always point to a file.
    2. URLs with a known media extension are matched instantly (no HTTP call).
    3. All remaining URLs are probed with a HEAD request; if the Content-Type
       or Content-Disposition header indicates a downloadable file the URL is
       included.
    """
    link_matches = re.finditer(_MESSAGE_LINK_PATTERN, message_text, re.IGNORECASE)
    media_urls = []

    for link_match in link_matches:
        candidate_url = link_match.group("url")
        if not candidate_url or candidate_url in media_urls:
            continue

        # Strip trailing punctuation that is not part of the URL
        candidate_url = candidate_url.rstrip(".,;:!?")

        if _is_sendable_file_url(candidate_url):
            media_urls.append(candidate_url)

    return media_urls


def _remove_markdown_link_and_bare_url(message_text: str, media_url: str) -> str:
    """Remove one URL from the text: its markdown links [text](url) first, then every bare occurrence."""
    # Escape special regex characters in URL
    escaped_url = re.escape(media_url)
    # Remove markdown format [text](url)
    markdown_link_pattern = r"\[([^\]]*)\]\(" + escaped_url + r"\)"
    text_without_markdown_links = re.sub(markdown_link_pattern, "", message_text)
    # Remove bare URL
    return text_without_markdown_links.replace(media_url, "")


def _collapse_blank_lines_and_spaces(message_text: str) -> str:
    """Collapse blank lines and runs of spaces, then strip leading and trailing whitespace."""
    collapsed_text = re.sub(r"\n\s*\n", "\n", message_text)  # Multiple newlines
    collapsed_text = re.sub(r"  +", " ", collapsed_text)  # Multiple spaces
    return collapsed_text.strip()


def strip_media_urls_from_message(message_text: str, media_urls: list) -> str:
    """Remove media URLs and markdown links from message."""
    cleaned_message = message_text

    # Remove markdown links containing media URLs: [text](url)
    for media_url in media_urls:
        cleaned_message = _remove_markdown_link_and_bare_url(cleaned_message, media_url)

    # Clean up extra whitespace and newlines
    return _collapse_blank_lines_and_spaces(cleaned_message)


# ---------------------------------------------------------------------------
# File download helpers (Google Drive auth fallback, HEIC conversion)
# ---------------------------------------------------------------------------


def _extract_file_id_from_any_google_drive_url(drive_url: str) -> str | None:
    """Extract Google Drive file ID from any Drive URL variant."""
    file_id_match = re.search(r"(?:id=|/d/)([a-zA-Z0-9_-]+)", drive_url)
    return file_id_match.group(1) if file_id_match else None


def _build_google_drive_service_with_service_account():
    """Build a Drive v3 API client authorized by the service account key, with the read-only Drive scope."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    drive_readonly_scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    service_account_credentials = service_account.Credentials.from_service_account_file(
        "serviceKey.json", scopes=drive_readonly_scopes
    )
    return build("drive", "v3", credentials=service_account_credentials)


def _download_google_drive_file_bytes(drive_service, file_id: str) -> bytes:
    """Download the file's content through the Drive API, chunk by chunk, and return its bytes."""
    from io import BytesIO

    from googleapiclient.http import MediaIoBaseDownload

    media_download_request = drive_service.files().get_media(fileId=file_id)
    download_buffer = BytesIO()
    downloader = MediaIoBaseDownload(download_buffer, media_download_request)
    is_download_complete = False
    while not is_download_complete:
        _, is_download_complete = downloader.next_chunk()

    return download_buffer.getvalue()


def download_google_drive_file_with_service_account(drive_url: str) -> tuple[bytes, str | None, str | None]:
    """Download a private Google Drive file using the service account.

    Returns (file_bytes, filename, mimetype).
    Works with both personal Drive and Workspace Shared Drives.
    """
    file_id = _extract_file_id_from_any_google_drive_url(drive_url)
    if not file_id:
        raise ValueError(f"Could not extract Google Drive file ID from: {drive_url}")

    drive_service = _build_google_drive_service_with_service_account()

    # supportsAllDrives required for Workspace Shared Drives
    drive_file_metadata = (
        drive_service.files().get(fileId=file_id, fields="name,mimeType", supportsAllDrives=True).execute()
    )

    file_bytes = _download_google_drive_file_bytes(drive_service, file_id)

    return file_bytes, drive_file_metadata.get("name"), drive_file_metadata.get("mimeType")


def _extract_filename_from_content_disposition(content_disposition_header: str) -> str | None:
    """Return the filename parameter of a Content-Disposition header, or None when the header names none."""
    import cgi

    if "filename" not in content_disposition_header:
        return None
    _, disposition_params = cgi.parse_header(content_disposition_header)
    return disposition_params.get("filename")


def _resolve_download_mimetype(content_type_header: str | None, direct_url: str) -> str:
    """Return the Content-Type without its parameters, else a guess from the URL, else application/octet-stream."""
    import mimetypes

    resolved_mimetype = content_type_header
    if resolved_mimetype:
        resolved_mimetype = resolved_mimetype.split(";")[0].strip()
    if not resolved_mimetype:
        resolved_mimetype, _ = mimetypes.guess_type(direct_url)
    if not resolved_mimetype:
        resolved_mimetype = "application/octet-stream"

    return resolved_mimetype


def download_media_file_with_metadata(media_url: str) -> tuple[bytes, str | None, str | None]:
    """Download a media file from URL. Falls back to authenticated Drive API
    if Google Drive returns an HTML page (file not public).

    Returns (file_bytes, filename, mimetype).
    """
    direct_url = percent_encode_url_path_and_query(convert_to_direct_download_url(media_url))

    try:
        download_response = requests.get(direct_url, timeout=30)
        download_response.raise_for_status()
    except requests.exceptions.RequestException as download_error:
        raise RuntimeError(f"Failed to download file from {direct_url}: {download_error}")

    # If Drive returned HTML instead of the file → not public → use service account
    response_content_type = (download_response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if response_content_type == "text/html" and "drive.google.com" in direct_url:
        return download_google_drive_file_with_service_account(media_url)

    # Extract filename from Content-Disposition
    filename = _extract_filename_from_content_disposition(download_response.headers.get("Content-Disposition", ""))

    # Detect mimetype
    mimetype = _resolve_download_mimetype(download_response.headers.get("Content-Type"), direct_url)

    return download_response.content, filename, mimetype


def _transcode_heic_bytes_to_jpeg(heic_bytes: bytes) -> bytes:
    """Decode HEIC/HEIF bytes with pillow-heif and re-encode them as an RGB JPEG at quality 90."""
    from io import BytesIO

    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    heic_image = Image.open(BytesIO(heic_bytes))
    jpeg_buffer = BytesIO()
    heic_image.convert("RGB").save(jpeg_buffer, format="JPEG", quality=90)
    return jpeg_buffer.getvalue()


def convert_heic_to_jpeg_if_needed(original_file_bytes: bytes, original_filename: str | None) -> tuple[bytes, str | None, str]:
    """Convert HEIC/HEIF image bytes to JPEG. Returns original data unchanged
    if the file is not HEIC or conversion fails.

    Returns (file_bytes, filename, mimetype).
    """
    if not original_filename or not original_filename.lower().endswith((".heic", ".heif")):
        # Not HEIC — return as-is, mimetype unknown (caller keeps existing)
        return original_file_bytes, original_filename, ""

    try:
        jpeg_bytes = _transcode_heic_bytes_to_jpeg(original_file_bytes)
        jpeg_filename = original_filename.rsplit(".", 1)[0] + ".jpg"
        return jpeg_bytes, jpeg_filename, "image/jpeg"
    except Exception as error:
        logger.error(f"Warning: HEIC conversion failed for {original_filename}, sending as-is: {error}")
        return original_file_bytes, original_filename, ""
