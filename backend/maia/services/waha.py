"""WahaService: the WAHA HTTP API (sessions, sending text / media, webhooks) for a tenant's WhatsApp number."""

import base64
import logging
import mimetypes
import re
import time
import uuid
from datetime import datetime, timezone
from os import getenv
from urllib.parse import unquote, urlparse

import requests
from fastapi import HTTPException

from maia.models.tenants import WahaAccount, TenantDocument
from maia.utils.media_url_utils import strip_media_urls_from_message as _clean_message_from_urls
from maia.utils.media_url_utils import convert_heic_to_jpeg_if_needed as _convert_heic_to_jpeg
from maia.utils.media_url_utils import download_media_file_with_metadata as _download_media_file
from maia.utils.media_url_utils import collect_media_urls_from_message as _extract_media_urls

logger = logging.getLogger(__name__)

# Events Maia subscribes to on every WAHA session. Kept in one place so
# session-create and webhook-update never drift apart.
SUBSCRIBED_WEBHOOK_EVENTS = [
    "message.any",
    "session.status",
]

# How long "show QR" waits for a freshly (re)started session to reach SCAN_QR_CODE (NOWEB needs ~1-3 s).
QR_READY_TIMEOUT_SECONDS = 15

# Extension of a sent file's name, by bare content type, where mimetypes.guess_extension is missing or wrong.
# mimetypes returns some odd defaults — fix common ones
CONTENT_TYPE_EXTENSION_OVERRIDES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/msword": ".doc",
    "application/vnd.ms-powerpoint": ".ppt",
}

