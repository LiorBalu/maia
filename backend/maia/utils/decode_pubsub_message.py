"""Decodes the base64 Pub/Sub envelope Google sends for Gmail push notifications."""

import base64
import json


def decode_pubsub_message(pubsub_message):
    # Decode the Base64 payload
    decoded_data = base64.b64decode(pubsub_message["message"]["data"]).decode("utf-8")
    # Convert to JSON if that is the expected format
    if isinstance(decoded_data, bytes):
        decoded_data = decoded_data.decode("utf-8")
    if isinstance(decoded_data, str):
        return json.loads(decoded_data)
    return json.loads(decoded_data)
