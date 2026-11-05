"""The ConversationalRAG chain: provider/model from the tenant's agent, prompt assembly, retrieval, tool binding and the answer the pipeline sends."""


import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pytz
from langchain_anthropic import ChatAnthropic
from langchain_classic.chains.combine_documents.stuff import create_stuff_documents_chain
# LangChain 1.0 imports - functions moved to langchain_classic
from langchain_classic.chains.history_aware_retriever import create_history_aware_retriever
from langchain_classic.chains.retrieval import create_retrieval_chain
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from maia.pipeline.context import MessageContext
from maia.services.llm_keys import resolve_anthropic_api_key_for_tenant, resolve_google_api_key_for_tenant, resolve_openai_api_key_for_tenant
from maia.models.prompts import PromptDocument
from maia.models.tenants import TenantDocument
from maia.utils.get_time_of_day import classify_time_of_day
from maia.knowledge.search import CombinedRetriever, MultiDocumentRetriever, QARetriever

if TYPE_CHECKING:
    from langchain_classic.agents import AgentExecutor

logger = logging.getLogger(__name__)


# What OpenAI's /v1/chat/completions accepts from each model family (checked live against the
# catalog on 2026-10-28):
#   - gpt-4.1 / gpt-4o: temperature works, there is no reasoning parameter.
#   - gpt-5.x / gpt-6.x: any temperature other than the default is rejected (langchain-openai drops
#     it only for "gpt-5*", so we never send it ourselves for these families).
#   - gpt-5.6-* and gpt-6-* / gpt-6.1-*: function tools work only with reasoning_effort="none"
#     (fastest answers, no hidden reasoning).
#   - gpt-6-astra and gpt-6.1-sol accept neither "none" nor function tools on chat completions,
#     so they go through the Responses API with the lowest effort they offer.
OPENAI_MODELS_REQUIRING_RESPONSES_API = ("gpt-6-astra", "gpt-6.1-sol")
OPENAI_MODEL_PREFIXES_WITH_REASONING_DISABLED = ("gpt-5.6", "gpt-6")
OPENAI_MODEL_PREFIXES_REJECTING_TEMPERATURE = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def build_openai_model_arguments(openai_model_name: str, requested_temperature: float) -> Dict[str, Any]:
    """Extra ChatOpenAI arguments for an OpenAI model: temperature only for the gpt-4 generation,
    reasoning_effort="none" for gpt-5.6 / gpt-6 on chat completions, and the Responses API with
    effort "low" for the two models that offer neither (see the table above).
    """
    normalized_model_name = str(openai_model_name)
    if normalized_model_name.startswith(OPENAI_MODELS_REQUIRING_RESPONSES_API):
        return {"use_responses_api": True, "reasoning": {"effort": "low"}}
    if normalized_model_name.startswith(OPENAI_MODEL_PREFIXES_WITH_REASONING_DISABLED):
        return {"reasoning_effort": "none"}
    if normalized_model_name.startswith(OPENAI_MODEL_PREFIXES_REJECTING_TEMPERATURE):
        return {}
    return {"temperature": requested_temperature}


def extract_answer_text(answer_content: Any) -> str:
    """The text of a model answer: a plain string, or the joined text blocks the Responses API returns."""
    if isinstance(answer_content, str):
        return answer_content
    if isinstance(answer_content, list):
        text_parts = []
        for content_block in answer_content:
            if isinstance(content_block, str):
                text_parts.append(content_block)
            elif isinstance(content_block, dict) and content_block.get("type") in ("text", "output_text"):
                text_parts.append(content_block.get("text", ""))
        return "".join(text_parts)
    return "" if answer_content is None else str(answer_content)


def _current_time_for_prompt() -> tuple[datetime, str]:
    """Now in Asia/Jerusalem, and its text for the prompt's date ("%a, %d %b %Y, %H:%M")."""
    jerusalem_timezone = pytz.timezone("Asia/Jerusalem")
    jerusalem_now = datetime.now(jerusalem_timezone)
    return jerusalem_now, jerusalem_now.strftime("%a, %d %b %Y, %H:%M")


