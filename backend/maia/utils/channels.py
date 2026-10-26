"""Channel / entity tags of a message (`meta_data.channel`, `meta_data.entity`): phone or WhatsApp, AI or human, inferred from `platform` + `role` when missing."""

# Channels
CHANNEL_PHONE_AI = "phone_ai"  # AI phone call (Twilio)
CHANNEL_PHONE_HUMAN = "phone_human"  # A human representative on the phone
CHANNEL_WHATSAPP_AI = "whatsapp_ai"  # AI answering on WhatsApp
CHANNEL_WHATSAPP_HUMAN = "whatsapp_human"  # Human rep writing on WhatsApp

# Entities
ENTITY_AI = "ai"
ENTITY_HUMAN = "human"

PHONE_PLATFORMS = {"telephone", "phone", "voice", "sip"}


def infer_channel(platform: str | None, role: str | None, meta_data: dict | None = None) -> dict:
    """Infer (channel, entity) for a message that has no explicit channel tag.

    Used as a fallback at write time and when serializing messages saved without a tag.
    Returns a dict with "channel" and "entity".
    """
    if meta_data and meta_data.get("channel"):
        return {"channel": meta_data["channel"], "entity": meta_data.get("entity", ENTITY_AI)}

    platform = (platform or "").lower()
    role = (role or "").lower()
    is_human = role == "manager"

    if platform in PHONE_PLATFORMS:
        channel = CHANNEL_PHONE_HUMAN if is_human else CHANNEL_PHONE_AI
        return {"channel": channel, "entity": ENTITY_HUMAN if is_human else ENTITY_AI}

    # WhatsApp and every other text platform share the "whatsapp" channel (the text conversation).
    channel = CHANNEL_WHATSAPP_HUMAN if is_human else CHANNEL_WHATSAPP_AI
    return {"channel": channel, "entity": ENTITY_HUMAN if is_human else ENTITY_AI}
