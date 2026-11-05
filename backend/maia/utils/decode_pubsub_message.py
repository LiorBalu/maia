"""Decodes the base64 Pub/Sub envelope Google sends for Gmail push notifications."""

import base64
import json


def extract_pubsub_push_payload(push_envelope):
    # Decode the Base64 payload
    """The JSON payload (emailAddress, historyId) of a Pub/Sub push envelope."""
    payload_text = base64.b64decode(push_envelope["message"]["data"]).decode("utf-8")
    # Convert to JSON if that is the expected format
    if isinstance(payload_text, bytes):
        payload_text = payload_text.decode("utf-8")
    if isinstance(payload_text, str):
        return json.loads(payload_text)
    return json.loads(payload_text)
