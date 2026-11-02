"""WahaService: the WAHA HTTP API (sessions, sending text / media, webhooks) for a tenant's WhatsApp number."""

import logging
import time
import uuid
from datetime import datetime, timezone
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
]

# How long "show QR" waits for a freshly (re)started session to reach SCAN_QR_CODE (NOWEB needs ~1-3 s).
QR_WAIT_SECONDS = 15


def get_waha_base_url() -> str:
    """Base URL of the WAHA server (WAHA_BASE_URL, http://waha:3000 inside docker compose)."""
    return getenv("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")


def get_waha_webhook_base_url() -> str:
    """Base URL WAHA posts webhooks to: WAHA_WEBHOOK_BASE_URL when set (e.g. http://fastapi:8000 when
    WAHA shares the docker network), otherwise the public BACKEND_URL."""
    return (getenv("WAHA_WEBHOOK_BASE_URL") or getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")


class WahaService:
    """HTTP client of one WAHA session (the tenant's connected WhatsApp number): session lifecycle, QR, status,
    sending text and files, LID resolution.
    """
    def __init__(self, tenant_name: str, phone_number: str, **kwargs):
        """Resolve the account of the number (falling back to the tenant's sole account); needs WAHA_API_KEY."""
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
        self.base_url = get_waha_base_url()

    def _get_account_info(self) -> WahaAccount:
        """Get WAHA account info from TenantDocument"""
        tenant = TenantDocument.objects(name=self.tenant_name).first()
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        account = tenant.get_waha_by_phone_number(self.phone_number)
        if not account:
            # The connection key must be the connected account number. Occasionally a
            # recipient number leaks into this slot from the message pipeline and it
            # never identifies an account. When the tenant has
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
        """Full URL of a WAHA endpoint."""
        return f"{self.base_url}{endpoint}"

    def _headers(self) -> dict:
        """Get headers with API Key for WAHA Pro"""
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _update_account(self, **fields):
        """Set fields on this number's WahaAccount in the tenant document and save."""
        tenant = TenantDocument.objects(name=self.tenant_name).first()
        if not tenant:
            return
        for account in tenant.waha_accounts:
            if account.phone_number.replace("+", "") == self.phone_number.replace("+", ""):
                for key, value in fields.items():
                    setattr(account, key, value)
                tenant.save()
                break

    def _record_status(self, status: str | None):
        """Mirror a status WAHA reported into the account (webhooks do the same, but a session's webhook can point
        at another tenant, so the dashboard must not rely on them alone)."""
        if status:
            self._update_account(
                is_connected=status == "WORKING", session_status=status, last_status_update=datetime.now(timezone.utc)
            )

    def _webhook_url(self) -> str:
        """Where WAHA posts this tenant's events."""
        return f"{get_waha_webhook_base_url()}/api/async/waha/{self.tenant_name}"

    def _session_config(self, webhook_url: str) -> dict:
        """The session config Maia needs: our webhook with its events, the tenant name, a NOWEB store."""
        return {
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

    def get_session(self) -> dict | None:
        """The WAHA session of this number (`status`, `config`, `me`...), or None when WAHA has no such session."""
        response = requests.get(self._url(f"/api/sessions/{self.phone_number}"), headers=self._headers())
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json()

    def _start_or_restart(self, status: str | None):
        """Bring a STOPPED session up (`/start`) or a FAILED one back (`/restart`; WAHA refuses `/start` on it).
        Any other status is already running."""
        if status == "STOPPED":
            endpoint = "start"
        elif status == "FAILED":
            endpoint = "restart"
        else:
            return
        response = requests.post(self._url(f"/api/sessions/{self.phone_number}/{endpoint}"), headers=self._headers())
        # 422 = "already started": someone else won the race, which is fine.
        if response.status_code not in [200, 201, 422]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

    def create_whatsapp_client(self) -> dict:
        """Create (or re-point) the WAHA session of this number with this tenant's webhook, and start it.

        A session that already exists keeps nothing from before: `PUT /api/sessions/{name}` replaces its config
        (WAHA restarts it when it is running), then a STOPPED/FAILED one is started. The old
        `POST /api/sessions/start` started an existing session with its stale config, i.e. with another tenant's
        webhook when the number had been linked elsewhere.
        """
        webhook_url = self._webhook_url()
        config = self._session_config(webhook_url)
        session = self.get_session()

        if session is None:
            payload = {"name": self.phone_number, "start": True, "config": config}
            response = requests.post(self._url("/api/sessions"), json=payload, headers=self._headers())
            if response.status_code not in [200, 201]:
                raise HTTPException(status_code=response.status_code, detail=response.text)
        else:
            self.set_webhook(webhook_url)
            self._start_or_restart((self.get_session() or {}).get("status"))

        self._update_account(webhook_url=webhook_url)
        session = self.get_session() or {"name": self.phone_number}
        self._record_status(session.get("status"))
        return session

    def get_qr_code(self) -> dict:
        """The QR image (JSON with base64 `data`) the owner scans from the phone.

        WAHA drops an unscanned QR after ~2.5 minutes and marks the session FAILED, and refuses a QR in any status
        but SCAN_QR_CODE; so a missing session is created, a STOPPED/FAILED one is (re)started, and the call waits
        up to QR_WAIT_SECONDS for SCAN_QR_CODE. 409 when the number is already connected (WORKING).
        """
        session = self.get_session()
        if session is None:
            self.create_whatsapp_client()
        else:
            self._start_or_restart(session.get("status"))

        status = None
        deadline = time.monotonic() + QR_WAIT_SECONDS
        while time.monotonic() < deadline:
            status = (self.get_session() or {}).get("status")
            if status == "SCAN_QR_CODE":
                break
            if status == "WORKING":
                self._record_status(status)
                raise HTTPException(status_code=409, detail="This number is already connected (WORKING); no QR is needed")
            time.sleep(0.5)
        self._record_status(status)
        if status != "SCAN_QR_CODE":
            raise HTTPException(
                status_code=503,
                detail=f"WAHA session is {status or 'missing'} and did not reach SCAN_QR_CODE within {QR_WAIT_SECONDS}s; try again",
            )

        headers = self._headers()
        headers["Accept"] = "application/json"
        response = requests.get(self._url(f"/api/{self.phone_number}/auth/qr"), headers=headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"WAHA did not return a QR (session status {status}): {response.text[:200]}",
            )
        return response.json()

    def set_webhook(self, webhook_url: str) -> dict:
        """Replace the session config with ours (`PUT`; WAHA restarts a running session) and remember the URL."""
        payload = {"config": self._session_config(webhook_url)}
        response = requests.put(self._url(f"/api/sessions/{self.phone_number}"), json=payload, headers=self._headers())

        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        self._update_account(webhook_url=webhook_url)
        return response.json()

    def get_client_status(self) -> dict:
        """The session as WAHA sees it; its status (STOPPED, STARTING, SCAN_QR_CODE, WORKING, FAILED) is stored."""
        url = self._url(f"/api/sessions/{self.phone_number}")
        response = requests.get(url, headers=self._headers())

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        status_data = response.json()
        self._record_status(status_data.get("status"))
        return status_data

    def disconnect_client(self) -> dict:
        """Disconnect WhatsApp client"""
        url = self._url(f"/api/sessions/{self.phone_number}/logout")
        response = requests.post(url, headers=self._headers())

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        self._update_account(is_connected=False)

        return response.json()

    def stop_session(self) -> dict:
        """Stop and delete WhatsApp session in WAHA Pro"""
        url = self._url(f"/api/sessions/{self.phone_number}/stop")
        response = requests.post(url, headers=self._headers())

        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        self._update_account(is_connected=False, session_status="STOPPED", last_status_update=datetime.now(timezone.utc))

        return response.json()

    def _format_chat_id(self, phone_number: str) -> str:
        """Phone number to WhatsApp chat id (number@c.us), unless it already is one."""
        if "@" in phone_number:
            return phone_number
        return f"{phone_number}@c.us"

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

    def _send_media_urls(self, phone_number: str, message: str, media_urls: list[str]) -> list:
        """Send each media URL found in the reply as a file; the cleaned text becomes the caption of the first one.
        Returns the sids.
        """
        media_sids = []
        caption = message.strip()
        for index, media_url in enumerate(media_urls):
            try:
                media_result = self.send_whatsapp_file(
                    phone_number=phone_number,
                    message=caption if index == 0 else "",
                    media_url=media_url,
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

        response = requests.post(self._url("/api/sendText"), json=payload, headers=self._headers())
        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        result = response.json()
        return self._extract_sent_message_id(result)

    def send_whatsapp_message(self, phone_number: str, message: str, **kwargs) -> dict:
        """Send WhatsApp message
        Automatically detects and sends media files if URLs are found in message
        """
        # Skip if phone number looks like an unresolved LID
        if "@lid" in str(phone_number):
            raise HTTPException(status_code=400, detail="Cannot send message to unresolved LID")

        media_urls = _extract_media_urls(message)
        if media_urls:
            message = _clean_message_from_urls(message, media_urls)

        phone_number = self._format_chat_id(phone_number)

        # When send_whatsapp_message receives text that includes media URLs,
        # send the first media with the cleaned text as a WAHA caption.
        media_sids = self._send_media_urls(phone_number, message, media_urls)
        if media_sids:
            primary_sid = media_sids[0]
            return {"sid": primary_sid, "fake_sid": primary_sid, "media_sids": media_sids, "text_sid": None}

        text_sid = self._send_text_message(phone_number, message, link_preview=kwargs.get("link_preview", True))
        primary_sid = text_sid if text_sid else (media_sids[0] if media_sids else str(uuid.uuid4()))

        return {"sid": primary_sid, "fake_sid": primary_sid, "media_sids": media_sids, "text_sid": text_sid}

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

    def send_whatsapp_file(self, phone_number: str, message: str, media_url: str, **kwargs) -> dict:
        """Send a file via WhatsApp using URL or base64
        Automatically detects file type and uses appropriate WAHA endpoint:
        - /api/sendImage for images (jpg, jpeg, png, gif, webp)
        - /api/sendVideo for videos (mp4, avi, mov, mkv)
        - /api/sendVoice for audio/voice (mp3, ogg, opus, wav, m4a, aac)
        - /api/sendFile for documents (pdf, doc, docx, xls, xlsx, etc.)

        Supports both URL and base64 formats.
        """
        import base64
        import re

        phone_number = self._format_chat_id(phone_number)

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
