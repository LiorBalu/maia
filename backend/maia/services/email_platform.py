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
from maia.api.email import get_email_service

logger = logging.getLogger(__name__)


def contains_hebrew(text: str) -> bool:
    """Check if text contains Hebrew characters"""
    hebrew_pattern = re.compile(r"[\u0590-\u05FF]")
    return bool(hebrew_pattern.search(text))


def wrap_with_html(text: str, direction: str = "auto") -> str:
    """Wrap text in HTML with proper direction support.
    Always wraps in HTML to ensure email clients respect text direction.

    Args:
        text: The message text
        direction: 'rtl', 'ltr', or 'auto'
    """
    text_align = "right" if direction == "rtl" else ("left" if direction == "ltr" else "start")
    font = "Arial, Helvetica, sans-serif"
    html_content = f"""<!DOCTYPE html>
<html dir="{direction}" lang="{"he" if direction == "rtl" else "en"}">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
    <style>
        body {{
            direction: {direction};
            text-align: {text_align};
            unicode-bidi: embed;
            font-family: {font};
        }}
        p {{
            direction: {direction};
            text-align: {text_align};
            unicode-bidi: embed;
        }}
    </style>
</head>
<body dir="{direction}">
    <div dir="{direction}" style="direction: {direction}; text-align: {text_align}; unicode-bidi: embed;">
        {text.replace(chr(10), "<br>")}
    </div>
</body>
</html>"""
    return html_content


def send_tenant_email(tenant_name: str, to: str, subject: str, html_body: str) -> Optional[dict]:
    """Send an HTML email from the tenant's connected Gmail account (the default one, else any). Returns the Gmail
    response, or None when the tenant has no connected account (the notification is then only logged).
    """
    account = (
        GoogleShared.objects(tenant_name=tenant_name, default=True).first()
        or GoogleShared.objects(tenant_name=tenant_name).first()
    )
    if account is None:
        logger.info("No Google account connected to %s: email to %s (%s) not sent", tenant_name, to, subject)
        return None
    service = get_email_service(account.access_token, account.refresh_token)
    message = MIMEText(html_body, "html", "utf-8")
    message["To"] = to
    message["Subject"] = subject
    body = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")}
    return service.users().messages().send(userId="me", body=body).execute()


class EmailPlatformService:
    """Sender of the email channel: sends through the tenant's connected Gmail account. It implements the same
    send_whatsapp_message interface the pipeline calls for every platform.
    """
    def __init__(self, tenant_name: str, **kwargs) -> None:
        """Load the tenant's Google account and build the Gmail client."""
        logger.info(" ".join(str(part) for part in ("EmailPlatformService", tenant_name)))
        _platform = GoogleShared.objects(tenant_name=tenant_name).first()
        self.email = _platform.email
        self._access_token = _platform.access_token
        self._refresh_token = _platform.refresh_token
        self._service = get_email_service(self._access_token, self._refresh_token)

    def send_whatsapp_message(self, phone_number: str, message: str, **kwargs) -> dict:
        """Send the reply as an HTML email (RTL when it contains Hebrew), inside the Gmail thread when thread_id /
        message_id are given; returns {sid, thread_id}.
        """
        thread_id = kwargs.get("thread_id")
        message_id = kwargs.get("message_id")
        subject = kwargs.get("subject", "")
        attachments = kwargs.get("attachments", [])

        # Add "Re:" prefix to subject when replying in a thread
        if thread_id and subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Always send as HTML to ensure proper text direction
        direction = "rtl" if contains_hebrew(message) else "ltr"
        message_content = wrap_with_html(message, direction=direction)

        if attachments:
            email_msg = MIMEMultipart()
            email_msg.attach(MIMEText(message_content, "html", "utf-8"))
            for attachment_path in attachments:
                if os.path.exists(attachment_path):
                    with open(attachment_path, "rb") as f:
                        part = MIMEApplication(f.read(), Name=os.path.basename(attachment_path))

                    part["Content-Disposition"] = f'attachment; filename="{os.path.basename(attachment_path)}"'

                    email_msg.attach(part)

        else:
            email_msg = MIMEText(message_content, "html", "utf-8")

        email_msg["To"] = phone_number
        email_msg["Subject"] = subject
        if thread_id and message_id:
            # Ensure message_id is wrapped in angle brackets for email headers
            formatted_message_id = message_id if message_id.startswith("<") else f"<{message_id}>"
            email_msg["In-Reply-To"] = formatted_message_id
            email_msg["References"] = formatted_message_id
        raw_message = base64.urlsafe_b64encode(email_msg.as_bytes()).decode("utf-8")
        body = {"raw": raw_message}
        if thread_id:
            body["threadId"] = thread_id
        sent_message = self._service.users().messages().send(userId="me", body=body).execute()

        return {
            "sid": sent_message["id"],
            "thread_id": sent_message.get("threadId"),
        }

    def send_whatsapp_file(self, phone_number: str, message: str, media_url: str, **kwargs) -> dict:
        # Always send as HTML to ensure proper text direction
        """Send an email that references a media URL (kept for the sender interface)."""
        direction = "rtl" if contains_hebrew(message) else "ltr"
        message_content = wrap_with_html(message, direction=direction)

        email_msg = MIMEText(message_content, "html", "utf-8")
        email_msg["To"] = phone_number
        email_msg["media_url"] = media_url
        raw_message = base64.urlsafe_b64encode(email_msg.as_bytes()).decode("utf-8")
        body = {"raw": raw_message}
        sent_message = self._service.users().messages().send(userId="me", body=body).execute()

        return {
            "sid": sent_message["id"],
            "thread_id": sent_message.get("threadId"),
        }
