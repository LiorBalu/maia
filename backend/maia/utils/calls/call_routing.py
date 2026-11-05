"""Build the media-stream WebSocket URL a Twilio call connects to (used by ``connect_inbound_call_to_media_stream``).

Calls are inbound only; the ``direction`` query param is informational (the stream handler does not read it).
"""

# tts_provider -> media-stream router path under /api.
_MEDIA_STREAM_PATH_BY_TTS_PROVIDER = {"realtime": "media-stream"}


def build_media_stream_url(
    backend_host: str, tenant_name: str, from_number: str, called_number: str, tts_provider: str, call_direction: str = "inbound"
) -> str:
    """Return the ``wss://`` Stream URL for *tts_provider* on *backend_host* (defaults to the Realtime stream)."""
    media_stream_path = _MEDIA_STREAM_PATH_BY_TTS_PROVIDER.get(tts_provider, "media-stream")
    return f"wss://{backend_host}/api/{media_stream_path}/{tenant_name}/{from_number}/{called_number}?direction={call_direction}"