# Mapping of mimetypes to proper WAHA category
CONTENT_TYPE_TO_WAHA_FILE_TYPE = {
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

# WAHA send endpoint of each file category (anything else goes to /api/sendFile).
WAHA_SEND_ENDPOINTS = {
    "image": "/api/sendImage",
    "video": "/api/sendVideo",
    "voice": "/api/sendVoice",
    "document": "/api/sendFile",
}


def resolve_waha_base_url() -> str:
    """Base URL of the WAHA server (WAHA_BASE_URL, http://waha:3000 inside docker compose)."""
    return getenv("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")


def resolve_waha_webhook_base_url() -> str:
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
        self.account = self._resolve_waha_account()
        # When _resolve_waha_account fell back to the tenant's sole account (the
        # requested number was a recipient or the default server, not a session name),
        # align self.phone_number with it — every WAHA call uses
        # self.phone_number as the session, and a stale value produces
        # 422 "Session does not exist" on send. _resolve_waha_account never returns
        # None (it raises 404), and WahaAccount.phone_number is a required
        # field on the embedded document, so this attribute access is safe.
        account_phone_number = getattr(self.account, "phone_number", None)
        if account_phone_number and account_phone_number != self.phone_number:
            self.phone_number = account_phone_number
        self.base_url = resolve_waha_base_url()

    def _resolve_waha_account(self) -> WahaAccount:
        """Get WAHA account info from TenantDocument"""
        tenant_doc = TenantDocument.objects(name=self.tenant_name).first()
        if not tenant_doc:
            raise HTTPException(status_code=404, detail="Tenant not found")

        waha_account = tenant_doc.find_waha_account_by_phone_number(self.phone_number)
        if not waha_account:
            # The connection key must be the connected account number. Occasionally a
            # recipient number leaks into this slot from the message pipeline and it
            # never identifies an account. When the tenant has
            # a single connected account, fall back to it so the reply is still
            # delivered instead of failing with a 404 and dropping the AI response.
            waha_accounts = tenant_doc.waha_accounts or []
            if len(waha_accounts) == 1:
                waha_account = waha_accounts[0]
                logger.warning(f"WAHA connection '{self.phone_number}' not found for tenant "
                    f"'{self.tenant_name}'; falling back to sole account '{waha_account.phone_number}'")
        if not waha_account:
            raise HTTPException(status_code=404, detail=f"WAHA connection '{self.phone_number}' not found")

        return waha_account

    def _build_waha_url(self, endpoint_path: str) -> str:
        """Full URL of a WAHA endpoint."""
        return f"{self.base_url}{endpoint_path}"

    def _build_api_headers(self) -> dict:
        """Get headers with API Key for WAHA Pro"""
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _persist_account_fields(self, **field_values):
        """Set fields on this number's WahaAccount in the tenant document and save."""
        tenant_doc = TenantDocument.objects(name=self.tenant_name).first()
        if not tenant_doc:
            return
        for waha_account in tenant_doc.waha_accounts:
            if waha_account.phone_number.replace("+", "") == self.phone_number.replace("+", ""):
                for field_name, field_value in field_values.items():
                    setattr(waha_account, field_name, field_value)
                tenant_doc.save()
                break

    def _record_session_status(self, session_status: str | None):
        """Mirror a status WAHA reported into the account (webhooks do the same, but a session's webhook can point
        at another tenant, so the dashboard must not rely on them alone)."""
        if session_status:
            self._persist_account_fields(
                is_connected=session_status == "WORKING", session_status=session_status, last_status_update=datetime.now(timezone.utc)
            )

    def _build_tenant_webhook_url(self) -> str:
        """Where WAHA posts this tenant's events."""
        return f"{resolve_waha_webhook_base_url()}/api/async/waha/{self.tenant_name}"

    def _build_session_config(self, tenant_webhook_url: str) -> dict:
        """The session config Maia needs: our webhook with its events, the tenant name, a NOWEB store."""
        return {
            "webhooks": [
                {
                    "url": tenant_webhook_url,
                    "events": SUBSCRIBED_WEBHOOK_EVENTS,
                    "customHeaders": [{"name": "X-Tenant-Name", "value": self.tenant_name}],
                }
            ],
            "metadata": {"tenant_name": self.tenant_name},
            "noweb": {"store": {"enabled": True, "fullSync": False}},
        }

    def find_session(self) -> dict | None:
        """The WAHA session of this number (`status`, `config`, `me`...), or None when WAHA has no such session."""
        response = requests.get(self._build_waha_url(f"/api/sessions/{self.phone_number}"), headers=self._build_api_headers())
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json()

    def _start_session_if_stopped_or_failed(self, session_status: str | None):
        """Bring a STOPPED session up (`/start`) or a FAILED one back (`/restart`; WAHA refuses `/start` on it).
        Any other status is already running."""
        if session_status == "STOPPED":
            lifecycle_action = "start"
        elif session_status == "FAILED":
            lifecycle_action = "restart"
        else:
            return
        response = requests.post(self._build_waha_url(f"/api/sessions/{self.phone_number}/{lifecycle_action}"), headers=self._build_api_headers())
        # 422 = "already started": someone else won the race, which is fine.
        if response.status_code not in [200, 201, 422]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

    def _create_session(self, session_config: dict) -> None:
        """Create this number's WAHA session with the given config and start it (`POST /api/sessions`)."""
        create_payload = {"name": self.phone_number, "start": True, "config": session_config}
        response = requests.post(self._build_waha_url("/api/sessions"), json=create_payload, headers=self._build_api_headers())
        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

    def provision_session(self) -> dict:
        """Create (or re-point) the WAHA session of this number with this tenant's webhook, and start it.

        A session that already exists keeps nothing from before: `PUT /api/sessions/{name}` replaces its config
        (WAHA restarts it when it is running), then a STOPPED/FAILED one is started. The old
        `POST /api/sessions/start` started an existing session with its stale config, i.e. with another tenant's
        webhook when the number had been linked elsewhere.
        """
        tenant_webhook_url = self._build_tenant_webhook_url()
        session_config = self._build_session_config(tenant_webhook_url)
        existing_session = self.find_session()

        if existing_session is None:
            self._create_session(session_config)
        else:
            self.replace_session_config(tenant_webhook_url)
            self._start_session_if_stopped_or_failed((self.find_session() or {}).get("status"))

        self._persist_account_fields(webhook_url=tenant_webhook_url)
        provisioned_session = self.find_session() or {"name": self.phone_number}
        self._record_session_status(provisioned_session.get("status"))
        return provisioned_session

    def _wait_for_scan_qr_status(self) -> str:
        """Poll the session until it reaches SCAN_QR_CODE (at most QR_READY_TIMEOUT_SECONDS) and record the last
        status seen; returns that status. 409 when the number is already connected (WORKING), 503 on timeout.
        """
        session_status = None
        wait_deadline = time.monotonic() + QR_READY_TIMEOUT_SECONDS
        while time.monotonic() < wait_deadline:
            session_status = (self.find_session() or {}).get("status")
            if session_status == "SCAN_QR_CODE":
                break
            if session_status == "WORKING":
                self._record_session_status(session_status)
                raise HTTPException(status_code=409, detail="This number is already connected (WORKING); no QR is needed")
            time.sleep(0.5)
        self._record_session_status(session_status)
        if session_status != "SCAN_QR_CODE":
            raise HTTPException(
                status_code=503,
                detail=f"WAHA session is {session_status or 'missing'} and did not reach SCAN_QR_CODE within {QR_READY_TIMEOUT_SECONDS}s; try again",
            )
        return session_status

    def _fetch_qr_image(self, session_status: str | None) -> dict:
        """WAHA's QR of this session (JSON with base64 `data`); a non-200 answer is raised with its status code, the
        session status and the first 200 characters of WAHA's body."""
        qr_request_headers = self._build_api_headers()
        qr_request_headers["Accept"] = "application/json"
        response = requests.get(self._build_waha_url(f"/api/{self.phone_number}/auth/qr"), headers=qr_request_headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"WAHA did not return a QR (session status {session_status}): {response.text[:200]}",
            )
        return response.json()

    def fetch_qr_code_when_ready(self) -> dict:
        """The QR image (JSON with base64 `data`) the owner scans from the phone.

        WAHA drops an unscanned QR after ~2.5 minutes and marks the session FAILED, and refuses a QR in any status
        but SCAN_QR_CODE; so a missing session is created, a STOPPED/FAILED one is (re)started, and the call waits
        up to QR_READY_TIMEOUT_SECONDS for SCAN_QR_CODE. 409 when the number is already connected (WORKING).
        """
        existing_session = self.find_session()
        if existing_session is None:
            self.provision_session()
        else:
            self._start_session_if_stopped_or_failed(existing_session.get("status"))

        session_status = self._wait_for_scan_qr_status()
        return self._fetch_qr_image(session_status)

    def replace_session_config(self, tenant_webhook_url: str) -> dict:
        """Replace the session config with ours (`PUT`; WAHA restarts a running session) and remember the URL."""
        config_payload = {"config": self._build_session_config(tenant_webhook_url)}
        response = requests.put(self._build_waha_url(f"/api/sessions/{self.phone_number}"), json=config_payload, headers=self._build_api_headers())

        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        self._persist_account_fields(webhook_url=tenant_webhook_url)
        return response.json()

    def sync_session_status(self) -> dict:
        """The session as WAHA sees it; its status (STOPPED, STARTING, SCAN_QR_CODE, WORKING, FAILED) is stored."""
        session_url = self._build_waha_url(f"/api/sessions/{self.phone_number}")
        response = requests.get(session_url, headers=self._build_api_headers())

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        waha_session = response.json()
        self._record_session_status(waha_session.get("status"))
        return waha_session

    def logout_session(self) -> dict:
        """Disconnect WhatsApp client"""
        logout_url = self._build_waha_url(f"/api/sessions/{self.phone_number}/logout")
        response = requests.post(logout_url, headers=self._build_api_headers())

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        self._persist_account_fields(is_connected=False)

        return response.json()

    def stop_session_and_record_status(self) -> dict:
        """Stop and delete WhatsApp session in WAHA Pro"""
        stop_url = self._build_waha_url(f"/api/sessions/{self.phone_number}/stop")
        response = requests.post(stop_url, headers=self._build_api_headers())

        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        self._persist_account_fields(is_connected=False, session_status="STOPPED", last_status_update=datetime.now(timezone.utc))

        return response.json()

    def _normalize_chat_id(self, phone_or_chat_id: str) -> str:
        """Phone number to WhatsApp chat id (number@c.us), unless it already is one."""
        if "@" in phone_or_chat_id:
            return phone_or_chat_id
        return f"{phone_or_chat_id}@c.us"

    @staticmethod
    def _extract_message_id_from_response(send_response: dict) -> str:
        """Extract the WhatsApp message id from a WAHA send response, across engines.

        NOWEB/GOWS: {"key": {"id": "3EB..."}, ...}
        WEBJS:      {"id": {"id": "3EB...", "_serialized": "true_..._3EB..."}}

        Returning the real id (not a random UUID) is critical: it is stored as the
        message sid, and the inbound fromMe echo is de-duplicated against it. A wrong
        id means the echo is saved as a fresh "manager" message and blocks the next
        AI reply (block_ai_response).
        """
        if not isinstance(send_response, dict):
            return str(uuid.uuid4())
        message_key = send_response.get("key")
        if isinstance(message_key, dict) and message_key.get("id"):
            return message_key["id"]
        raw_id = send_response.get("id")
        if isinstance(raw_id, dict):
            return raw_id.get("id") or raw_id.get("_serialized") or str(uuid.uuid4())
        if isinstance(raw_id, str) and raw_id:
            return raw_id
        return str(uuid.uuid4())

    def _deliver_embedded_media(self, chat_id: str, message_text: str, media_urls: list[str]) -> list:
        """Send each media URL found in the reply as a file; the cleaned text becomes the caption of the first one.
        Returns the sids.
        """
        media_sids = []
        first_file_caption = message_text.strip()
        for index, media_url in enumerate(media_urls):
            try:
                send_result = self.deliver_file_to_customer(
                    recipient_address=chat_id,
                    message_text=first_file_caption if index == 0 else "",
                    media_url=media_url,
                )
                media_sids.append(send_result.get("sid"))
                logger.info(f"Media sent successfully: {media_url}")
            except Exception as error:
                logger.error(f"Failed to send media {media_url}: {str(error)}")
        return media_sids

    def _deliver_text_message(self, chat_id: str, message_text: str, should_show_link_preview: bool = True) -> str | None:
        """Send a plain WAHA text message.

        ``should_show_link_preview=False`` hides the preview card WhatsApp renders above the message for the
        first URL (passed through ``deliver_message_to_customer`` send_options as ``link_preview``).
        """
        if not message_text.strip():
            return None

        text_payload = {"chatId": chat_id, "text": message_text, "session": self.phone_number}
        if not should_show_link_preview:
            text_payload["linkPreview"] = False

        response = requests.post(self._build_waha_url("/api/sendText"), json=text_payload, headers=self._build_api_headers())
        if response.status_code not in [200, 201]:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        send_result = response.json()
        return self._extract_message_id_from_response(send_result)

    def deliver_message_to_customer(self, recipient_address: str, message_text: str, **send_options) -> dict:
        """Send WhatsApp message
        Automatically detects and sends media files if URLs are found in message
        """
        # Skip if phone number looks like an unresolved LID
        if "@lid" in str(recipient_address):
            raise HTTPException(status_code=400, detail="Cannot send message to unresolved LID")

        embedded_media_urls = _extract_media_urls(message_text)
        cleaned_text = message_text
        if embedded_media_urls:
            cleaned_text = _clean_message_from_urls(message_text, embedded_media_urls)

        chat_id = self._normalize_chat_id(recipient_address)

        # When deliver_message_to_customer receives text that includes media URLs,
        # send the first media with the cleaned text as a WAHA caption.
        media_sids = self._deliver_embedded_media(chat_id, cleaned_text, embedded_media_urls)
        if media_sids:
            primary_message_sid = media_sids[0]
            return {"sid": primary_message_sid, "fake_sid": primary_message_sid, "media_sids": media_sids, "text_sid": None}

        text_message_sid = self._deliver_text_message(chat_id, cleaned_text, should_show_link_preview=send_options.get("link_preview", True))
        primary_message_sid = text_message_sid if text_message_sid else (media_sids[0] if media_sids else str(uuid.uuid4()))

        return {"sid": primary_message_sid, "fake_sid": primary_message_sid, "media_sids": media_sids, "text_sid": text_message_sid}

    @staticmethod
    def _filename_from_url_path(media_source: str) -> str | None:
        """Last segment of the (percent-decoded) URL path when it looks like a filename (contains a dot); None
        otherwise, also when the source cannot be parsed."""
        try:
            parsed_url = urlparse(media_source)
            url_path = unquote(parsed_url.path)
            filename_from_url = url_path.rsplit("/", 1)[-1] if "/" in url_path else ""
            # Accept it only if it contains a dot (extension) and isn't empty
            if filename_from_url and "." in filename_from_url:
                return filename_from_url
        except Exception:
            pass
        return None

    @staticmethod
    def _extension_for_content_type(content_type: str | None) -> str:
        """File extension (with its dot, never empty) for a content type: mimetypes' guess corrected by
        CONTENT_TYPE_EXTENSION_OVERRIDES, '.bin' when the type is missing or unknown."""
        bare_content_type = (content_type or "").split(";")[0].strip().lower()
        guessed_extension = mimetypes.guess_extension(bare_content_type) if bare_content_type else None
        return CONTENT_TYPE_EXTENSION_OVERRIDES.get(bare_content_type, guessed_extension) or ".bin"

    def _derive_filename(self, media_url: str, content_type: str | None, downloaded_filename: str | None = None) -> str:
        """Build a human-readable filename for the file being sent.

        Resolution order:
        1. Filename extracted from Content-Disposition during download.
        2. Filename parsed from the URL path (last segment before query string).
        3. Fallback: generated from content_type (e.g. "document.xlsx").
        """
        # 1. Use downloaded_filename from Content-Disposition if available
        if downloaded_filename:
            return downloaded_filename

        # 2. Try to extract from URL path
        filename_from_url = self._filename_from_url_path(media_url)
        if filename_from_url:
            return filename_from_url

        # 3. Fallback: build filename from content_type
        return f"file{self._extension_for_content_type(content_type)}"

    @staticmethod
    def _decode_inline_media(media_source: str, content_type: str | None) -> tuple[str | None, str | None]:
        """Base64 payload of an inline file: a data:<type>;base64,<payload> URI (its own type wins; 400 when
        malformed) or raw base64 without prefix (application/octet-stream unless a type was given).

        Returns (file_base64, content_type); file_base64 is None for an http(s) URL, which still has to be
        downloaded (an empty source counts as raw base64 and gives "").
        """
        file_base64 = None
        if media_source.startswith("data:"):
            # Format: data:image/jpeg;base64,/9j/4AAQSkZJRg...
            data_uri_match = re.match(r"data:([^;]+);base64,(.+)", media_source)
            if data_uri_match:
                content_type = data_uri_match.group(1)
                file_base64 = data_uri_match.group(2)
            else:
                raise HTTPException(status_code=400, detail="Invalid base64 data format")
        elif not media_source.startswith("http://") and not media_source.startswith("https://"):
            # Assume it's raw base64 without data: prefix
            file_base64 = media_source
            if not content_type:
                content_type = "application/octet-stream"
        return file_base64, content_type

    @staticmethod
    def _download_media_as_base64(media_source: str, content_type: str | None) -> tuple[str, str | None, str | None]:
        """Download the file of a URL (HEIC/HEIF converted to JPEG) and base64-encode it; 500 when that fails.

        Returns (file_base64, downloaded_filename, content_type): a content type already known wins over the
        detected one, and a HEIC conversion overrides both.
        """
        try:
            file_content, downloaded_filename, detected_content_type = _download_media_file(media_source)
            if not content_type:
                content_type = detected_content_type

            # Convert HEIC/HEIF to JPEG — WAHA cannot render HEIC as an inline image
            file_content, downloaded_filename, converted_content_type = _convert_heic_to_jpeg(file_content, downloaded_filename)
            if converted_content_type:
                content_type = converted_content_type

            file_base64 = base64.b64encode(file_content).decode("utf-8")
        except Exception as error:
            logger.error(f"Failed to download file from {media_source}: {str(error)}")
            raise HTTPException(status_code=500, detail=f"Failed to download file: {str(error)}")
        return file_base64, downloaded_filename, content_type

    @staticmethod
    def _classify_media(content_type: str | None, media_source: str) -> tuple[str, str | None]:
        """WAHA file category (image / video / voice / document, default document) of the file: from its content
        type (exact match in CONTENT_TYPE_TO_WAHA_FILE_TYPE, then the type prefix) or, when there is none, from the
        URL extension, which also guesses the content type. Returns (waha_file_type, content_type).
        """
        waha_file_type = "document"  # default

        if content_type:
            # Exact match first
            bare_content_type = content_type.split(";")[0].strip().lower()
            if bare_content_type in CONTENT_TYPE_TO_WAHA_FILE_TYPE:
                waha_file_type = CONTENT_TYPE_TO_WAHA_FILE_TYPE[bare_content_type]
            elif bare_content_type.startswith("image/"):
                waha_file_type = "image"
            elif bare_content_type.startswith("video/"):
                waha_file_type = "video"
            elif bare_content_type.startswith("audio/") or "ogg" in bare_content_type:
                waha_file_type = "voice"
        else:
            # Fallback: detect from URL extension
            lowercase_source = media_source.lower()
            if any(extension in lowercase_source for extension in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                waha_file_type = "image"
                content_type = "image/jpeg"
            elif any(extension in lowercase_source for extension in [".mp4", ".avi", ".mov", ".mkv", ".webm"]):
                waha_file_type = "video"
                content_type = "video/mp4"
            elif any(extension in lowercase_source for extension in [".mp3", ".ogg", ".opus", ".wav", ".m4a", ".aac"]):
                waha_file_type = "voice"
                content_type = "audio/ogg; codecs=opus"
            elif any(extension in lowercase_source for extension in [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt"]):
                waha_file_type = "document"
                if ".pdf" in lowercase_source:
                    content_type = "application/pdf"
        return waha_file_type, content_type

    def _post_media_file(self, send_endpoint_path: str, waha_file_type: str, file_payload: dict) -> dict:
        """POST the file payload to its WAHA send endpoint; returns {"sid", "fake_sid"}.

        A non-2xx answer is raised with WAHA's status code; a network or JSON-decoding error becomes a 500.
        """
        send_url = self._build_waha_url(send_endpoint_path)
        try:
            response = requests.post(send_url, json=file_payload, headers=self._build_api_headers())

            if response.status_code not in [200, 201]:
                logger.error(f"Failed to send {waha_file_type} via {send_endpoint_path}: status={response.status_code}")
                raise HTTPException(status_code=response.status_code, detail=response.text)

            send_result = response.json()
            message_sid = self._extract_message_id_from_response(send_result)
            serialized_sid = send_result.get("id", {}).get("_serialized") if isinstance(send_result.get("id"), dict) else message_sid
            return {"sid": message_sid, "fake_sid": serialized_sid or message_sid}
        except requests.exceptions.RequestException as error:
            logger.error(f"Failed to send {waha_file_type} via {send_endpoint_path}: {str(error)}")
            raise HTTPException(status_code=500, detail=f"Failed to send {waha_file_type}: {str(error)}")

    def deliver_file_to_customer(self, recipient_address: str, message_text: str, media_url: str, **send_options) -> dict:
        """Send a file via WhatsApp using URL or base64
        Automatically detects file type and uses appropriate WAHA endpoint:
        - /api/sendImage for images (jpg, jpeg, png, gif, webp)
        - /api/sendVideo for videos (mp4, avi, mov, mkv)
        - /api/sendVoice for audio/voice (mp3, ogg, opus, wav, m4a, aac)
        - /api/sendFile for documents (pdf, doc, docx, xls, xlsx, etc.)

        Supports both URL and base64 formats.
        """
        chat_id = self._normalize_chat_id(recipient_address)

        # Detect if it's base64 data
        file_base64, content_type = self._decode_inline_media(media_url, send_options.get("mimetype"))

        # Download file and convert to base64 if it's a URL
        downloaded_filename = None  # filename obtained from the download
        if file_base64 is None:
            file_base64, downloaded_filename, content_type = self._download_media_as_base64(media_url, content_type)

        # ---------- Detect file type from content_type or URL extension ----------
        waha_file_type, content_type = self._classify_media(content_type, media_url)

        # Select appropriate endpoint based on file type
        send_endpoint_path = WAHA_SEND_ENDPOINTS.get(waha_file_type, "/api/sendFile")

        # Resolve a filename for the file (required by WAHA for documents)
        send_filename = self._derive_filename(media_url, content_type, downloaded_filename)

        # Build payload
        file_payload = {
            "chatId": chat_id,
            "file": {"mimetype": content_type, "filename": send_filename, "data": file_base64},
            "session": self.phone_number,
        }

        # Add caption/message if provided
        if message_text:
            file_payload["caption"] = message_text

        # Add convert flag for voice and video
        if waha_file_type in ["voice", "video"]:
            file_payload["convert"] = send_options.get("convert", False)

        return self._post_media_file(send_endpoint_path, waha_file_type, file_payload)

    def fetch_phone_number_for_lid(self, raw_lid: str) -> dict:
        """Get phone number by LID"""
        bare_lid = raw_lid
        if "@" in raw_lid:
            bare_lid = raw_lid.split("@")[0]

        lid_lookup_url = self._build_waha_url(f"/api/{self.phone_number}/lids/{bare_lid}")
        response = requests.get(lid_lookup_url, headers=self._build_api_headers())

        if response.status_code != 200:
            return {}

        return response.json()

    def fetch_chat_overview(self, phone_or_chat_id: str) -> dict:
        """Get chat information including archive status"""
        chat_id = phone_or_chat_id
        if "@" not in phone_or_chat_id:
            chat_id = f"{phone_or_chat_id}@c.us"

        # WAHA doesn't have direct chat info endpoint
        # Use chats/overview with ids filter instead
        overview_url = self._build_waha_url(f"/api/{self.phone_number}/chats/overview")
        overview_params = {"ids": chat_id}
        response = requests.get(overview_url, params=overview_params, headers=self._build_api_headers())

        if response.status_code != 200:
            return {}

        try:
            chat_overviews = response.json()
            # Return first chat if found, otherwise empty dict
            return chat_overviews[0] if chat_overviews and len(chat_overviews) > 0 else {}
        except Exception as error:
            logger.error(f"Error getting chat info: {error}")
            return {}
