"""Sends the bot's replies and the tenant's notification emails through its connected Gmail account (HTML, RTL for
Hebrew, threads).
"""

import logging
import base64
import os
import re
from typing import Optional
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from maia.models.google_accounts import GoogleShared
from maia.api.email import build_gmail_client

logger = logging.getLogger(__name__)


def has_hebrew_characters(message_text: str) -> bool:
    """Check if text contains Hebrew characters"""
    hebrew_letter_pattern = re.compile(r"[\u0590-\u05FF]")
    return bool(hebrew_letter_pattern.search(message_text))


def render_directional_html(message_text: str, text_direction: str = "auto") -> str:
    """Wrap text in HTML with proper direction support.
    Always wraps in HTML to ensure email clients respect text direction.

    Args:
        message_text: The message text
        text_direction: 'rtl', 'ltr', or 'auto'
    """
    horizontal_alignment = "right" if text_direction == "rtl" else ("left" if text_direction == "ltr" else "start")
    font_family = "Arial, Helvetica, sans-serif"
    html_document = f"""<!DOCTYPE html>
<html dir="{text_direction}" lang="{"he" if text_direction == "rtl" else "en"}">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
    <style>
        body {{
            direction: {text_direction};
            text-align: {horizontal_alignment};
            unicode-bidi: embed;
            font-family: {font_family};
        }}
        p {{
            direction: {text_direction};
            text-align: {horizontal_alignment};
            unicode-bidi: embed;
        }}
    </style>
</head>
<body dir="{text_direction}">
    <div dir="{text_direction}" style="direction: {text_direction}; text-align: {horizontal_alignment}; unicode-bidi: embed;">
        {message_text.replace(chr(10), "<br>")}
    </div>
</body>
</html>"""
    return html_document


def _find_tenant_google_account(tenant_name: str) -> Optional[GoogleShared]:
    """The tenant's default connected Google account, else any of its accounts, else None."""
    return (
        GoogleShared.objects(tenant_name=tenant_name, default=True).first()
        or GoogleShared.objects(tenant_name=tenant_name).first()
    )


def _send_raw_gmail_message(
    gmail_client, mime_message: MIMEText | MIMEMultipart, gmail_thread_id: Optional[str] = None
) -> dict:
    """Encode the message and send it through the Gmail API (inside the Gmail thread when gmail_thread_id is given);
    returns the Gmail response unchanged.
    """
    raw_message = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("utf-8")
    gmail_send_body = {"raw": raw_message}
    if gmail_thread_id:
        gmail_send_body["threadId"] = gmail_thread_id
    return gmail_client.users().messages().send(userId="me", body=gmail_send_body).execute()


def send_email_from_tenant_gmail(tenant_name: str, recipient_email: str, subject_line: str, message_html: str) -> Optional[dict]:
    """Send an HTML email from the tenant's connected Gmail account (the default one, else any). Returns the Gmail
    response, or None when the tenant has no connected account (the notification is then only logged).
    """
    google_account_doc = _find_tenant_google_account(tenant_name)
    if google_account_doc is None:
        logger.info("No Google account connected to %s: email to %s (%s) not sent", tenant_name, recipient_email, subject_line)
        return None
    gmail_client = build_gmail_client(google_account_doc.access_token, google_account_doc.refresh_token)
    mime_message = MIMEText(message_html, "html", "utf-8")
    mime_message["To"] = recipient_email
    mime_message["Subject"] = subject_line
    return _send_raw_gmail_message(gmail_client, mime_message)


def _render_html_with_detected_direction(message_text: str) -> str:
    """The message as an HTML document: right-to-left when it contains Hebrew characters, else left-to-right."""
    text_direction = "rtl" if has_hebrew_characters(message_text) else "ltr"
    return render_directional_html(message_text, text_direction=text_direction)


