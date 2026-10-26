"""Twilio Voice helper: the REST client a tenant's Twilio account gives us, used only to fetch
call recordings after a phone call (``update_conversation_with_call_record`` in
``maia/utils/conversation/reset.py``). WhatsApp goes through WAHA, not Twilio."""

import logging
from typing import Optional

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from maia.models.tenants import TenantDocument

logger = logging.getLogger(__name__)


class TwilioSmsService:
    def __init__(self, tenant_name: str, tenant: TenantDocument, sender_number: Optional[str] = None):
        twilio_data = (sender_number and tenant.get_twilio_by_phone_number(sender_number)) or tenant.twilio_accounts[0]
        self.twilio_account_sid = twilio_data.account_sid
        self.twilio_auth_token = twilio_data.auth_token
        self.twilio_phone_number = twilio_data.phone_number
        self.client = Client(self.twilio_account_sid, self.twilio_auth_token)

    def get_record_by_sid(self, sid: str):
        """Return the first recording of a call (``call_sid``), or None."""
        try:
            records = self.client.recordings.list(call_sid=sid, limit=20)
            return records[0] if records else None
        except TwilioRestException as e:
            logger.error(f"Failed to fetch record by SID {sid}: {str(e)}")
            return None
