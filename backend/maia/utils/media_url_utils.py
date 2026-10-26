"""Shared utilities for extracting and handling media URLs in bot responses.

Used by WahaService and other platform
services to detect media links (Google Drive, Dropbox, OneDrive, direct file
URLs) in AI-generated text, convert sharing URLs to direct-download URLs,
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
MEDIA_EXTENSIONS = {
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
MEDIA_CONTENT_TYPES = (
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
_GDRIVE_FILE_RE = r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)"
_GDRIVE_OPEN_RE = r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)"
_DROPBOX_RE = r"dropbox\.com/"
_ONEDRIVE_RE = r"1drv\.ms/|onedrive\.live\.com/"


# ---------------------------------------------------------------------------
# URL conversion helpers
# ---------------------------------------------------------------------------


# Characters that must stay literal in a percent-encoded path / query.
# '%' is kept safe so the function is idempotent on already-encoded URLs.
_SAFE_PATH_CHARS = "/%:@&=+$,;~-._!*'()"
_SAFE_QUERY_CHARS = "/%:@&=+$,;?~-._!*'()"


def encode_url_path(url: str) -> str:
    """Percent-encode non-ASCII characters in a URL's path and query.

    External fetchers (Twilio, Meta Graph API) reject media URLs containing raw
    non-ASCII bytes — e.g. a GCS object named with Hebrew characters. Newly
    uploaded files are encoded at upload time, but URLs stored before that fix,
    or produced by other sources, still arrive raw. Encoding is idempotent:
    '%' is treated as a safe character so an already-encoded URL is unchanged.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme:
        return url
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe=_SAFE_PATH_CHARS),
            quote(parts.query, safe=_SAFE_QUERY_CHARS),
            parts.fragment,
        )
    )


def to_direct_download_url(url: str) -> str:
    """Convert cloud-storage sharing URLs to direct-download equivalents.

    Supported services:
    - Google Drive  (/file/d/ID/view  or  /open?id=ID)
    - Dropbox       (?dl=0 → ?dl=1)
    - OneDrive      (?download=1 appended)

    Returns the original URL unchanged if no conversion applies.
    """
    # --- Google Drive ---
    m = re.search(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    m = re.search(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"

    # --- Dropbox ---
    if "dropbox.com/" in url:
        if "dl=0" in url:
            return url.replace("dl=0", "dl=1")
        elif "dl=1" not in url:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}dl=1"

    # --- OneDrive / 1drv.ms ---
    if "1drv.ms/" in url or "onedrive.live.com/" in url:
        if "download=1" not in url:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}download=1"

    return url


def is_cloud_storage_url(url: str) -> bool:
    """Return True if the URL is a known cloud-storage sharing link."""
    return bool(
        re.search(_GDRIVE_FILE_RE, url)
        or re.search(_GDRIVE_OPEN_RE, url)
        or re.search(_DROPBOX_RE, url)
        or re.search(_ONEDRIVE_RE, url)
    )


def is_media_url(url: str) -> bool:
    """Return True if *url* points to a downloadable media / document file.

    Performs a lightweight HEAD request first; falls back to a streamed GET
    for servers that do not support HEAD.
    """
    try:
        # Convert cloud URLs before probing
        probe_url = to_direct_download_url(url)

        resp = requests.head(probe_url, timeout=5, allow_redirects=True)
        if resp.status_code >= 400:
            resp = requests.get(probe_url, timeout=5, stream=True, allow_redirects=True)

        content_type = resp.headers.get("Content-Type", "").lower()
        for media_type in MEDIA_CONTENT_TYPES:
            if content_type.startswith(media_type):
                return True

        # Content-Disposition with attachment/filename means a downloadable file
        content_disposition = resp.headers.get("Content-Disposition", "")
        if "attachment" in content_disposition or "filename" in content_disposition:
            return True

        return False
    except Exception as e:
        logger.error(f"Warning: Could not check URL type for {url}: {e}")
        return False


# ---------------------------------------------------------------------------
# Message-level extraction / cleaning
# ---------------------------------------------------------------------------


def extract_media_urls(message: str) -> list:
    """Extract media URLs from message (images, videos, documents).

    Detection works in three passes:
    1. Known cloud-storage sharing URLs (Google Drive, Dropbox, OneDrive)
       are accepted immediately — they always point to a file.
    2. URLs with a known media extension are matched instantly (no HTTP call).
    3. All remaining URLs are probed with a HEAD request; if the Content-Type
       or Content-Disposition header indicates a downloadable file the URL is
       included.
    """
    # Match markdown links [text](url) and bare http(s) URLs
    url_pattern = r"(?:\[[^\]]*\]\()?(?P<url>https?://[^\s\)\]]+)(?:\))?"
    matches = re.finditer(url_pattern, message, re.IGNORECASE)
    media_urls = []

    for match in matches:
        url = match.group("url")
        if not url or url in media_urls:
            continue

        # Strip trailing punctuation that is not part of the URL
        url = url.rstrip(".,;:!?")

        # Pass 1: known cloud-storage sharing links
        if is_cloud_storage_url(url):
            media_urls.append(url)
            continue

        # Pass 2: URL path has a known media extension
        url_path = url.lower().split("?")[0].split("#")[0]
        has_known_ext = any(url_path.endswith(ext) for ext in MEDIA_EXTENSIONS)

        if has_known_ext:
            media_urls.append(url)
        else:
            # Pass 3: probe with HEAD to check Content-Type
            if is_media_url(url):
                media_urls.append(url)

    return media_urls


