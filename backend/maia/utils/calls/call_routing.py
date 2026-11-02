"""Build the media-stream WebSocket URL a Twilio call connects to (used by ``connect_call_to_stream``).

Calls are inbound only; the ``direction`` query param is informational (the stream handler does not read it).
"""

# tts_provider -> media-stream router path under /api.
_PROVIDER_STREAM_PATHS = {"realtime": "media-stream"}


def build_call_stream_url(
    host: str, tenant_name: str, from_number: str, called_number: str, tts_provider: str, direction: str = "inbound"
) -> str:
    """Return the ``wss://`` Stream URL for *tts_provider* on *host* (defaults to the Realtime stream)."""
    path = _PROVIDER_STREAM_PATHS.get(tts_provider, "media-stream")
    return f"wss://{host}/api/{path}/{tenant_name}/{from_number}/{called_number}?direction={direction}"
