"""Shared vocabulary of the message pipeline.

Two things live here, and every stage of the pipeline imports them:

- ``MessageContext`` — everything the pipeline knows about one inbound message. It is built once
  per message (``maia/inbound/reply.py::build_message_context`` for chat platforms,
  ``maia/voice/normalize_call.py::normalize_call_data`` for the Twilio Voice webhook,
  ``maia/voice/media_stream.py::build_message_context`` for the Realtime call session) and then
  mutated in place by the business handler (``tenants/<folder>/handling.py``) and by the tools.
- The flow-control exceptions below. They are raised on purpose to jump to the next phase of the
  pipeline; none of them is an error.
"""

from typing import Optional, TypedDict

from maia.models.customers import CustomerDocument
from maia.models.conversations import Conversation
from maia.models.tenants import TenantDocument
from maia.models.settings import ChatSettingsDocument


class ReplyReady(Exception):
    """The reply text is ready: raised with the answer, caught by the send phase.

    ``raise ReplyReady(answer)`` is the SUCCESS path of ``reply_to_message``; the whole
    send phase lives in ``except ReplyReady``. Tools and business handlers may raise it too, to
    send a fixed text instead of a generated one. An empty answer means "send nothing".
    """


class TenantNotFound(Exception):
    """No tenant with that name exists."""


class TwimlReady(Exception):
    """Phone webhook: raised with the TwiML ``VoiceResponse`` that connects the call to the media stream."""


class MessageContext(TypedDict):
    """The context of one inbound message, passed through the whole pipeline as a plain dict.

    Identity:
    - ``tenant_name`` / ``tenant``: the business (tenancy key and its document).
    - ``platform``: ``waha`` (WhatsApp) | ``web`` | ``email`` | ``telephone``.
    - ``customer``: the end user writing to the business; ``conversation``: the open conversation.
    - ``chat_settings``: the tenant's chat settings (delay, features).

    The message itself:
    - ``sender_id``: phone number or email of the sender (the customer's key in this tenant).
    - ``recipient_id``: the business number / address the message was sent to.
    - ``sender_name``: the sender's display name.
    - ``message_text``, ``message_id`` (the platform's id: WAHA message id, Gmail id, uuid for calls).
    - ``message_type`` (text / image / audio...), ``media_url``, ``media_content_type``.

    Per-message state:
    - ``meta_data``: per-message flags (``request_message``, ``thread_id``,
      ``subject``, ``call_sid``, ``should_reply_after_process``...).

    Tools (filled by the business handler, consumed by the tool adapters):
    - ``tool_names``: names of the ``@gpt_tool`` functions bound to the model for this message.
    - ``tool_schemas``: ready-made tool schemas for the Realtime session (phone).
    - ``tool_results``: ``tool_call_id`` → result, so the tool messages can be saved.
    """

    tenant_name: str
    platform: str
    customer: CustomerDocument
    tenant: TenantDocument
    conversation: Conversation
    chat_settings: Optional[ChatSettingsDocument]
    sender_id: str
    recipient_id: Optional[str]
    sender_name: str
    message_text: str
    message_id: str
    message_type: Optional[str]
    media_url: Optional[str]
    media_content_type: Optional[str]
    meta_data: dict
    tool_names: list
    tool_schemas: list
    tool_results: dict