def clean_message_from_urls(message: str, media_urls: list) -> str:
    """Remove media URLs and markdown links from message."""
    cleaned_message = message

    # Remove markdown links containing media URLs: [text](url)
    for url in media_urls:
        # Escape special regex characters in URL
        escaped_url = re.escape(url)
        # Remove markdown format [text](url)
        pattern = r"\[([^\]]*)\]\(" + escaped_url + r"\)"
        cleaned_message = re.sub(pattern, "", cleaned_message)
        # Remove bare URL
        cleaned_message = cleaned_message.replace(url, "")

    # Clean up extra whitespace and newlines
    cleaned_message = re.sub(r"\n\s*\n", "\n", cleaned_message)  # Multiple newlines
    cleaned_message = re.sub(r"  +", " ", cleaned_message)  # Multiple spaces
    cleaned_message = cleaned_message.strip()

    return cleaned_message


# ---------------------------------------------------------------------------
# File download helpers (Google Drive auth fallback, HEIC conversion)
# ---------------------------------------------------------------------------


def _extract_gdrive_file_id(url: str) -> str | None:
    """Extract Google Drive file ID from any Drive URL variant."""
    m = re.search(r"(?:id=|/d/)([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def download_drive_file_authenticated(url: str) -> tuple[bytes, str | None, str | None]:
    """Download a private Google Drive file using the service account.

    Returns (file_bytes, filename, mimetype).
    Works with both personal Drive and Workspace Shared Drives.
    """
    from io import BytesIO

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    file_id = _extract_gdrive_file_id(url)
    if not file_id:
        raise ValueError(f"Could not extract Google Drive file ID from: {url}")

    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
    credentials = service_account.Credentials.from_service_account_file("serviceKey.json", scopes=SCOPES)
    drive_service = build("drive", "v3", credentials=credentials)

    # supportsAllDrives required for Workspace Shared Drives
    meta = drive_service.files().get(fileId=file_id, fields="name,mimeType", supportsAllDrives=True).execute()

    request = drive_service.files().get_media(fileId=file_id)
    buf = BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    return buf.getvalue(), meta.get("name"), meta.get("mimeType")


def download_media_file(url: str) -> tuple[bytes, str | None, str | None]:
    """Download a media file from URL. Falls back to authenticated Drive API
    if Google Drive returns an HTML page (file not public).

    Returns (file_bytes, filename, mimetype).
    """
    import cgi
    import mimetypes

    direct_url = encode_url_path(to_direct_download_url(url))

    try:
        resp = requests.get(direct_url, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to download file from {direct_url}: {e}")

    # If Drive returned HTML instead of the file → not public → use service account
    resp_ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if resp_ct == "text/html" and "drive.google.com" in direct_url:
        return download_drive_file_authenticated(url)

    # Extract filename from Content-Disposition
    filename = None
    content_disp = resp.headers.get("Content-Disposition", "")
    if "filename" in content_disp:
        _, params = cgi.parse_header(content_disp)
        filename = params.get("filename")

    # Detect mimetype
    mimetype = resp.headers.get("Content-Type")
    if mimetype:
        mimetype = mimetype.split(";")[0].strip()
    if not mimetype:
        mimetype, _ = mimetypes.guess_type(direct_url)
    if not mimetype:
        mimetype = "application/octet-stream"

    return resp.content, filename, mimetype


def convert_heic_to_jpeg(file_bytes: bytes, filename: str | None) -> tuple[bytes, str | None, str]:
    """Convert HEIC/HEIF image bytes to JPEG. Returns original data unchanged
    if the file is not HEIC or conversion fails.

    Returns (file_bytes, filename, mimetype).
    """
    if not filename or not filename.lower().endswith((".heic", ".heif")):
        # Not HEIC — return as-is, mimetype unknown (caller keeps existing)
        return file_bytes, filename, ""

    try:
        from io import BytesIO

        import pillow_heif
        from PIL import Image

        pillow_heif.register_heif_opener()
        img = Image.open(BytesIO(file_bytes))
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=90)
        new_filename = filename.rsplit(".", 1)[0] + ".jpg"
        return buf.getvalue(), new_filename, "image/jpeg"
    except Exception as e:
        logger.error(f"Warning: HEIC conversion failed for {filename}, sending as-is: {e}")
        return file_bytes, filename, ""
