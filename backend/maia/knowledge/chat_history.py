"""Redis-backed chat history of a conversation (the LangChain session the RAG chain reads and writes)."""


from typing import Any, Dict, List, Optional

from maia.knowledge.conversational_rag import ConversationalRAG


def record_customer_message_in_history(
    tenant_name: str, history_session_id: str, customer_message_text: str, media_items: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Simple function to add a message to the conversation history for a session.

    Args:
        tenant_name: Name of the tenant
        history_session_id: Unique session identifier for conversation
        customer_message_text: User's message text
        media_items: Optional list of media (images only) to include in LangChain format:
               For images: {"type": "image", "base64": "...", "mime_type": "image/jpeg"}
               Note: Audio is NOT stored here — it is transcribed to text upstream
               by download_and_transcribe_voice_note() and the transcription becomes the customer_message_text param.

    Returns:
        Dict with status
    """
    try:
        tenant_rag = ConversationalRAG(tenant_name)
        tenant_rag.record_customer_message_in_history(
            history_session_id, customer_message_text, media_items=media_items
        )
        return {"status": "success"}
    except Exception as error:
        return {"status": "error", "message": "Could not add message to history", "error": str(error)}


def record_assistant_message_in_history(tenant_name: str, history_session_id: str, assistant_message_text: str) -> Dict[str, Any]:
    """
    Add an AI/assistant message to the Redis conversation history for a session.
    Use this for manager-sent messages so the LLM sees them as assistant turns.

    Args:
        tenant_name: Name of the tenant
        history_session_id: Unique session identifier (str(Conversation.id) for the online conv)
        assistant_message_text: The message text (e.g. "[Manager]: hello" for manager dashboard messages)

    Returns:
        Dict with status
    """
    try:
        from langchain_core.messages import AIMessage

        tenant_rag = ConversationalRAG(tenant_name)
        conversation_history = tenant_rag._open_session_history(history_session_id)
        conversation_history.add_message(AIMessage(content=assistant_message_text))
        return {"status": "success"}
    except Exception as error:
        return {"status": "error", "message": "Could not add AI message to history", "error": str(error)}
