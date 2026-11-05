"""
Cohere Embeddings Wrapper for RAG System.

This module provides a LangChain-compatible wrapper for Cohere embed-v4.0.

Features:
- Automatic batching (96 texts per request)
- Tenacity retry logic for transient failures
- Proper input_type handling for indexing vs retrieval

Configuration:
- Model: embed-v4.0
- Dimensions: 1024
- Batch size: 96 (max texts per API call)
"""

import os
from typing import List
import cohere
from langchain_core.embeddings import Embeddings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv
from requests.exceptions import HTTPError
COHERE_MAX_TEXTS_PER_REQUEST = 96  # Max texts per API call to avoid rate limits
load_dotenv()


def _require_cohere_api_key() -> str:
    """The COHERE_API_KEY environment variable; raises ValueError when it is not set."""
    cohere_api_key = os.getenv("COHERE_API_KEY")
    if not cohere_api_key:
        raise ValueError("COHERE_API_KEY is not set")
    return cohere_api_key


class CohereEmbeddingsV4(Embeddings):
    """
    LangChain-compatible wrapper for Cohere embed-v4.0.
    
    Features:
    - Automatic batching (96 texts per request)
    - Tenacity retry logic for transient failures
    - Proper input_type handling for indexing vs retrieval
    
    Usage:
        # For indexing documents
        embeddings = CohereEmbeddingsV4(input_type="search_document")
        vectors = embeddings.embed_documents(["text1", "text2", ...])
        
        # For search queries
        embeddings = CohereEmbeddingsV4(input_type="search_query")
        vector = embeddings.embed_query("search query")
    """
    
    embedding_model_name: str = "embed-v4.0"
    embedding_dimensions: int = 1024
    
    def __init__(self, input_type: str = "search_document"):
        """
        Initialize Cohere embeddings wrapper.
        
        Args:
            input_type: "search_document" for indexing, "search_query" for retrieval
        """
        cohere_api_key = _require_cohere_api_key()
        self.client = cohere.ClientV2(api_key=cohere_api_key)
        self.input_type = input_type
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((HTTPError, TimeoutError)),
        reraise=True
    )
    def _embed_text_batch(self, batch_texts: List[str], input_type: str) -> List[List[float]]:
        """
        Embed a single batch with retry logic.
        
        Args:
            batch_texts: List of texts to embed (max 96)
            input_type: "search_document" or "search_query"
            
        Returns:
            List of embedding vectors
        """
        embed_response = self.client.embed(
            texts=batch_texts,
            model=self.embedding_model_name,
            input_type=input_type,
            embedding_types=["float"],
            output_dimension=1024
        )
        return embed_response.embeddings.float
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed multiple texts for indexing.
        Automatically batches into chunks of 96.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            List of embedding vectors (1024 dimensions each)
        """
        document_embeddings = []
        
        for batch_start in range(0, len(texts), COHERE_MAX_TEXTS_PER_REQUEST):
            batch_texts = texts[batch_start:batch_start + COHERE_MAX_TEXTS_PER_REQUEST]
            batch_embeddings = self._embed_text_batch(batch_texts, input_type="search_document")
            document_embeddings.extend(batch_embeddings)
        
        return document_embeddings
    
    def embed_query(self, text: str) -> List[float]:
        """
        Embed single query for retrieval.
        
        Args:
            text: Query text to embed
            
        Returns:
            Embedding vector (1024 dimensions)
        """
        query_batch_embeddings = self._embed_text_batch([text], input_type="search_query")
        return query_batch_embeddings[0]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((HTTPError, TimeoutError)),
        reraise=True
    )
    def embed_image_from_data_url(self, image_data_uri: str) -> List[float]:
        """
        Embed a single image using Cohere embed-v4.0 multimodal support.
        
        Args:
            image_data_uri: Base64 data URI string (e.g. "data:image/jpeg;base64,/9j/4AAQ...")
            
        Returns:
            Embedding vector (1024 dimensions)
        """
        image_embed_response = self.client.embed(
            images=[image_data_uri],
            model=self.embedding_model_name,
            input_type="image",
            embedding_types=["float"],
            output_dimension=1024
        )
        return image_embed_response.embeddings.float[0]