def _strip_media_from_chat_history(chain_inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Extract text-only content from chat history, removing media."""
    chat_history = chain_inputs.get("chat_history", [])
    text_only_history = []

    for history_message in chat_history:
        if isinstance(history_message, (HumanMessage, AIMessage, SystemMessage)):
            # Extract text content
            if isinstance(history_message.content, str):
                # Simple text message - keep as is
                text_only_history.append(history_message)
            elif isinstance(history_message.content, list):
                # Multimodal message - extract only text parts
                text_parts = []
                for content_block in history_message.content:
                    if isinstance(content_block, dict) and content_block.get("type") == "text":
                        text_parts.append(content_block.get("text", ""))
                    elif isinstance(content_block, str):
                        text_parts.append(content_block)

                if text_parts:
                    # Create new message with only text
                    text_content = " ".join(text_parts)
                    if isinstance(history_message, HumanMessage):
                        text_only_history.append(HumanMessage(content=text_content))
                    elif isinstance(history_message, AIMessage):
                        text_only_history.append(AIMessage(content=text_content))
                    elif isinstance(history_message, SystemMessage):
                        text_only_history.append(SystemMessage(content=text_content))
        else:
            # Keep other message types as is
            text_only_history.append(history_message)

    # Return inputs with filtered history
    return {**chain_inputs, "chat_history": text_only_history}


class ConversationalRAG:
    """
    Conversational RAG system using LangChain's conversation chains with Redis for session management.
    Supports searching in documents, Q&A, or both.
    Uses Cohere embeddings for vector search.
    """

    def _configure_llm(self, tenant_doc: TenantDocument) -> None:
        """Set self.llm to the chat model of the agent's provider (OpenAI / Anthropic / Google) and raise ValueError
        when that provider's API key is missing; an unknown provider leaves self.llm unset.
        """
        llm_provider = self.agent_config.get("provider", "openai")

        if llm_provider == "openai":
            if not self.openai_api_key:
                raise ValueError(f"No OpenAI API key found for tenant {self.tenant_name}")
            openai_model_name = self.agent_config.get("model", "gpt-6.1-terra")
            self.llm = ChatOpenAI(
                openai_api_key=self.openai_api_key,
                model=openai_model_name,
                **build_openai_model_arguments(openai_model_name, self.agent_config.get("temperature", 0)),
            )
        elif llm_provider == "anthropic":
            provider_api_key = resolve_anthropic_api_key_for_tenant(tenant_doc)
            if not provider_api_key:
                raise ValueError(f"No Anthropic API key found for tenant {self.tenant_name}")
            self.llm = ChatAnthropic(
                anthropic_api_key=provider_api_key,
                model=self.agent_config.get("model", "claude-2"),
                temperature=self.agent_config.get("temperature", 0),
            )
        elif llm_provider == "google_llm":
            provider_api_key = resolve_google_api_key_for_tenant(tenant_doc)
            if not provider_api_key:
                raise ValueError(f"No Google LLM API key found for tenant {self.tenant_name}")
            self.llm = ChatGoogleGenerativeAI(
                google_api_key=provider_api_key,
                model=self.agent_config.get("model", "gemini-2.0-flash"),
                temperature=self.agent_config.get("temperature", 0),
            )

    def __init__(
        self,
        tenant_name: str,
        agent_config: Optional[dict] = None,
        include_qa: bool = True,
        include_documents: bool = True,
        qa_metadata_filter: Optional[Dict[str, Any]] = None,
        decorated_function_tools: Optional[List[str]] = None,
        request: Optional[MessageContext] = None,
    ):
        """Build the chain for one message: the LLM of the tenant's agent (OpenAI / Anthropic / Google), the bound
        tools, the document + Q&A retriever and the history-aware conversation chain.
        """
        self.tenant_name = tenant_name
        self.agent_config = agent_config or {}
        self.include_qa = include_qa
        self.include_documents = include_documents
        self.qa_metadata_filter = qa_metadata_filter
        self.decorated_function_tools = decorated_function_tools or []
        self.request: Optional[MessageContext] = request

        # Get the tenant to retrieve API keys
        tenant_doc = TenantDocument.objects(name=tenant_name).first()
        if not tenant_doc:
            raise ValueError(f"Tenant {tenant_name} not found")

        self.openai_api_key = resolve_openai_api_key_for_tenant(tenant_doc)

        # Setup LLM based on provider
        self._configure_llm(tenant_doc)

        # 🔧 Bind tools to LLM if provided
        self._bind_tools_to_llm()

        # Setup retriever (uses Cohere embeddings internally)
        self._assemble_combined_retriever()

        # Setup conversation chain
        self._assemble_rag_chain()

    @staticmethod
    def _parse_float_or_fallback(raw_value: Any, fallback: float) -> float:
        """Safely convert a value to float."""
        try:
            if raw_value is None:
                return fallback
            if isinstance(raw_value, str) and raw_value.strip().lower() in ("none", "null", ""):
                return fallback
            return float(raw_value)
        except Exception:
            return fallback

    @staticmethod
    def _parse_int_or_fallback(raw_value: Any, fallback: int) -> int:
        """int(raw_value), or the fallback for None / empty / invalid values."""
        try:
            if raw_value is None:
                return fallback
            if isinstance(raw_value, str) and raw_value.strip().lower() in ("none", "null", ""):
                return fallback
            return int(raw_value)
        except Exception:
            return fallback

    @staticmethod
    def _first_image_data_url(media_items: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """The data URL of the first image among the media items (type "image" with a base64 payload), or None."""
        if media_items and isinstance(media_items, list):
            for media_item in media_items:
                if isinstance(media_item, dict) and media_item.get("type") == "image" and media_item.get("base64"):
                    return f"data:{media_item.get('mime_type', 'image/jpeg')};base64,{media_item['base64']}"
        return None

    @staticmethod
    def _summarize_sources(retrieved_chunks: List[Document]) -> List[Dict[str, Any]]:
        """The source entries the dashboard stores, one per retrieved chunk: the Q&A pair (qa_id, question, answer)
        or the document part (title, document_id, part_number), with its scores and content preview.
        """
        source_summaries: List[Dict[str, Any]] = []
        for retrieved_chunk in retrieved_chunks:
            chunk_metadata = getattr(retrieved_chunk, "metadata", None) or {}
            chunk_source_type = chunk_metadata.get("source_type", "document")
            content_preview = retrieved_chunk.page_content or ""

            if chunk_source_type == "qa":
                source_summaries.append(
                    {
                        "source_type": "qa",
                        "qa_id": chunk_metadata.get("qa_id"),
                        "question": chunk_metadata.get("question"),
                        "answer": chunk_metadata.get("qa_answer"),
                        # Use final_score (rerank) as main score — this matches the threshold filter
                        "score": chunk_metadata.get("final_score", chunk_metadata.get("vector_score")),
                        "vector_score": chunk_metadata.get("vector_score"),
                        "content_preview": content_preview,
                    }
                )
            else:
                source_summaries.append(
                    {
                        "source_type": "document",
                        "title": chunk_metadata.get("title", chunk_metadata.get("document_title", "Unknown")),
                        "document_id": chunk_metadata.get("document_id"),
                        "part_number": chunk_metadata.get("part_number"),
                        # Use final_score (rerank) as main score — this matches the threshold filter
                        "score": chunk_metadata.get(
                            "final_score", chunk_metadata.get("vector_score", chunk_metadata.get("score"))
                        ),
                        "vector_score": chunk_metadata.get("vector_score"),
                        "content_preview": content_preview,
                    }
                )
        return source_summaries

    @staticmethod
    def _build_human_message(customer_message_text: str, media_items: Optional[List[Dict[str, Any]]]) -> HumanMessage:
        """The customer's turn as a HumanMessage: a text block followed by the media items when there are any,
        else the plain text.
        """
        if media_items and isinstance(media_items, list) and len(media_items) > 0:
            # Build multimodal message for history
            multimodal_content_blocks = [{"type": "text", "text": customer_message_text}]
            multimodal_content_blocks.extend(media_items)
            return HumanMessage(content=multimodal_content_blocks)
        return HumanMessage(content=customer_message_text)

    @staticmethod
    def _build_answer_payload(
        answer_text: str,
        standalone_question: str,
        retrieved_chunks: List[Document],
        source_summaries: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """The answer dict the pipeline reads: the answer, the reformulated question, the sources and their counts."""
        return {
            "answer": answer_text,
            "reformulated_question": standalone_question,
            "sources": source_summaries,
            "documents_found": len(retrieved_chunks),
            "sources_found": len(source_summaries),
            "documents_sources_count": sum(1 for summary in source_summaries if summary.get("source_type") == "document"),
            "qa_sources_count": sum(1 for summary in source_summaries if summary.get("source_type") == "qa"),
        }

    def _bind_tools_to_llm(self):
        """Setup and bind tools to the LLM."""
        from maia.pipeline.tools.langchain_adapter import build_langchain_tools

        # Convert decorated tools and request tools to LangChain format
        langchain_tools = build_langchain_tools(
            tool_names=(self.decorated_function_tools if self.decorated_function_tools else None),
            message_context=self.request,
        )

        # Save tools for later use in Agent
        self.tools = langchain_tools

        if langchain_tools:
            # Bind tools to the LLM
            self.llm = self.llm.bind_tools(langchain_tools)
            logger.info(f"Bound {len(langchain_tools)} tools to LLM for tenant '{self.tenant_name}'")
        else:
            logger.info(f"ℹNo tools to bind for tenant '{self.tenant_name}'")

    def _build_document_retriever(self, document_result_limit: int) -> MultiDocumentRetriever:
        """The retriever over the tenant's knowledge documents (the creation is logged)."""
        document_retriever = MultiDocumentRetriever(
            tenant_name=self.tenant_name,
            top_k=document_result_limit,
        )
        logger.info(f"Added Cohere document retriever for tenant '{self.tenant_name}'")
        return document_retriever

    def _build_qa_retriever(self, qa_result_limit: int) -> Optional[QARetriever]:
        """The retriever over the tenant's Q&A bank (the creation is logged), or None when it cannot be set up."""
        try:
            qa_retriever = QARetriever(
                tenant_name=self.tenant_name,
                top_k=qa_result_limit,
                metadata_filter=self.qa_metadata_filter,
            )
            logger.info(f"Added Cohere Q&A retriever for tenant '{self.tenant_name}'")
            return qa_retriever
        except Exception as error:
            logger.error(f"Could not setup Q&A retriever: {error}")
            return None

    def _assemble_combined_retriever(self):
        """Setup the document retriever using Cohere embeddings."""
        source_retrievers = []

        document_result_limit = max(0, self._parse_int_or_fallback(self.agent_config.get("top_k_general", 10), 10))
        qa_result_limit = max(0, self._parse_int_or_fallback(self.agent_config.get("top_k_qa", 5), 5))

        # Extract score_threshold from agent config (default 0 = no filtering)
        score_threshold = self._parse_float_or_fallback(self.agent_config.get("score_threshold", 0), 0)

        # Add document retriever if enabled
        if self.include_documents:
            source_retrievers.append(self._build_document_retriever(document_result_limit))

        # Add Q&A retriever if enabled
        if self.include_qa:
            qa_retriever = self._build_qa_retriever(qa_result_limit)
            if qa_retriever is not None:
                source_retrievers.append(qa_retriever)

        # Combine retrievers
        self.retriever = CombinedRetriever(
            retrievers=source_retrievers,
            documents_top_k=document_result_limit,
            qa_top_k=qa_result_limit,
            top_k=document_result_limit + qa_result_limit,
            score_threshold=score_threshold,
        )
        if score_threshold > 0:
            logger.info(f"Score threshold set to {score_threshold} for tenant '{self.tenant_name}'")

    def _load_reformulation_instructions(self) -> str:
        """The system prompt that turns the latest question into a standalone one: the tenant's
        ``contextualize_q_system_prompt`` document (braces escaped), or the default text below, saved as that
        document when it is missing.
        """
        # Contextualize question prompt
        reformulation_instructions = """Given a chat history and the latest user question which might reference context in the chat history,
        formulate a standalone question which can be understood without the chat history.

        CRITICAL: You MUST ONLY return a reformulated question. Do NOT provide an answer. Do NOT explain anything. ONLY return the question itself.

        If the question is already standalone, return it exactly as is without any changes or additions.
        DO NOT add explanations, answers, or any other text. ONLY the question."""

        # Try to load from PromptDocument if available
        reformulation_prompt_doc = PromptDocument.objects(
            tenant_name=self.tenant_name, name="contextualize_q_system_prompt"
        ).first()
        if reformulation_prompt_doc:
            reformulation_instructions = self._escape_template_braces(reformulation_prompt_doc.prompt_text)
        else:
            PromptDocument(
                tenant_name=self.tenant_name, name="contextualize_q_system_prompt", content=reformulation_instructions
            ).save()
        return reformulation_instructions

    def _build_reformulating_retriever(self, reformulation_instructions: str) -> Runnable:
        """The history-aware retriever: the chat history is stripped of media, the LLM turns the latest question
        into a standalone one with the given instructions, and the combined retriever searches with it.
        """
        reformulation_prompt = ChatPromptTemplate.from_messages(
            [("system", reformulation_instructions), MessagesPlaceholder("chat_history"), ("human", "{input}")]
        )

        # This filters out media (images, audio) during question reformulation
        # Wrap create_history_aware_retriever with text-only filter
        media_filter_step = RunnableLambda(_strip_media_from_chat_history)
        question_reformulating_retriever = create_history_aware_retriever(self.llm, self.retriever, reformulation_prompt)

        # Chain: filter text-only -> history-aware retriever
        return media_filter_step | question_reformulating_retriever

    async def _run_tool_calling_agent(self, agent_executor: "AgentExecutor", chain_inputs: Dict[str, Any]) -> str:
        """Run the tool-calling agent for one turn: the retrieved chunks become the {context} of the prompt and the
        current Jerusalem date its {current_date}; returns the text of the agent's answer.
        """
        # Retrieve the documents found by the previous step
        retrieved_chunks = chain_inputs.get("context", [])

        # Format docs to string
        if isinstance(retrieved_chunks, list):
            context_text = "\n\n".join([chunk.page_content for chunk in retrieved_chunks])
        else:
            context_text = str(retrieved_chunks)

        # Calculate current_date for prompt template
        _, formatted_date = _current_time_for_prompt()

        # Run the agent with documents injected in {context}
        agent_result = await agent_executor.ainvoke(
            {
                "input": chain_inputs["input"],
                "chat_history": chain_inputs.get("chat_history", []),
                "context": context_text,
                "current_date": formatted_date,
            }
        )
        return extract_answer_text(agent_result["output"])

    def _build_tool_agent_answer_chain(self, answer_instructions: str) -> RunnableLambda:
        """The answer step when the tenant has tools: a tool-calling agent on the answer prompt (plus the
        agent_scratchpad slot), run by an AgentExecutor behind an async entry point.
        """
        from langchain_classic.agents import AgentExecutor, create_tool_calling_agent

        # Prompt for Agent: Needs agent_scratchpad for tool outputs
        agent_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", answer_instructions),
                MessagesPlaceholder("chat_history"),
                ("human", "{input}"),
                MessagesPlaceholder("agent_scratchpad"),
            ]
        )

        # Create the Agent (LLM + Tools + Prompt)
        tool_calling_agent = create_tool_calling_agent(self.llm, self.tools, agent_prompt)

        # Create the Executor (The loop that runs tools)
        agent_executor = AgentExecutor(agent=tool_calling_agent, tools=self.tools, verbose=True)

        # Wrapper to bridge RAG Retrival Output -> Agent Input
        async def run_agent_entry_point(chain_inputs):
            """Bridge between the retrieval step and the tool-calling agent: format the retrieved documents as the
            {context} of the prompt and run the agent executor.
            """
            return await self._run_tool_calling_agent(agent_executor, chain_inputs)

        # This replaces create_stuff_documents_chain
        return RunnableLambda(run_agent_entry_point)

    def _build_stuff_answer_chain(self, answer_instructions: str) -> Runnable:
        """The answer step without tools: the answer prompt filled with the retrieved documents."""
        # ORIGINAL LOGIC if no tools
        answer_prompt = ChatPromptTemplate.from_messages(
            [("system", answer_instructions), MessagesPlaceholder("chat_history"), ("human", "{input}")]
        )

        # Create question-answer chain
        return create_stuff_documents_chain(self.llm, answer_prompt)

    def _assemble_rag_chain(self):
        """Setup the conversational retrieval chain."""
        reformulation_instructions = self._load_reformulation_instructions()

        # Create history-aware retriever with text-only filter for chat history
        self.history_aware_retriever = self._build_reformulating_retriever(reformulation_instructions)

        # QA system prompt

        answer_instructions = self._compose_answer_system_prompt()

        # Check if we have tools to setup an Agent Executor logic
        if hasattr(self, "tools") and self.tools:
            answer_chain = self._build_tool_agent_answer_chain(answer_instructions)
        else:
            answer_chain = self._build_stuff_answer_chain(answer_instructions)

        # Create the full RAG chain
        self.rag_chain = create_retrieval_chain(self.history_aware_retriever, answer_chain)

        # Wrap with message history
        self.conversational_rag_chain = RunnableWithMessageHistory(
            self.rag_chain,
            self._open_session_history,
            input_messages_key="input",
            history_messages_key="chat_history",
            output_messages_key="answer",
        )

    @staticmethod
    def _escape_template_braces(prompt_text: str) -> str:
        """
        Escape curly braces in dynamic prompt content loaded from DB so that
        LangChain's ChatPromptTemplate does not mistake JSON examples or any
        {word} patterns as template variables.

        We escape ALL braces here because this method is only called on the
        dynamic sections — the real template variables ({context}, {input},
        {chat_history}, {agent_scratchpad}, {current_date}) are added separately
        as literal strings AFTER this escaping step.
        """
        return prompt_text.replace("{", "{{").replace("}", "}}")

    def _ensure_default_prompt_doc(self) -> PromptDocument:
        """The tenant's ``default`` prompt document, created empty (saved and reloaded) when it is missing."""
        default_prompt_doc = PromptDocument.objects(tenant_name=self.tenant_name, name="default").first()
        if not default_prompt_doc:
            default_prompt_doc = PromptDocument(tenant_name=self.tenant_name, name="default", content="")
            default_prompt_doc.save()
            default_prompt_doc.reload()
        return default_prompt_doc

    def _load_platform_prompt_text(self) -> str:
        """The ``platform_<platform>`` prompt of the message's platform after a blank line, or '' when there is no
        message context or no such prompt document.
        """
        platform_instructions = ""
        if self.request:
            # fetch additional prompt for specific platform
            platform_prompt_doc = PromptDocument.objects(
                tenant_name=self.tenant_name, name=f"platform_{self.request['platform']}"
            ).first()
            if platform_prompt_doc:
                platform_instructions += f"\n\n{platform_prompt_doc.prompt_text}"
        return platform_instructions

    def _compose_answer_system_prompt(self):
        """Assemble the system prompt: date and time of day, the tenant's default prompt, the platform prompt
        (``platform_<platform>``, when one exists) and the {context} slot; braces in DB content are escaped.
        """
        default_prompt_doc = self._ensure_default_prompt_doc()
        # Prepare message for chatbot
        current_time, formatted_date = _current_time_for_prompt()
        platform_instructions = self._load_platform_prompt_text()

        # Escape curly braces in ALL dynamic content from the DB so that
        # LangChain's ChatPromptTemplate does not treat JSON like {"name": "..."} as
        # missing template variables (which causes a KeyError crash → silent fallback
        # to plain GPT with no RAG and no tools).
        escaped_default_prompt = self._escape_template_braces(default_prompt_doc.prompt_text)
        escaped_platform_instructions = self._escape_template_braces(platform_instructions)

        answer_system_prompt = (
            f"Today is {formatted_date}. We are now in {classify_time_of_day(current_time)}."
            + escaped_default_prompt
            + "\n\n"
            + escaped_platform_instructions
            + "\n\n"
            + """
        <rag_context>
        {context}
        </rag_context>
        """
        )

        return answer_system_prompt

    def _open_session_history(self, history_session_id: str) -> BaseChatMessageHistory:
        """Get chat message history for a session."""
        from maia.services.redis_clients import build_redis_chat_history_store

        return build_redis_chat_history_store(history_session_id=history_session_id, history_key_prefix=f"chat_history:{self.tenant_name}:")

    def _resolve_standalone_question(self, fallback_text: str) -> str:
        """The question as the history-aware retriever reformulated it (logged), or the fallback text when the
        retriever kept none.
        """
        # Retrieve the reformulated question from the retriever
        # The history_aware_retriever passes the reformulated question to the retriever
        if hasattr(self, "retriever") and hasattr(self.retriever, "last_reformulated_query"):
            if self.retriever.last_reformulated_query:
                reformulated_question = self.retriever.last_reformulated_query
                logger.info(f"✅ Reformulated question retrieved from retriever: '{reformulated_question[:100]}..."
                    if len(reformulated_question) > 100
                    else f"✅ Reformulated question: '{reformulated_question}'")
                return reformulated_question
        return fallback_text

    async def answer_customer_message(self, customer_message_text: str, history_session_id: str, media_items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Chat with documents maintaining conversation history.

        Args:
            customer_message_text: User's text message
            history_session_id: Unique session identifier
            media_items: Optional list of media items in LangChain format

        Returns:
            Dict with answer and source documents
        """
        try:
            # Get history for media injection
            session_history = self._open_session_history(history_session_id)

            # IMPORTANT: For retrieval, always use text only (Cohere can't embed HumanMessage)
            # The media will be injected later in the QA prompt
            # The question reformulation is handled internally by history_aware_retriever
            # and will be captured in the retriever's _get_relevant_documents method

            # If user sent an image, set it on the retriever for multimodal Q&A search.
            # This enables finding similar images in the Q&A knowledge base.
            image_data_url = self._first_image_data_url(media_items)
            if image_data_url is not None:
                self.retriever.current_image = image_data_url
                logger.info("Image set on retriever for multimodal Q&A search")

            # If we have media, inject it into the conversation history BEFORE the RAG call
            # This ensures the multimodal message is available in chat_history for the current turn
            if media_items and isinstance(media_items, list) and len(media_items) > 0:
                # Build multimodal message for history
                multimodal_message = self._build_human_message(customer_message_text, media_items)

                # Add to history BEFORE RAG invocation (critical for current turn context)
                session_history.add_message(multimodal_message)
                logger.info(f"Added {len(media_items)} media item(s) to conversation history")

            # Call RAG chain with text for retrieval
            rag_response = await self.conversational_rag_chain.ainvoke(
                {"input": customer_message_text},
                config={"configurable": {"session_id": history_session_id}},
            )

            standalone_question = self._resolve_standalone_question(customer_message_text)

            retrieved_chunks = rag_response.get("context", [])

            source_summaries = self._summarize_sources(retrieved_chunks)

            return self._build_answer_payload(rag_response["answer"], standalone_question, retrieved_chunks, source_summaries)
        except Exception as error:
            import traceback

            logger.error(f"[ConversationalRAG.answer_customer_message] ERROR for tenant '{self.tenant_name}': {type(error).__name__}: {error}")
            logger.error(traceback.format_exc())
            return {"answer": "", "sources": [], "documents_found": 0, "error_details": f"{type(error).__name__}: {error}"}

    def record_customer_message_in_history(self, history_session_id: str, customer_message_text: str, media_items: Optional[List[Dict[str, Any]]] = None):
        """Add a message to the conversation history for a session."""
        session_history = self._open_session_history(history_session_id)
        session_history.add_message(self._build_human_message(customer_message_text, media_items))

async def generate_rag_answer(
    tenant_name: str,
    customer_message_text: str,
    history_session_id: str,
    agent_config: Optional[dict] = None,
    should_include_qa_pairs: bool = True,
    should_include_documents: bool = True,
    qa_pair_metadata_filter: Optional[Dict[str, Any]] = None,
    tool_names: Optional[List[str]] = None,
    message_context: Optional[Dict[str, Any]] = None,
    media_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Simple function to chat with documents and/or Q&A using conversational RAG.
    Uses Cohere embeddings for vector search.

    Args:
        tenant_name: Name of the tenant
        customer_message_text: User's message/question
        history_session_id: Unique session identifier for conversation
        agent_config: Optional agent configuration
        should_include_qa_pairs: Whether to include Q&A in search
        should_include_documents: Whether to include documents in search
        qa_pair_metadata_filter: Optional filter for Q&A metadata
        tool_names: List of decorated tool names to use
        message_context: MessageContext for accessing group tools (customers, integrations, features)
        media_items: Optional list of media (images only) to include in LangChain format:
               For images: {"type": "image", "base64": "...", "mime_type": "image/jpeg"}
               Note: Audio is NOT stored — it is transcribed to text upstream.

    Returns:
        Dict with answer and source documents
    """
    try:
        conversational_rag = ConversationalRAG(
            tenant_name,
            agent_config=agent_config,
            include_qa=should_include_qa_pairs,
            include_documents=should_include_documents,
            qa_metadata_filter=qa_pair_metadata_filter,
            decorated_function_tools=tool_names,
            request=message_context,
        )
        return await conversational_rag.answer_customer_message(customer_message_text, history_session_id, media_items=media_items)
    except Exception as error:
        return {"error": str(error)}
