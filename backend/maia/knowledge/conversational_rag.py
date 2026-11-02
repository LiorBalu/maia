"""The ConversationalRAG chain: provider/model from the tenant's agent, prompt assembly, retrieval, tool binding and the answer the pipeline sends."""


import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz
from langchain_anthropic import ChatAnthropic
from langchain_classic.chains.combine_documents.stuff import create_stuff_documents_chain
# LangChain 1.0 imports - functions moved to langchain_classic
from langchain_classic.chains.history_aware_retriever import create_history_aware_retriever
from langchain_classic.chains.retrieval import create_retrieval_chain
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from maia.pipeline.context import MessageContext
from maia.services.llm_keys import resolve_anthropic_api_key, resolve_google_api_key, resolve_openai_api_key
from maia.models.prompts import PromptDocument
from maia.models.tenants import TenantDocument
from maia.utils.get_time_of_day import get_time_of_day
from maia.knowledge.search import CombinedRetriever, MultiDocumentRetriever, QARetriever

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
OPENAI_RESPONSES_ONLY_MODELS = ("gpt-6-astra", "gpt-6.1-sol")
OPENAI_NO_REASONING_PREFIXES = ("gpt-5.6", "gpt-6")
OPENAI_REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def openai_model_kwargs(model_name: str, temperature: float) -> Dict[str, Any]:
    """Extra ChatOpenAI arguments for an OpenAI model: temperature only for the gpt-4 generation,
    reasoning_effort="none" for gpt-5.6 / gpt-6 on chat completions, and the Responses API with
    effort "low" for the two models that offer neither (see the table above).
    """
    name = str(model_name)
    if name.startswith(OPENAI_RESPONSES_ONLY_MODELS):
        return {"use_responses_api": True, "reasoning": {"effort": "low"}}
    if name.startswith(OPENAI_NO_REASONING_PREFIXES):
        return {"reasoning_effort": "none"}
    if name.startswith(OPENAI_REASONING_PREFIXES):
        return {}
    return {"temperature": temperature}


