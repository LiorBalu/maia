"""Sends support / notification emails from the platform mailbox."""

import logging
import base64
import requests
import mimetypes
from datetime import datetime, timedelta
from urllib.parse import urlparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Optional
from maia.models.google_accounts import GoogleShared
from maia.api.email import get_email_service

logger = logging.getLogger(__name__)


def send_support_email(
    html_message_body: str,
    subject: str,
    to: str,
    media_urls: Optional[List[str]] = None,
):
    google_shared = GoogleShared.objects(tenant_name="support", default=True).first()

    # Use existing scopes if available, otherwise use default Gmail scopes
    scopes = google_shared.scopes if google_shared.scopes else None

    service = get_email_service(
        access_token=google_shared.access_token,
        refresh_token=google_shared.refresh_token,
        scopes=scopes,
    )

    # Update tokens if they were refreshed
    credentials = service._http.credentials
    if credentials.token != google_shared.access_token:
        google_shared.access_token = credentials.token
        if credentials.refresh_token:
            google_shared.refresh_token = credentials.refresh_token

        # Update expires_at (tokens expire in 1 hour by default)
        if credentials.expiry:
            google_shared.expires_at = credentials.expiry
        else:
            google_shared.expires_at = datetime.utcnow() + timedelta(hours=1)

        google_shared.save()
        logger.info(f"Tokens updated in database, expires at: {google_shared.expires_at}")

    # Create multipart message if there are attachments, otherwise use simple text message
    if media_urls:
        message = MIMEMultipart()
        message["To"] = to
        message["Subject"] = subject

        # Attach HTML body
        html_part = MIMEText(html_message_body, "html", "utf-8")
        message.attach(html_part)

        # Download and attach media files
        for media_url in media_urls:
            try:
                # Get the file from URL
                response = requests.get(media_url, timeout=30)
                response.raise_for_status()

                # Get filename from URL or use default
                parsed_url = urlparse(media_url)
                filename = parsed_url.path.split("/")[-1]
                if not filename or "." not in filename:
                    # Try to guess extension from content type
                    content_type = response.headers.get("content-type", "")
                    extension = mimetypes.guess_extension(content_type.split(";")[0])
                    filename = f"attachment{extension or '.bin'}"

                # Create attachment
                attachment = MIMEBase("application", "octet-stream")
                attachment.set_payload(response.content)
                encoders.encode_base64(attachment)

                # Add header
                attachment.add_header(
                    "Content-Disposition", f"attachment; filename= {filename}"
                )

                message.attach(attachment)
                logger.info(f"Successfully attached: {filename} from {media_url}")

            except Exception as e:
                logger.error(f"Failed to attach media from {media_url}: {str(e)}")
                continue
    else:
        # Simple text message without attachments
        message = MIMEText(html_message_body, "html", "utf-8")
        message["To"] = to
        message["Subject"] = subject

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    body = {"raw": raw_message}

    try:
        sent_message = service.users().messages().send(userId="me", body=body).execute()
        logger.info(f"Message Id: {sent_message['id']}")
        return sent_message
    except Exception as error:
        logger.error(f"An error occurred: {error}")
        return None