def _build_attachment_part(attachment_path: str) -> MIMEApplication:
    """The file as an attachment part, named after its basename."""
    with open(attachment_path, "rb") as attachment_file:
        attachment_part = MIMEApplication(attachment_file.read(), Name=os.path.basename(attachment_path))

    attachment_part["Content-Disposition"] = f'attachment; filename="{os.path.basename(attachment_path)}"'
    return attachment_part


def _compose_mime_message(message_html: str, attachment_paths: list[str]) -> MIMEText | MIMEMultipart:
    """The email without its headers: multipart (the HTML part plus every attachment path that exists on disk) when
    attachment paths are given, else a plain HTML message.
    """
    if attachment_paths:
        mime_message = MIMEMultipart()
        mime_message.attach(MIMEText(message_html, "html", "utf-8"))
        for attachment_path in attachment_paths:
            if os.path.exists(attachment_path):
                mime_message.attach(_build_attachment_part(attachment_path))

    else:
        mime_message = MIMEText(message_html, "html", "utf-8")
    return mime_message


def _set_reply_headers(mime_message: MIMEText | MIMEMultipart, original_message_id: str) -> None:
    """Set In-Reply-To and References to the id of the message being answered."""
    # Ensure message_id is wrapped in angle brackets for email headers
    bracketed_message_id = original_message_id if original_message_id.startswith("<") else f"<{original_message_id}>"
    mime_message["In-Reply-To"] = bracketed_message_id
    mime_message["References"] = bracketed_message_id


class EmailPlatformService:
    """Sender of the email channel: sends through the tenant's connected Gmail account. It implements the same
    deliver_message_to_customer interface the pipeline calls for every platform.
    """
    def __init__(self, tenant_name: str, **kwargs) -> None:
        """Load the tenant's Google account and build the Gmail client."""
        logger.info(" ".join(str(fragment) for fragment in ("EmailPlatformService", tenant_name)))
        google_account_doc = GoogleShared.objects(tenant_name=tenant_name).first()
        self.email = google_account_doc.email
        self._access_token = google_account_doc.access_token
        self._refresh_token = google_account_doc.refresh_token
        self._service = build_gmail_client(self._access_token, self._refresh_token)

    def deliver_message_to_customer(self, recipient_address: str, message_text: str, **send_options) -> dict:
        """Send the reply as an HTML email (RTL when it contains Hebrew), inside the Gmail thread when thread_id /
        message_id are given; returns {sid, thread_id}.
        """
        gmail_thread_id = send_options.get("thread_id")
        original_message_id = send_options.get("message_id")
        subject_line = send_options.get("subject", "")
        attachment_paths = send_options.get("attachments", [])

        # Add "Re:" prefix to subject when replying in a thread
        if gmail_thread_id and subject_line and not subject_line.lower().startswith("re:"):
            subject_line = f"Re: {subject_line}"

        # Always send as HTML to ensure proper text direction
        message_html = _render_html_with_detected_direction(message_text)
        mime_message = _compose_mime_message(message_html, attachment_paths)

        mime_message["To"] = recipient_address
        mime_message["Subject"] = subject_line
        if gmail_thread_id and original_message_id:
            _set_reply_headers(mime_message, original_message_id)
        gmail_response = _send_raw_gmail_message(self._service, mime_message, gmail_thread_id)

        return {
            "sid": gmail_response["id"],
            "thread_id": gmail_response.get("threadId"),
        }

    def deliver_file_to_customer(self, recipient_address: str, message_text: str, media_url: str, **send_options) -> dict:
        """Send an email that references a media URL (kept for the sender interface)."""
        # Always send as HTML to ensure proper text direction
        message_html = _render_html_with_detected_direction(message_text)

        mime_message = MIMEText(message_html, "html", "utf-8")
        mime_message["To"] = recipient_address
        mime_message["media_url"] = media_url
        gmail_response = _send_raw_gmail_message(self._service, mime_message)

        return {
            "sid": gmail_response["id"],
            "thread_id": gmail_response.get("threadId"),
        }
