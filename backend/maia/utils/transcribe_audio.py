"""Transcribe a voice note with OpenAI.

The language hint comes from the tenant's ``default_language`` ("he"/"en"); when it is missing
OpenAI auto-detects the language.
"""

TRANSCRIPTION_MODEL = "gpt-4o-transcribe"


def transcribe_audio_file(file_path: str, tenant_name: str) -> str:
    """Transcribe an audio file with OpenAI (gpt-4o-transcribe), with the tenant's language as a hint."""
    from maia.services.registry import chatbot_for
    from maia.models.tenants import TenantDocument

    tenant = TenantDocument.objects(name=tenant_name).only("default_language").first()
    language = tenant.default_language if tenant and tenant.default_language else None
    client = chatbot_for(tenant_name).client
    with open(file_path, "rb") as audio_file:
        kwargs = {"model": TRANSCRIPTION_MODEL, "file": audio_file}
        if language:
            kwargs["language"] = language
        result = client.audio.transcriptions.create(**kwargs)
    return (getattr(result, "text", None) or "").strip()
