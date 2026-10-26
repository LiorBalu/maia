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
COHERE_BATCH_SIZE = 96  # Max texts per API call to avoid rate limits
load_dotenv()

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
    
    model: str = "embed-v4.0"
    dimensions: int = 1024
    
    def __init__(self, input_type: str = "search_document"):
        """
        Initialize Cohere embeddings wrapper.
        
        Args:
            input_type: "search_document" for indexing, "search_query" for retrieval
        """
        COHERE_API_KEY = os.getenv("COHERE_API_KEY")
        if not COHERE_API_KEY:
            raise ValueError("COHERE_API_KEY is not set")
        self.client = cohere.ClientV2(api_key=os.getenv("COHERE_API_KEY"))
        self.input_type = input_type
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((HTTPError, TimeoutError)),
        reraise=True
    )
    def _embed_batch(self, texts: List[str], input_type: str) -> List[List[float]]:
        """
        Embed a single batch with retry logic.
        
        Args:
            texts: List of texts to embed (max 96)
            input_type: "search_document" or "search_query"
            
        Returns:
            List of embedding vectors
        """
        response = self.client.embed(
            texts=texts,
            model=self.model,
            input_type=input_type,
            embedding_types=["float"],
            output_dimension=1024
        )
        return response.embeddings.float
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed multiple texts for indexing.
        Automatically batches into chunks of 96.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            List of embedding vectors (1024 dimensions each)
        """
        all_embeddings = []
        
        for i in range(0, len(texts), COHERE_BATCH_SIZE):
            batch = texts[i:i + COHERE_BATCH_SIZE]
            batch_embeddings = self._embed_batch(batch, input_type="search_document")
            all_embeddings.extend(batch_embeddings)
        
        return all_embeddings
    
    def embed_query(self, text: str) -> List[float]:
        """
        Embed single query for retrieval.
        
        Args:
            text: Query text to embed
            
        Returns:
            Embedding vector (1024 dimensions)
        """
        embeddings = self._embed_batch([text], input_type="search_query")
        return embeddings[0]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((HTTPError, TimeoutError)),
        reraise=True
    )
    def embed_image(self, image_data_uri: str) -> List[float]:
        """
        Embed a single image using Cohere embed-v4.0 multimodal support.
        
        Args:
            image_data_uri: Base64 data URI string (e.g. "data:image/jpeg;base64,/9j/4AAQ...")
            
        Returns:
            Embedding vector (1024 dimensions)
        """
        response = self.client.embed(
            images=[image_data_uri],
            model=self.model,
            input_type="image",
            embedding_types=["float"],
            output_dimension=1024
        )
        return response.embeddings.float[0]
