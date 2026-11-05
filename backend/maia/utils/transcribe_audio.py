"""Transcribe a voice note with OpenAI.

The language hint comes from the tenant's ``default_language`` ("he"/"en"); when it is missing
OpenAI auto-detects the language.
"""

VOICE_NOTE_TRANSCRIPTION_MODEL = "gpt-4o-transcribe"


def _resolve_language_hint(tenant_name: str) -> str | None:
    """The tenant's default_language as the language hint, or None (OpenAI auto-detects) when the tenant or the field
    is missing.
    """
    from maia.models.tenants import TenantDocument

    tenant_doc = TenantDocument.objects(name=tenant_name).only("default_language").first()
    return tenant_doc.default_language if tenant_doc and tenant_doc.default_language else None


def transcribe_voice_note(audio_path: str, tenant_name: str) -> str:
    """Transcribe an audio file with OpenAI (gpt-4o-transcribe), with the tenant's language as a hint."""
    from maia.services.registry import build_tenant_chatbot

    language_hint = _resolve_language_hint(tenant_name)
    openai_client = build_tenant_chatbot(tenant_name).client
    with open(audio_path, "rb") as audio_stream:
        transcription_params = {"model": VOICE_NOTE_TRANSCRIPTION_MODEL, "file": audio_stream}
        if language_hint:
            transcription_params["language"] = language_hint
        transcription_response = openai_client.audio.transcriptions.create(**transcription_params)
    return (getattr(transcription_response, "text", None) or "").strip()
