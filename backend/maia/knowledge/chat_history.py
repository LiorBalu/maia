"""Redis-backed chat history of a conversation (the LangChain session the RAG chain reads and writes)."""


from typing import Any, Dict, List, Optional

from maia.knowledge.conversational_rag import ConversationalRAG


def add_message_to_history(
    tenant_name: str, session_id: str, text: str, media: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Simple function to add a message to the conversation history for a session.

    Args:
        tenant_name: Name of the tenant
        session_id: Unique session identifier for conversation
        text: User's message text
        media: Optional list of media (images only) to include in LangChain format:
               For images: {"type": "image", "base64": "...", "mime_type": "image/jpeg"}
               Note: Audio is NOT stored here — it is transcribed to text upstream
               by handle_audio_message() and the transcription becomes the text param.

    Returns:
        Dict with status
    """
    try:
        rag = ConversationalRAG(tenant_name)
        rag.add_message_to_history(session_id, text, media=media)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": "Could not add message to history", "error": str(e)}


def add_ai_message_to_history(tenant_name: str, session_id: str, text: str) -> Dict[str, Any]:
    """
    Add an AI/assistant message to the Redis conversation history for a session.
    Use this for manager-sent messages so the LLM sees them as assistant turns.

    Args:
        tenant_name: Name of the tenant
        session_id: Unique session identifier (str(Conversation.id) for the online conv)
        text: The message text (e.g. "[Manager]: hello" for manager dashboard messages)

    Returns:
        Dict with status
    """
    try:
        from langchain_core.messages import AIMessage

        rag = ConversationalRAG(tenant_name)
        history = rag._get_session_history(session_id)
        history.add_message(AIMessage(content=text))
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": "Could not add AI message to history", "error": str(e)}