def message_text(content: Any) -> str:
    """The text of a model answer: a plain string, or the joined text blocks the Responses API returns."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                parts.append(part.get("text", ""))
        return "".join(parts)
    return "" if content is None else str(content)


class ConversationalRAG:
    """
    Conversational RAG system using LangChain's conversation chains with Redis for session management.
    Supports searching in documents, Q&A, or both.
    Uses Cohere embeddings for vector search.
    """

    def __init__(
        self,
        tenant_name: str,
        agent_config: Optional[dict] = None,
        include_qa: bool = True,
        include_documents: bool = True,
        document_categories: Optional[List[str]] = None,
        qa_categories: Optional[List[str]] = None,
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
        self.document_categories = document_categories
        self.qa_categories = qa_categories
        self.qa_metadata_filter = qa_metadata_filter
        self.decorated_function_tools = decorated_function_tools or []
        self.request: Optional[MessageContext] = request

        # Direct answer threshold: if a retrieved result has score >= this value,
        # return it directly without passing through the LLM chain.
        # Value is a float between 0 and 1 (e.g. 0.95 = 95%). 0 = disabled.
        self.direct_answer_threshold = self._safe_float_static(self.agent_config.get("direct_answer_threshold", 0), 0)
        if self.direct_answer_threshold > 0:
            logger.info(f"Direct answer threshold set to {self.direct_answer_threshold} for tenant '{tenant_name}'")

        # Get the tenant to retrieve API keys
        tenant = TenantDocument.objects(name=tenant_name).first()
        if not tenant:
            raise ValueError(f"Tenant {tenant_name} not found")

        self.openai_api_key = resolve_openai_api_key(tenant)

        # Setup LLM based on provider
        provider = self.agent_config.get("provider", "openai")

        if provider == "openai":
            if not self.openai_api_key:
                raise ValueError(f"No OpenAI API key found for tenant {tenant_name}")
            model_name = self.agent_config.get("model", "gpt-6.1-terra")
            self.llm = ChatOpenAI(
                openai_api_key=self.openai_api_key,
                model=model_name,
                **openai_model_kwargs(model_name, self.agent_config.get("temperature", 0)),
            )
        elif provider == "anthropic":
            key = resolve_anthropic_api_key(tenant)
            if not key:
                raise ValueError(f"No Anthropic API key found for tenant {tenant_name}")
            self.llm = ChatAnthropic(
                anthropic_api_key=key,
                model=self.agent_config.get("model", "claude-2"),
                temperature=self.agent_config.get("temperature", 0),
            )
        elif provider == "google_llm":
            key = resolve_google_api_key(tenant)
            if not key:
                raise ValueError(f"No Google LLM API key found for tenant {tenant_name}")
            self.llm = ChatGoogleGenerativeAI(
                google_api_key=key,
                model=self.agent_config.get("model", "gemini-2.0-flash"),
                temperature=self.agent_config.get("temperature", 0),
            )

        # 🔧 Bind tools to LLM if provided
        self._setup_tools()

        # Setup retriever (uses Cohere embeddings internally)
        self._setup_retriever()

        # Setup conversation chain
        self._setup_conversation_chain()

    @staticmethod
    def _safe_float_static(value: Any, default: float) -> float:
        """Safely convert a value to float."""
        try:
            if value is None:
                return default
            if isinstance(value, str) and value.strip().lower() in ("none", "null", ""):
                return default
            return float(value)
        except Exception:
            return default

    def _try_direct_answer(
        self, message: str, session_id: str, media: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Check if any retrieved result has a score >= direct_answer_threshold.
        If so, return the answer directly without invoking the LLM chain.
        Works for both Q&A results (returns the qa_answer) and document results (returns the chunk content).

        Returns:
            A full response dict if a direct answer was found, or None to proceed normally.
        """
        # Detect if user sent an image
        has_image = False
        if media and isinstance(media, list):
            for item in media:
                if isinstance(item, dict) and item.get("type") == "image" and item.get("base64"):
                    has_image = True
                    break

        # For text queries, respect the configured threshold (0 = disabled).
        # For image queries, always try — image-to-image matching should bypass the LLM
        # since the LLM can't reliably match images to a Q&A knowledge base.
        IMAGE_DIRECT_ANSWER_THRESHOLD = 0.65
        if not has_image and (not self.direct_answer_threshold or self.direct_answer_threshold <= 0):
            return None

        effective_threshold = (
            self.direct_answer_threshold
            if self.direct_answer_threshold and self.direct_answer_threshold > 0
            else IMAGE_DIRECT_ANSWER_THRESHOLD
        )

        try:
            # Run retrieval only (no LLM)

            # If we have media with an image, set it on the retriever for multimodal search
            if media and isinstance(media, list):
                for item in media:
                    if isinstance(item, dict) and item.get("type") == "image" and item.get("base64"):
                        image_data_uri = f"data:{item.get('mime_type', 'image/jpeg')};base64,{item['base64']}"
                        self.retriever.current_image = image_data_uri
                        break

            # Use the retriever directly with the raw message
            # (no history-aware reformulation — for direct answer we match the literal query)
            docs = self.retriever.invoke(message)

            if not docs:
                return None

            # Find the best scoring document that meets the threshold
            best_doc = None
            best_score = 0.0
            for doc in docs:
                meta = getattr(doc, "metadata", None) or {}
                score = meta.get("final_score", meta.get("vector_score", 0))
                try:
                    score = float(score)
                except (TypeError, ValueError):
                    score = 0.0
                if score >= effective_threshold and score > best_score:
                    best_doc = doc
                    best_score = score

            if best_doc is None:
                return None

            meta = getattr(best_doc, "metadata", None) or {}
            source_type = meta.get("source_type", "document")

            # Build the direct answer
            if source_type == "qa" and meta.get("qa_answer"):
                answer = meta["qa_answer"]
            else:
                # For documents, return the chunk content as-is
                answer = best_doc.page_content or ""

            logger.warning(f"Direct answer triggered! Score {best_score:.4f} >= threshold {effective_threshold} "
                f"({'image Q&A' if has_image else 'text'}). Skipping LLM chain.")

            # Build sources list from ALL retrieved docs (same format as normal flow)
            sources: List[Dict[str, Any]] = []
            for doc in docs:
                m = getattr(doc, "metadata", None) or {}
                st = m.get("source_type", "document")
                content_preview = doc.page_content or ""
                if st == "qa":
                    sources.append(
                        {
                            "source_type": "qa",
                            "qa_id": m.get("qa_id"),
                            "question": m.get("question"),
                            "answer": m.get("qa_answer"),
                            "score": m.get("final_score", m.get("vector_score")),
                            "vector_score": m.get("vector_score"),
                            "content_preview": content_preview,
                        }
                    )
                else:
                    sources.append(
                        {
                            "source_type": "document",
                            "title": m.get("title", m.get("document_title", "Unknown")),
                            "document_id": m.get("document_id"),
                            "part_number": m.get("part_number"),
                            "score": m.get("final_score", m.get("vector_score", m.get("score"))),
                            "vector_score": m.get("vector_score"),
                            "content_preview": content_preview,
                        }
                    )

            # Save question + answer to history so conversation stays coherent
            history = self._get_session_history(session_id)
            if media and isinstance(media, list) and len(media) > 0:
                content_parts = [{"type": "text", "text": message}]
                content_parts.extend(media)
                history.add_message(HumanMessage(content=content_parts))
            else:
                history.add_message(HumanMessage(content=message))
            from langchain_core.messages import AIMessage

            history.add_message(AIMessage(content=answer))

            return {
                "answer": answer,
                "reformulated_question": message,
                "sources": sources,
                "documents_found": len(docs),
                "sources_found": len(sources),
                "documents_sources_count": sum(1 for s in sources if s.get("source_type") == "document"),
                "qa_sources_count": sum(1 for s in sources if s.get("source_type") == "qa"),
                "direct_answer": True,
                "direct_answer_score": best_score,
            }

        except Exception as e:
            logger.error(f"Direct answer check failed, falling back to normal chain: {e}")
            return None

    def _setup_tools(self):
        """Setup and bind tools to the LLM."""
        from maia.pipeline.tools.langchain_adapter import convert_to_langchain_tools

        # Convert decorated tools and request tools to LangChain format
        tools = convert_to_langchain_tools(
            function_names=(self.decorated_function_tools if self.decorated_function_tools else None),
            request=self.request,
        )

        # Save tools for later use in Agent
        self.tools = tools

        if tools:
            # Bind tools to the LLM
            self.llm = self.llm.bind_tools(tools)
            logger.info(f"Bound {len(tools)} tools to LLM for tenant '{self.tenant_name}'")
        else:
            logger.info(f"ℹNo tools to bind for tenant '{self.tenant_name}'")

    def _setup_retriever(self):
        """Setup the document retriever using Cohere embeddings."""
        all_retrievers = []

        def _safe_int(value: Any, default: int) -> int:
            """int(value), or the default for None / empty / invalid values."""
            try:
                if value is None:
                    return default
                if isinstance(value, str) and value.strip().lower() in ("none", "null", ""):
                    return default
                return int(value)
            except Exception:
                return default

        documents_top_k = max(0, _safe_int(self.agent_config.get("top_k_general", 10), 10))
        qa_top_k = max(0, _safe_int(self.agent_config.get("top_k_qa", 5), 5))

        # Extract score_threshold from agent config (default 0 = no filtering)
        score_threshold = self._safe_float_static(self.agent_config.get("score_threshold", 0), 0)

        # Add document retriever if enabled
        if self.include_documents:
            doc_retriever = MultiDocumentRetriever(
                tenant_name=self.tenant_name,
                top_k=documents_top_k,
                categories=self.document_categories,
            )
            all_retrievers.append(doc_retriever)
            logger.info(f"Added Cohere document retriever for tenant '{self.tenant_name}'")

        # Add Q&A retriever if enabled
        if self.include_qa:
            try:
                qa_retriever = QARetriever(
                    tenant_name=self.tenant_name,
                    top_k=qa_top_k,
                    categories=self.qa_categories,
                    metadata_filter=self.qa_metadata_filter,
                )
                all_retrievers.append(qa_retriever)
                logger.info(f"Added Cohere Q&A retriever for tenant '{self.tenant_name}'")
            except Exception as e:
                logger.error(f"Could not setup Q&A retriever: {e}")

        # Combine retrievers
        self.retriever = CombinedRetriever(
            retrievers=all_retrievers,
            documents_top_k=documents_top_k,
            qa_top_k=qa_top_k,
            top_k=documents_top_k + qa_top_k,
            score_threshold=score_threshold,
        )
        if score_threshold > 0:
            logger.info(f"Score threshold set to {score_threshold} for tenant '{self.tenant_name}'")

    def _setup_conversation_chain(self):
        """Setup the conversational retrieval chain."""
        # Contextualize question prompt
        contextualize_q_system_prompt = """Given a chat history and the latest user question which might reference context in the chat history,
        formulate a standalone question which can be understood without the chat history.

        CRITICAL: You MUST ONLY return a reformulated question. Do NOT provide an answer. Do NOT explain anything. ONLY return the question itself.

        If the question is already standalone, return it exactly as is without any changes or additions.
        DO NOT add explanations, answers, or any other text. ONLY the question."""

        # Try to load from PromptDocument if available
        contextualize_q_system_prompt_doc = PromptDocument.objects(
            tenant_name=self.tenant_name, name="contextualize_q_system_prompt"
        ).first()
        if contextualize_q_system_prompt_doc:
            contextualize_q_system_prompt = self._escape_prompt_content(contextualize_q_system_prompt_doc.prompt)
        else:
            PromptDocument(
                tenant_name=self.tenant_name, name="contextualize_q_system_prompt", content=contextualize_q_system_prompt
            ).save()

        contextualize_q_prompt = ChatPromptTemplate.from_messages(
            [("system", contextualize_q_system_prompt), MessagesPlaceholder("chat_history"), ("human", "{input}")]
        )

        # Create history-aware retriever with text-only filter for chat history
        # This filters out media (images, audio) during question reformulation
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        from langchain_core.runnables import RunnableLambda

        def filter_text_only_from_history(inputs):
            """Extract text-only content from chat history, removing media."""
            chat_history = inputs.get("chat_history", [])
            text_only_history = []

            for msg in chat_history:
                if isinstance(msg, (HumanMessage, AIMessage, SystemMessage)):
                    # Extract text content
                    if isinstance(msg.content, str):
                        # Simple text message - keep as is
                        text_only_history.append(msg)
                    elif isinstance(msg.content, list):
                        # Multimodal message - extract only text parts
                        text_parts = []
                        for part in msg.content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)

                        if text_parts:
                            # Create new message with only text
                            text_content = " ".join(text_parts)
                            if isinstance(msg, HumanMessage):
                                text_only_history.append(HumanMessage(content=text_content))
                            elif isinstance(msg, AIMessage):
                                text_only_history.append(AIMessage(content=text_content))
                            elif isinstance(msg, SystemMessage):
                                text_only_history.append(SystemMessage(content=text_content))
                else:
                    # Keep other message types as is
                    text_only_history.append(msg)

            # Return inputs with filtered history
            return {**inputs, "chat_history": text_only_history}

        # Wrap create_history_aware_retriever with text-only filter
        text_filter = RunnableLambda(filter_text_only_from_history)
        base_retriever = create_history_aware_retriever(self.llm, self.retriever, contextualize_q_prompt)

        # Chain: filter text-only -> history-aware retriever
        self.history_aware_retriever = text_filter | base_retriever

        # QA system prompt

        qa_system_prompt = self.generate_qa_prompt()

        # Check if we have tools to setup an Agent Executor logic
        if hasattr(self, "tools") and self.tools:
            from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
            from langchain_core.runnables import RunnableLambda

            # Prompt for Agent: Needs agent_scratchpad for tool outputs
            qa_prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", qa_system_prompt),
                    MessagesPlaceholder("chat_history"),
                    ("human", "{input}"),
                    MessagesPlaceholder("agent_scratchpad"),
                ]
            )

            # Create the Agent (LLM + Tools + Prompt)
            agent = create_tool_calling_agent(self.llm, self.tools, qa_prompt)

            # Create the Executor (The loop that runs tools)
            agent_executor = AgentExecutor(agent=agent, tools=self.tools, verbose=True)

            # Wrapper to bridge RAG Retrival Output -> Agent Input
            async def run_agent_entry_point(input_dict):
                # Retrieve the documents found by the previous step
                """Bridge between the retrieval step and the tool-calling agent: format the retrieved documents as the
                {context} of the prompt and run the agent executor.
                """
                context_docs = input_dict.get("context", [])

                # Format docs to string
                if isinstance(context_docs, list):
                    context_str = "\n\n".join([d.page_content for d in context_docs])
                else:
                    context_str = str(context_docs)

                # Calculate current_date for prompt template
                israel_timezone = pytz.timezone("Asia/Jerusalem")
                now = datetime.now(israel_timezone)
                formatted_date = now.strftime("%a, %d %b %Y, %H:%M")

                # Run the agent with documents injected in {context}
                result = await agent_executor.ainvoke(
                    {
                        "input": input_dict["input"],
                        "chat_history": input_dict.get("chat_history", []),
                        "context": context_str,
                        "current_date": formatted_date,
                    }
                )
                return message_text(result["output"])

            # This replaces create_stuff_documents_chain
            question_answer_chain = RunnableLambda(run_agent_entry_point)

        else:
            # ORIGINAL LOGIC if no tools
            qa_prompt = ChatPromptTemplate.from_messages(
                [("system", qa_system_prompt), MessagesPlaceholder("chat_history"), ("human", "{input}")]
            )

            # Create question-answer chain
            question_answer_chain = create_stuff_documents_chain(self.llm, qa_prompt)

        # Create the full RAG chain
        self.rag_chain = create_retrieval_chain(self.history_aware_retriever, question_answer_chain)

        # Wrap with message history
        self.conversational_rag_chain = RunnableWithMessageHistory(
            self.rag_chain,
            self._get_session_history,
            input_messages_key="input",
            history_messages_key="chat_history",
            output_messages_key="answer",
        )

    @staticmethod
    def _escape_prompt_content(text: str) -> str:
        """
        Escape curly braces in dynamic prompt content loaded from DB so that
        LangChain's ChatPromptTemplate does not mistake JSON examples or any
        {word} patterns as template variables.

        We escape ALL braces here because this method is only called on the
        dynamic sections — the real template variables ({context}, {input},
        {chat_history}, {agent_scratchpad}, {current_date}) are added separately
        as literal strings AFTER this escaping step.
        """
        return text.replace("{", "{{").replace("}", "}}")

    def generate_qa_prompt(self):
        """Assemble the system prompt: date and time of day, the tenant's default prompt, the platform prompt
        (``platform_<platform>``, when one exists) and the {context} slot; braces in DB content are escaped.
        """
        prompt_doc = PromptDocument.objects(tenant_name=self.tenant_name, name="default").first()
        if not prompt_doc:
            prompt_doc = PromptDocument(tenant_name=self.tenant_name, name="default", content="")
            prompt_doc.save()
            prompt_doc.reload()
        # Prepare message for chatbot
        israel_timezone = pytz.timezone("Asia/Jerusalem")
        now = datetime.now(israel_timezone)
        formatted_date = now.strftime("%a, %d %b %Y, %H:%M")
        additional_instructions = ""
        if self.request:
            # fetch additional prompt for specific platform
            platform_prompt_document = PromptDocument.objects(
                tenant_name=self.tenant_name, name=f"platform_{self.request['platform']}"
            ).first()
            if platform_prompt_document:
                additional_instructions += f"\n\n{platform_prompt_document.prompt}"

        # Escape curly braces in ALL dynamic content from the DB so that
        # LangChain's ChatPromptTemplate does not treat JSON like {"name": "..."} as
        # missing template variables (which causes a KeyError crash → silent fallback
        # to plain GPT with no RAG and no tools).
        safe_base_prompt = self._escape_prompt_content(prompt_doc.prompt)
        safe_additional = self._escape_prompt_content(additional_instructions)

        qa_system_prompt = (
            f"Today is {formatted_date}. We are now in {get_time_of_day(now)}."
            + safe_base_prompt
            + "\n\n"
            + safe_additional
            + "\n\n"
            + """
        <rag_context>
        {context}
        </rag_context>
        """
        )

        return qa_system_prompt

    def _get_session_history(self, session_id: str) -> BaseChatMessageHistory:
        """Get chat message history for a session."""
        from maia.services.redis_clients import make_redis_chat_history

        return make_redis_chat_history(session_id=session_id, key_prefix=f"chat_history:{self.tenant_name}:")

    async def chat(self, message: str, session_id: str, media: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Chat with documents maintaining conversation history.

        Args:
            message: User's text message
            session_id: Unique session identifier
            media: Optional list of media items in LangChain format

        Returns:
            Dict with answer and source documents
        """
        try:
            # ⚡ Direct answer shortcut: if a result scores above the threshold,
            # return it immediately without invoking the LLM chain (saves tokens & latency).
            direct = self._try_direct_answer(message, session_id, media=media)
            if direct is not None:
                return direct

            # Get history for media injection
            history = self._get_session_history(session_id)
            reformulated_question = message  # Fallback default

            # IMPORTANT: For retrieval, always use text only (Cohere can't embed HumanMessage)
            # The media will be injected later in the QA prompt
            # The question reformulation is handled internally by history_aware_retriever
            # and will be captured in the retriever's _get_relevant_documents method

            # If user sent an image, set it on the retriever for multimodal Q&A search.
            # This enables finding similar images in the Q&A knowledge base.
            if media and isinstance(media, list):
                for item in media:
                    if isinstance(item, dict) and item.get("type") == "image" and item.get("base64"):
                        image_data_uri = f"data:{item.get('mime_type', 'image/jpeg')};base64,{item['base64']}"
                        self.retriever.current_image = image_data_uri
                        logger.info("Image set on retriever for multimodal Q&A search")
                        break

            # If we have media, inject it into the conversation history BEFORE the RAG call
            # This ensures the multimodal message is available in chat_history for the current turn
            if media and isinstance(media, list) and len(media) > 0:
                # Build multimodal message for history
                content_parts = [{"type": "text", "text": message}]
                content_parts.extend(media)
                multimodal_message = HumanMessage(content=content_parts)

                # Add to history BEFORE RAG invocation (critical for current turn context)
                history.add_message(multimodal_message)
                logger.info(f"Added {len(media)} media item(s) to conversation history")

            # Call RAG chain with text for retrieval
            response = await self.conversational_rag_chain.ainvoke(
                {"input": message},
                config={"configurable": {"session_id": session_id}},
            )

            # Retrieve the reformulated question from the retriever
            # The history_aware_retriever passes the reformulated question to the retriever
            if hasattr(self, "retriever") and hasattr(self.retriever, "last_reformulated_query"):
                if self.retriever.last_reformulated_query:
                    reformulated_question = self.retriever.last_reformulated_query
                    logger.info(f"✅ Reformulated question retrieved from retriever: '{reformulated_question[:100]}..."
                        if len(reformulated_question) > 100
                        else f"✅ Reformulated question: '{reformulated_question}'")

            context_docs = response.get("context", [])

            sources: List[Dict[str, Any]] = []
            for doc in context_docs:
                meta = getattr(doc, "metadata", None) or {}
                source_type = meta.get("source_type", "document")
                content_preview = doc.page_content or ""

                if source_type == "qa":
                    sources.append(
                        {
                            "source_type": "qa",
                            "qa_id": meta.get("qa_id"),
                            "question": meta.get("question"),
                            "answer": meta.get("qa_answer"),
                            # Use final_score (rerank) as main score — this matches the threshold filter
                            "score": meta.get("final_score", meta.get("vector_score")),
                            "vector_score": meta.get("vector_score"),
                            "content_preview": content_preview,
                        }
                    )
                else:
                    sources.append(
                        {
                            "source_type": "document",
                            "title": meta.get("title", meta.get("document_title", "Unknown")),
                            "document_id": meta.get("document_id"),
                            "part_number": meta.get("part_number"),
                            # Use final_score (rerank) as main score — this matches the threshold filter
                            "score": meta.get("final_score", meta.get("vector_score", meta.get("score"))),
                            "vector_score": meta.get("vector_score"),
                            "content_preview": content_preview,
                        }
                    )

            return {
                "answer": response["answer"],
                "reformulated_question": reformulated_question,
                "sources": sources,
                "documents_found": len(context_docs),
                "sources_found": len(sources),
                "documents_sources_count": sum(1 for s in sources if s.get("source_type") == "document"),
                "qa_sources_count": sum(1 for s in sources if s.get("source_type") == "qa"),
            }
        except Exception as e:
            import traceback

            logger.error(f"[ConversationalRAG.chat] ERROR for tenant '{self.tenant_name}': {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            return {"answer": "", "sources": [], "documents_found": 0, "error_details": f"{type(e).__name__}: {e}"}

    def add_message_to_history(self, session_id: str, text: str, media: Optional[List[Dict[str, Any]]] = None):
        """Add a message to the conversation history for a session."""
        history = self._get_session_history(session_id)
        if media and isinstance(media, list) and len(media) > 0:
            # Build multimodal message for history
            content_parts = [{"type": "text", "text": text}]
            content_parts.extend(media)
            multimodal_message = HumanMessage(content=content_parts)
            history.add_message(multimodal_message)
        else:
            history.add_message(HumanMessage(content=text))

async def chat_with_documents(
    tenant_name: str,
    message: str,
    session_id: str,
    agent_config: Optional[dict] = None,
    include_qa: bool = True,
    include_documents: bool = True,
    document_categories: Optional[List[str]] = None,
    qa_categories: Optional[List[str]] = None,
    qa_metadata_filter: Optional[Dict[str, Any]] = None,
    decorated_function_tools: Optional[List[str]] = None,
    request: Optional[Dict[str, Any]] = None,
    media: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Simple function to chat with documents and/or Q&A using conversational RAG.
    Uses Cohere embeddings for vector search.

    Args:
        tenant_name: Name of the tenant
        message: User's message/question
        session_id: Unique session identifier for conversation
        agent_config: Optional agent configuration
        include_qa: Whether to include Q&A in search
        include_documents: Whether to include documents in search
        document_categories: Optional list of categories to filter documents by
        qa_categories: Optional list of categories to filter Q&A by
        qa_metadata_filter: Optional filter for Q&A metadata
        decorated_function_tools: List of decorated tool names to use
        request: MessageContext for accessing group tools (customers, integrations, features)
        media: Optional list of media (images only) to include in LangChain format:
               For images: {"type": "image", "base64": "...", "mime_type": "image/jpeg"}
               Note: Audio is NOT stored — it is transcribed to text upstream.

    Returns:
        Dict with answer and source documents
    """
    try:
        rag = ConversationalRAG(
            tenant_name,
            agent_config=agent_config,
            include_qa=include_qa,
            include_documents=include_documents,
            document_categories=document_categories,
            qa_categories=qa_categories,
            qa_metadata_filter=qa_metadata_filter,
            decorated_function_tools=decorated_function_tools,
            request=request,
        )
        return await rag.chat(message, session_id, media=media)
    except Exception as e:
        return {"error": str(e)}
