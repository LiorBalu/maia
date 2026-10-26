"""WahaService: the WAHA HTTP API (sessions, sending text / media, webhooks) for a tenant's WhatsApp number."""

import logging
import uuid
from os import getenv

import requests
from fastapi import HTTPException

from maia.models.tenants import WahaAccount, TenantDocument
from maia.utils.media_url_utils import clean_message_from_urls as _clean_message_from_urls
from maia.utils.media_url_utils import convert_heic_to_jpeg as _convert_heic_to_jpeg
from maia.utils.media_url_utils import download_media_file as _download_media_file
from maia.utils.media_url_utils import extract_media_urls as _extract_media_urls

logger = logging.getLogger(__name__)

# Events Maia subscribes to on every WAHA session. Kept in one place so
# session-create and webhook-update never drift apart.
WAHA_WEBHOOK_EVENTS = [
    "message.any",
    "session.status",
    "poll.vote",
    "poll.vote.failed",
    "engine.event",
    "call.received",
]


def get_default_waha_url() -> str:
    """Default WAHA server, used for accounts that have no server_url assigned."""
    return getenv("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")


def get_waha_webhook_base_url() -> str:
    """Base URL WAHA posts webhooks to: WAHA_WEBHOOK_BASE_URL when set (e.g. http://fastapi:8000 when
    WAHA shares the docker network), otherwise the public BACKEND_URL."""
    return (getenv("WAHA_WEBHOOK_BASE_URL") or getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")


def get_waha_worker_urls() -> list[str]:
    """Ordered WAHA worker fleet from WAHA_WORKER_URLS (comma-separated). Empty = no fleet."""
    raw = getenv("WAHA_WORKER_URLS", "")
    return [url.strip().rstrip("/") for url in raw.split(",") if url.strip()]


def get_known_waha_base_urls() -> list[str]:
    """All base URLs a WAHA media link can originate from (default server + every worker)."""
    return [get_default_waha_url(), *get_waha_worker_urls()]


def count_waha_accounts_per_server() -> dict[str, int]:
    """Count assigned WAHA accounts per server_url across all tenants (accounts on the default server excluded)."""
    pipeline = [
        {"$unwind": "$waha_accounts"},
        {"$match": {"waha_accounts.server_url": {"$nin": [None, ""]}}},
        {"$group": {"_id": "$waha_accounts.server_url", "count": {"$sum": 1}}},
    ]
    return {doc["_id"].rstrip("/"): doc["count"] for doc in TenantDocument.objects.aggregate(pipeline)}


def pick_waha_server_url() -> str | None:
    """Pick the worker for a new account: fill worker 1 up to capacity, then worker 2, and so on.

    Returns None when no fleet is configured (account will use the default server).
    Raises 503 when every worker is at capacity — add a worker to WAHA_WORKER_URLS.
    """
    workers = get_waha_worker_urls()
    if not workers:
        return None

    max_sessions = int(getenv("WAHA_WORKER_MAX_SESSIONS", "10"))
    counts = count_waha_accounts_per_server()
    for url in workers:
        if counts.get(url, 0) < max_sessions:
            return url

    raise HTTPException(
        status_code=503,
        detail=f"All {len(workers)} WAHA workers are at capacity ({max_sessions} sessions each). "
        "Add a new worker to WAHA_WORKER_URLS.",
    )


class WahaService:
    def __init__(self, tenant_name: str, phone_number: str, **kwargs):
        self.tenant_name = tenant_name
        self.phone_number = phone_number
        self.api_key = getenv("WAHA_API_KEY", "")
        if not self.api_key:
            raise HTTPException(status_code=500, detail="WAHA_API_KEY is not configured")

        # Get account info from TenantDocument
        self.account = self._get_account_info()
        # When _get_account_info fell back to the tenant's sole account (the
        # requested number was a recipient or the default server, not a session name),
        # align self.phone_number with it — every WAHA call uses
        # self.phone_number as the session, and a stale value produces
        # 422 "Session does not exist" on send. _get_account_info never returns
        # None (it raises 404), and WahaAccount.phone_number is a required
        # field on the embedded document, so this attribute access is safe.
        account_phone = getattr(self.account, "phone_number", None)
        if account_phone and account_phone != self.phone_number:
            self.phone_number = account_phone
        # Each account is pinned to one WAHA worker; accounts without one use the default server
        self.base_url = (self.account.server_url or "").rstrip("/") or get_default_waha_url()

    def _get_account_info(self) -> WahaAccount:
        """Get WAHA account info from TenantDocument"""
        tenant = TenantDocument.objects(name=self.tenant_name).first()
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        account = tenant.get_waha_by_phone_number(self.phone_number)
        if not account:
            # The connection key must be the connected account number. Group JIDs
            # (and occasionally recipient numbers) leak into this slot from the
            # message pipeline — they never identify an account. When the tenant has
            # a single connected account, fall back to it so the reply is still
            # delivered instead of failing with a 404 and dropping the AI response.
            accounts = tenant.waha_accounts or []
            if len(accounts) == 1:
                account = accounts[0]
                logger.warning(f"WAHA connection '{self.phone_number}' not found for tenant "
                    f"'{self.tenant_name}'; falling back to sole account '{account.phone_number}'")
        if not account:
            raise HTTPException(status_code=404, detail=f"WAHA connection '{self.phone_number}' not found")

        return account

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}{endpoint}"

    def _headers(self) -> dict:
        """Get headers with API Key for WAHA Pro"""
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _update_connection_status(self, is_connected: bool):
        """Update connection status in TenantDocument"""
        tenant = TenantDocument.objects(name=self.tenant_name).first()
        if tenant:
            for account in tenant.waha_accounts:
                if account.phone_number.replace("+", "") == self.phone_number.replace("+", ""):
                    account.is_connected = is_connected
                    tenant.save()
                    break

    def create_whatsapp_client(self) -> dict:
        """Create WhatsApp session in WAHA Pro and set webhook automatically"""
        webhook_url = f"{get_waha_webhook_base_url()}/api/async/waha/{self.tenant_name}"

        url = self._url("/api/sessions/start")
        payload = {
            "name": self.phone_number,
            "config": {
                "webhooks": [
                    {
                        "url": webhook_url,
                        "events": WAHA_WEBHOOK_EVENTS,
                        "customHeaders": [{"name": "X-Tenant-Name", "value": self.tenant_name}],
                    }
                ],
                "metadata": {"tenant_name": self.tenant_name},
                "noweb": {"store": {"enabled": True, "fullSync": False}},
            },
        }
        response = requests.post(url, json=payload, headers=self._headers())

        # If session already exists (422), just return success and update webhook
        if response.status_code == 422:
            # Session already exists, update webhook configuration
            self.set_webhook(webhook_url)
            return {"name": self.phone_number, "status": "ALREADY_EXISTS", "message": "Session already started"}

        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        # Update webhook URL in TenantDocument
        tenant = TenantDocument.objects(name=self.tenant_name).first()
        if tenant:
            for account in tenant.waha_accounts:
                if account.phone_number.replace("+", "") == self.phone_number.replace("+", ""):
                    account.webhook_url = webhook_url
                    tenant.save()
                    break

        return response.json()

    def get_qr_code(self) -> dict:
        """Get QR code for WhatsApp connection"""
        url = self._url(f"/api/{self.phone_number}/auth/qr")
        headers = self._headers()
        headers["Accept"] = "application/json"
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        return response.json()

    def set_webhook(self, webhook_url: str) -> dict:
        """Set webhook for WhatsApp connection"""
        url = self._url(f"/api/sessions/{self.phone_number}")
        payload = {
            "config": {
                "webhooks": [
                    {
                        "url": webhook_url,
                        "events": WAHA_WEBHOOK_EVENTS,
                        "customHeaders": [{"name": "X-Tenant-Name", "value": self.tenant_name}],
                    }
                ],
                "metadata": {"tenant_name": self.tenant_name},
                "noweb": {"store": {"enabled": True, "fullSync": False}},
            }
        }
        response = requests.put(url, json=payload, headers=self._headers())

        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        # Update webhook URL in TenantDocument
        tenant = TenantDocument.objects(name=self.tenant_name).first()
        if tenant:
            for account in tenant.waha_accounts:
                if account.phone_number.replace("+", "") == self.phone_number.replace("+", ""):
                    account.webhook_url = webhook_url
                    tenant.save()
                    break

        return response.json()

    def get_client_status(self) -> dict:
        """Get WhatsApp client status"""
        url = self._url(f"/api/sessions/{self.phone_number}")
        response = requests.get(url, headers=self._headers())

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        status_data = response.json()

        # Update connection status in local DB
        # WAHA uses 'status' field: STOPPED, STARTING, SCAN_QR_CODE, WORKING, FAILED
        is_connected = status_data.get("status") == "WORKING"
        self._update_connection_status(is_connected)

        return status_data

    def disconnect_client(self) -> dict:
        """Disconnect WhatsApp client"""
        url = self._url(f"/api/sessions/{self.phone_number}/logout")
        response = requests.post(url, headers=self._headers())

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        # Update connection status to false
        self._update_connection_status(False)

        return response.json()

    def stop_session(self) -> dict:
        """Stop and delete WhatsApp session in WAHA Pro"""
        url = self._url(f"/api/sessions/{self.phone_number}/stop")
        response = requests.post(url, headers=self._headers())

        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        # Update connection status to false
        self._update_connection_status(False)

        return response.json()

    def _fake_group_sid(self, phone_number: str, force_send_to_group: bool) -> dict | None:
        if len(phone_number) <= 15 or force_send_to_group is not False:
            return None
        logger.info(f"Cannot send message to group: {phone_number} (force_send_to_group=False)")
        fake_sid = f"fake_sid_{str(uuid.uuid4())}"
        return {"sid": fake_sid}

    def _format_chat_id(self, phone_number: str) -> str:
        if "@" in phone_number:
            return phone_number
        if len(phone_number) < 15:
            return f"{phone_number}@c.us"
        return f"{phone_number}@g.us"

    @staticmethod
    def _extract_sent_message_id(result: dict) -> str:
        """Extract the WhatsApp message id from a WAHA send response, across engines.

        NOWEB/GOWS: {"key": {"id": "3EB..."}, ...}
        WEBJS:      {"id": {"id": "3EB...", "_serialized": "true_..._3EB..."}}

        Returning the real id (not a random UUID) is critical: it is stored as the
        message sid, and the inbound fromMe echo is de-duplicated against it. A wrong
        id means the echo is saved as a fresh "manager" message and blocks the next
        AI reply (block_ai_response).
        """
        if not isinstance(result, dict):
            return str(uuid.uuid4())
        key = result.get("key")
        if isinstance(key, dict) and key.get("id"):
            return key["id"]
        rid = result.get("id")
        if isinstance(rid, dict):
            return rid.get("id") or rid.get("_serialized") or str(uuid.uuid4())
        if isinstance(rid, str) and rid:
            return rid
        return str(uuid.uuid4())

    def _send_media_urls(
        self, phone_number: str, message: str, media_urls: list[str], force_send_to_group: bool
    ) -> list:
        media_sids = []
        caption = message.strip()
        for index, media_url in enumerate(media_urls):
            try:
                media_result = self.send_whatsapp_file(
                    phone_number=phone_number,
                    message=caption if index == 0 else "",
                    media_url=media_url,
                    force_send_to_group=force_send_to_group,
                )
                media_sids.append(media_result.get("sid"))
                logger.info(f"Media sent successfully: {media_url}")
            except Exception as e:
                logger.error(f"Failed to send media {media_url}: {str(e)}")
        return media_sids

    def _send_text_message(self, phone_number: str, message: str, link_preview: bool = True) -> str | None:
        """Send a plain WAHA text message.

        ``link_preview=False`` hides the preview card WhatsApp renders above the message for the
        first URL (passed through ``send_whatsapp_message`` kwargs).
        """
        if not message.strip():
            return None

        payload = {"chatId": phone_number, "text": message, "session": self.phone_number}
        if not link_preview:
            payload["linkPreview"] = False
        if "@g.us" in phone_number:
            mentions = self.extract_mentions(message)
            if mentions:
                payload["mentions"] = mentions

        response = requests.post(self._url("/api/sendText"), json=payload, headers=self._headers())
        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        result = response.json()
        return self._extract_sent_message_id(result)

    def send_whatsapp_message(self, phone_number: str, message: str, **kwargs) -> dict:
        """Send WhatsApp message
        Right now, cannot send message in the group
        If @phone_number in message and its length > 15, we consider it a mention to a group
        Automatically detects and sends media files if URLs are found in message
        """
        # Skip if phone number looks like an unresolved LID
        if "@lid" in str(phone_number):
            raise HTTPException(status_code=400, detail="Cannot send message to unresolved LID")

        force_send_to_group = kwargs.get("force_send_to_group", False)
        if fake_response := self._fake_group_sid(phone_number, force_send_to_group):
            return fake_response

        media_urls = _extract_media_urls(message)
        if media_urls:
            message = _clean_message_from_urls(message, media_urls)

        phone_number = self._format_chat_id(phone_number)

        # When send_whatsapp_message receives text that includes media URLs,
        # send the first media with the cleaned text as a WAHA caption.
        media_sids = self._send_media_urls(phone_number, message, media_urls, force_send_to_group)
        if media_sids:
            primary_sid = media_sids[0]
            return {"sid": primary_sid, "fake_sid": primary_sid, "media_sids": media_sids, "text_sid": None}

        text_sid = self._send_text_message(phone_number, message, link_preview=kwargs.get("link_preview", True))
        primary_sid = text_sid if text_sid else (media_sids[0] if media_sids else str(uuid.uuid4()))

        return {"sid": primary_sid, "fake_sid": primary_sid, "media_sids": media_sids, "text_sid": text_sid}

    def extract_mentions(self, message):
        mentions = []
        words = message.split()
        for word in words:
            # Look for @phone_number pattern (@ followed by digits)
            if word.startswith("@") and len(word) > 1:
                # Extract the phone number (remove the @)
                phone = word[1:].strip()
                # Remove any non-digit characters except + at the start
                phone = "".join(c for c in phone if c.isdigit() or (c == "+" and phone.index(c) == 0))

                if phone and len(phone) >= 10:  # Valid phone number length
                    # Transform to WhatsApp format: phone_number@c.us
                    mention_id = f"{phone}@c.us"
                    mentions.append(mention_id)
        return mentions

    # Known media file extensions
    MEDIA_EXTENSIONS = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
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

    @staticmethod
    def _to_direct_download_url(url: str) -> str:
        """Convert cloud-storage sharing URLs to direct-download equivalents.

        Supported services:
        - Google Drive  (/file/d/ID/view  or  /open?id=ID)
        - Dropbox       (?dl=0 → ?dl=1)
        - OneDrive      (?download=1 appended)

        Returns the original URL unchanged if no conversion applies.
        """
        import re

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

    def _is_cloud_storage_url(self, url: str) -> bool:
        """Return True if the URL is a known cloud-storage sharing link."""
        import re

        return bool(
            re.search(self._GDRIVE_FILE_RE, url)
            or re.search(self._GDRIVE_OPEN_RE, url)
            or re.search(self._DROPBOX_RE, url)
            or re.search(self._ONEDRIVE_RE, url)
        )

    def extract_media_urls(self, message: str) -> list:
        """Extract media URLs from message (images, videos, documents).

        Detection works in three passes:
        1. Known cloud-storage sharing URLs (Google Drive, Dropbox, OneDrive)
           are accepted immediately — they always point to a file.
        2. URLs with a known media extension are matched instantly (no HTTP call).
        3. All remaining URLs are probed with a HEAD request; if the Content-Type
           or Content-Disposition header indicates a downloadable file the URL is
           included.
        """
        import re

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
            if self._is_cloud_storage_url(url):
                media_urls.append(url)
                continue

            # Pass 2: URL path has a known media extension
            url_path = url.lower().split("?")[0].split("#")[0]
            has_known_ext = any(url_path.endswith(ext) for ext in self.MEDIA_EXTENSIONS)

            if has_known_ext:
                media_urls.append(url)
            else:
                # Pass 3: probe with HEAD to check Content-Type
                if self._is_media_url(url):
                    media_urls.append(url)

        return media_urls

    def _is_media_url(self, url: str) -> bool:
        """Return True if *url* points to a downloadable media / document file.

        Performs a lightweight HEAD request first; falls back to a streamed GET
        for servers that do not support HEAD.
        """
        try:
            # Convert cloud URLs before probing
            probe_url = self._to_direct_download_url(url)

            resp = requests.head(probe_url, timeout=5, allow_redirects=True)
            if resp.status_code >= 400:
                resp = requests.get(probe_url, timeout=5, stream=True, allow_redirects=True)

            content_type = resp.headers.get("Content-Type", "").lower()
            for media_type in self.MEDIA_CONTENT_TYPES:
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

    def _resolve_filename(self, media_url: str, mimetype: str, download_filename: str | None = None) -> str:
        """Build a human-readable filename for the file being sent.

        Resolution order:
        1. Filename extracted from Content-Disposition during download.
        2. Filename parsed from the URL path (last segment before query string).
        3. Fallback: generated from mimetype (e.g. "document.xlsx").
        """
        import mimetypes
        from urllib.parse import unquote, urlparse

        # 1. Use download_filename from Content-Disposition if available
        if download_filename:
            return download_filename

        # 2. Try to extract from URL path
        try:
            parsed = urlparse(media_url)
            path = unquote(parsed.path)
            url_filename = path.rsplit("/", 1)[-1] if "/" in path else ""
            # Accept it only if it contains a dot (extension) and isn't empty
            if url_filename and "." in url_filename:
                return url_filename
        except Exception:
            pass

        # 3. Fallback: build filename from mimetype
        ext = mimetypes.guess_extension(mimetype.split(";")[0].strip()) if mimetype else None

        # mimetypes returns some odd defaults — fix common ones
        EXT_OVERRIDES = {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/vnd.ms-excel": ".xls",
            "application/msword": ".doc",
            "application/vnd.ms-powerpoint": ".ppt",
        }
        clean_mime = mimetype.split(";")[0].strip().lower() if mimetype else ""
        ext = EXT_OVERRIDES.get(clean_mime, ext) or ".bin"

        return f"file{ext}"

    def clean_message_from_urls(self, message: str, media_urls: list) -> str:
        """Remove media URLs and markdown links from message"""
        import re

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

    def send_whatsapp_file(self, phone_number: str, message: str, media_url: str, **kwargs) -> dict:
        """Send a file via WhatsApp using URL or base64
        Automatically detects file type and uses appropriate WAHA endpoint:
        - /api/sendImage for images (jpg, jpeg, png, gif, webp)
        - /api/sendVideo for videos (mp4, avi, mov, mkv)
        - /api/sendVoice for audio/voice (mp3, ogg, opus, wav, m4a, aac)
        - /api/sendFile for documents (pdf, doc, docx, xls, xlsx, etc.)

        Supports both URL and base64 formats.
        Right now, cannot send message in the group
        """
        import base64
        import re

        force_send_to_group = kwargs.get("force_send_to_group", False)
        phone_clean = phone_number.replace("@c.us", "")
        if len(phone_clean) > 15 and force_send_to_group is False:
            logger.info(f"Cannot send file to group: {phone_number} (force_send_to_group=False)")
            fake_sid = f"fake_sid_{str(uuid.uuid4())}"
            return {"sid": fake_sid}

        if "@" not in phone_number:
            if len(phone_number) < 15:
                phone_number = f"{phone_number}@c.us"
            else:
                phone_number = f"{phone_number}@g.us"

        # Detect if it's base64 data
        is_base64 = False
        file_base64 = None
        mimetype = kwargs.get("mimetype")

        if media_url.startswith("data:"):
            # Format: data:image/jpeg;base64,/9j/4AAQSkZJRg...
            is_base64 = True
            match = re.match(r"data:([^;]+);base64,(.+)", media_url)
            if match:
                mimetype = match.group(1)
                file_base64 = match.group(2)
            else:
                raise HTTPException(status_code=400, detail="Invalid base64 data format")
        elif not media_url.startswith("http://") and not media_url.startswith("https://"):
            # Assume it's raw base64 without data: prefix
            is_base64 = True
            file_base64 = media_url
            if not mimetype:
                mimetype = "application/octet-stream"

        # Download file and convert to base64 if it's a URL
        download_filename = None  # filename obtained from the download
        if not is_base64:
            try:
                file_bytes, download_filename, mimetype_detected = _download_media_file(media_url)
                if not mimetype:
                    mimetype = mimetype_detected

                # Convert HEIC/HEIF to JPEG — WAHA cannot render HEIC as an inline image
                file_bytes, download_filename, converted_mime = _convert_heic_to_jpeg(file_bytes, download_filename)
                if converted_mime:
                    mimetype = converted_mime

                file_base64 = base64.b64encode(file_bytes).decode("utf-8")
            except Exception as e:
                logger.error(f"Failed to download file from {media_url}: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")

        # ---------- Detect file type from mimetype or URL extension ----------
        # Mapping of mimetypes to proper WAHA category
        MIMETYPE_TO_TYPE = {
            "image/jpeg": "image",
            "image/png": "image",
            "image/gif": "image",
            "image/webp": "image",
            "image/bmp": "image",
            "video/mp4": "video",
            "video/avi": "video",
            "video/quicktime": "video",
            "video/x-msvideo": "video",
            "video/x-matroska": "video",
            "video/webm": "video",
            "audio/mpeg": "voice",
            "audio/ogg": "voice",
            "audio/wav": "voice",
            "audio/x-wav": "voice",
            "audio/mp4": "voice",
            "audio/aac": "voice",
            "application/pdf": "document",
            "application/msword": "document",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
            "application/vnd.ms-excel": "document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
            "application/vnd.ms-powerpoint": "document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
            "text/csv": "document",
            "text/plain": "document",
            "application/zip": "document",
            "application/x-rar-compressed": "document",
        }

        file_type = "document"  # default

        if mimetype:
            # Exact match first
            clean_mime = mimetype.split(";")[0].strip().lower()
            if clean_mime in MIMETYPE_TO_TYPE:
                file_type = MIMETYPE_TO_TYPE[clean_mime]
            elif clean_mime.startswith("image/"):
                file_type = "image"
            elif clean_mime.startswith("video/"):
                file_type = "video"
            elif clean_mime.startswith("audio/") or "ogg" in clean_mime:
                file_type = "voice"
        else:
            # Fallback: detect from URL extension
            url_lower = media_url.lower()
            if any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                file_type = "image"
                mimetype = "image/jpeg"
            elif any(ext in url_lower for ext in [".mp4", ".avi", ".mov", ".mkv", ".webm"]):
                file_type = "video"
                mimetype = "video/mp4"
            elif any(ext in url_lower for ext in [".mp3", ".ogg", ".opus", ".wav", ".m4a", ".aac"]):
                file_type = "voice"
                mimetype = "audio/ogg; codecs=opus"
            elif any(ext in url_lower for ext in [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt"]):
                file_type = "document"
                if ".pdf" in url_lower:
                    mimetype = "application/pdf"

        # Select appropriate endpoint based on file type
        endpoint_map = {
            "image": "/api/sendImage",
            "video": "/api/sendVideo",
            "voice": "/api/sendVoice",
            "document": "/api/sendFile",
        }

        endpoint = endpoint_map.get(file_type, "/api/sendFile")
        url = self._url(endpoint)

        # Resolve a filename for the file (required by WAHA for documents)
        filename = self._resolve_filename(media_url, mimetype, download_filename if not is_base64 else None)

        # Build payload
        payload = {
            "chatId": phone_number,
            "file": {"mimetype": mimetype, "filename": filename, "data": file_base64},
            "session": self.phone_number,
        }

        # Add caption/message if provided
        if message:
            payload["caption"] = message

        # Add convert flag for voice and video
        if file_type in ["voice", "video"]:
            payload["convert"] = kwargs.get("convert", False)

        try:
            response = requests.post(url, json=payload, headers=self._headers())

            if response.status_code not in [200, 201]:
                logger.error(f"Failed to send {file_type} via {endpoint}: status={response.status_code}")
                raise HTTPException(status_code=response.status_code, detail=response.text)

            result = response.json()
            sid = self._extract_sent_message_id(result)
            fake_sid = result.get("id", {}).get("_serialized") if isinstance(result.get("id"), dict) else sid
            return {"sid": sid, "fake_sid": fake_sid or sid}
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send {file_type} via {endpoint}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to send {file_type}: {str(e)}")

    def get_group_info(self, group_id: str) -> dict:
        """Get group info"""
        url = self._url(f"/api/{self.phone_number}/groups/{group_id}")
        response = requests.get(url, headers=self._headers())
        try:
            return response.json()
        except Exception as e:
            logger.error(f"Error getting group info: {e}")
            return {}

    def get_phone_number_by_lid(self, lid: str) -> dict:
        """Get phone number by LID"""
        if "@" in lid:
            lid = lid.split("@")[0]

        url = self._url(f"/api/{self.phone_number}/lids/{lid}")
        response = requests.get(url, headers=self._headers())

        if response.status_code != 200:
            return {}

        return response.json()

    def get_chat_info(self, chat_id: str) -> dict:
        """Get chat information including archive status"""
        if "@" not in chat_id:
            chat_id = f"{chat_id}@c.us"

        # WAHA doesn't have direct chat info endpoint
        # Use chats/overview with ids filter instead
        url = self._url(f"/api/{self.phone_number}/chats/overview")
        params = {"ids": chat_id}
        response = requests.get(url, params=params, headers=self._headers())

        if response.status_code != 200:
            return {}

        try:
            result = response.json()
            # Return first chat if found, otherwise empty dict
            return result[0] if result and len(result) > 0 else {}
        except Exception as e:
            logger.error(f"Error getting chat info: {e}")
            return {}
