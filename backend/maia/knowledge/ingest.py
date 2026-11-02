"""Knowledge ingestion: load a file or URL into text, split it into parts, embed them with Cohere and upsert them into the tenant's Qdrant collection (Celery runs this)."""


import logging
from typing import Any, Dict

# LangChain 1.0 imports - functions moved to langchain_classic
from langchain_community.document_loaders import (
    TextLoader,
    UnstructuredFileLoader,
    UnstructuredPowerPointLoader,
)
from langchain_core.documents import Document as LangchainDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter

from maia.models.knowledge_documents import EmbeddedDocument, EmbeddedPart
from maia.knowledge.qdrant import (
    COHERE_VECTOR_SIZE,
    doc_point_id,
    docs_collection_name,
    ensure_documents_collection,
    get_qdrant_client,
)
from maia.utils.read_csv import read_csv
from maia.utils.read_pdf import read_pdf
from maia.utils.read_word import read_word
from maia.utils.read_xlsx import read_xlsx

logger = logging.getLogger(__name__)


def load_document_with_langchain(file_path_or_url: str) -> str:
    """
    Load document content by file extension or URL type.
    Supports: PDF, DOCX, TXT, XLSX, XLS, CSV (the dashboard's upload types), PPTX, HTML (web pages), and more.

    Args:
        file_path_or_url: Path or URL to the document

    Returns:
        Extracted text content from the document
    """
    import tempfile
    from pathlib import Path

    import requests
    from langchain_community.document_loaders import WebBaseLoader

    # Check if it's a web URL (HTML page)
    if file_path_or_url.startswith(("http://", "https://")):
        # Get file extension from URL
        file_ext = Path(file_path_or_url).suffix.lower()

        # If no file extension or common web extensions, treat as HTML
        if not file_ext or file_ext in [".html", ".htm", ".php", ".asp", ".aspx", ""]:
            try:
                logger.info(f"Loading web page: {file_path_or_url}")
                loader = WebBaseLoader(file_path_or_url)
                documents = loader.load()
                content = "\n\n".join([doc.page_content for doc in documents])
                logger.info(f"Successfully loaded {len(content)} characters from web page")
                return content
            except Exception as e:
                logger.error(f"WebBaseLoader failed, trying standard download: {e}")
                # Fall through to download as file

        # Download file from URL
        response = requests.get(file_path_or_url)
        response.raise_for_status()

        # Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
            tmp_file.write(response.content)
            file_path = tmp_file.name
    else:
        file_path = file_path_or_url

    # Get file extension
    file_ext = Path(file_path).suffix.lower()

    try:
        # Choose loader based on file extension
        if file_ext == ".pdf":
            return read_pdf(file_path_or_url)
        elif file_ext == ".docx":
            return read_word(file_path_or_url)
        elif file_ext == ".doc":
            # python-docx reads only the Office Open XML format; the dashboard no longer offers .doc.
            raise ValueError("Legacy .doc files are not supported; save the file as .docx")
        elif file_ext in [".xlsx", ".xls"]:
            return read_xlsx(file_path_or_url)
        elif file_ext == ".csv":
            return read_csv(file_path_or_url)
        elif file_ext in [".pptx", ".ppt"]:
            loader = UnstructuredPowerPointLoader(file_path)
        elif file_ext in [".txt", ".md"]:
            loader = TextLoader(file_path)
        elif file_ext in [".html", ".htm"]:
            # For local HTML files
            loader = TextLoader(file_path)
        else:
            # Fallback: Universal loader for any file type
            logger.info(f"Using universal loader for {file_ext}")
            loader = UnstructuredFileLoader(file_path)

        # Load and extract text
        documents = loader.load()
        content = "\n\n".join([doc.page_content for doc in documents])

        return content

    except Exception as e:
        logger.error(f"Error loading document with Langchain: {e}")
        # Fallback to original read_pdf if available
        raise


def create_embedding_parts_for_document(document_id: str):
    """
    Split document into parts and create embeddings using Cohere.
    """
    document = EmbeddedDocument.objects(id=document_id).first()
    if not document:
        logger.warning(f"Document {document_id} not found")
        return

    # Delete existing parts for this document
    existing_parts_count = EmbeddedPart.objects(document_id=str(document_id)).count()
    if existing_parts_count > 0:
        logger.info(f" Deleting {existing_parts_count} existing parts...")
        EmbeddedPart.objects(document_id=str(document_id)).delete()

    # Load document content
    if document.content:
        # Prefer existing content (e.g., from web scraping)
        logger.info(f"Using existing content ({len(document.content)} characters)")
        content = document.content
    elif document.url:
        # Load from URL using Langchain (supports multiple formats)
        logger.info(f"Loading document from URL: {document.url}")
        try:
            content = load_document_with_langchain(document.url)
        except Exception as e:
            logger.error(f"Error loading with Langchain: {e}")
            return
    else:
        logger.info(f"No content or URL found for document {document_id}")
        return

    # Create a Langchain Document
    doc_categories = [c for c in (document.categories or []) if c]
    langchain_doc = LangchainDocument(
        page_content=content,
        metadata={
            "document_id": str(document.id),
            "tenant_name": document.tenant_name,
            "title": document.title,
            "categories": doc_categories,
        },
    )

    # Split into chunks
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    docs = text_splitter.split_documents([langchain_doc])

    logger.info(f"Document split into {len(docs)} chunks")

    # Update document metadata (without parts to avoid size limit)
    document.content = ""  # Clear content to save space
    document.is_indexed = False
    document.total_parts = len(docs)  # Store count for reference
    document.save()

    # Save each part individually to avoid document size limit
    logger.info(f"Saving {len(docs)} parts to MongoDB...")
    for idx, doc in enumerate(docs):
        vector_id = doc_point_id(str(document.id), idx)

        embedded_part = EmbeddedPart(
            part_number=idx,
            content=doc.page_content,
            vector_id=vector_id,
            vector_for_backup=None,
            document_id=str(document.id),
            tenant_name=document.tenant_name,
        )
        embedded_part.save()  # Save each part independently
        logger.info(f"  Part {idx + 1}/{len(docs)} saved")

    logger.info(f"Document {document_id} split into {len(docs)} parts and saved to MongoDB")
    logger.info("Now creating Cohere embeddings...")

    create_embeddings_for_parts(document_id)


def create_embeddings_for_parts(document_id: str):
    """
    Create Cohere embeddings for all parts of a document using Celery tasks for parallel processing.
    Uses Cohere embed-v4.0 with int8 quantization.
    """
    document = EmbeddedDocument.objects(id=document_id).first()
    if not document:
        logger.warning(f"Document {document_id} not found")
        return

    parts = EmbeddedPart.objects(document_id=str(document_id)).order_by("part_number")

    if not parts:
        logger.warning(f"No parts found for document {document_id}. Run create_embedding_parts_for_document first.")
        return

    # Use centralized Qdrant client
    client = get_qdrant_client()

    # Shared per-tenant collection. Create it (with payload indexes) if missing;
    # never drop it — it holds every other document in the tenant.
    collection_name = ensure_documents_collection(client, document.tenant_name)

    # Re-indexing a document must be clean: drop only THIS document's existing
    # points (deterministic ids are overwritten on upsert anyway, but a re-split
    # can change the part count, leaving stale tail parts otherwise).
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    try:
        client.delete(
            collection_name=collection_name,
            points_selector=Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=str(document.id)))]),
        )
    except Exception as e:
        logger.error(f"Could not clear previous points for document {document.id}: {e}")

    # Import Celery tasks
    from celery import chord

    from background_tasks import embed_chunk_task, mark_document_indexed_task

    # Prepare task arguments
    total_parts = parts.count()
    logger.info(f"Creating {total_parts} Cohere embedding tasks...")

    # Create tasks for each part
    task_group = []
    for embedded_part in parts:
        task = embed_chunk_task.s(
            document_id=str(document.id),
            part_id=str(embedded_part.id),
            part_number=embedded_part.part_number,
            content=embedded_part.content,
            tenant_name=document.tenant_name,
            collection_name=collection_name,
        )
        task_group.append(task)

    # Execute all tasks in parallel with a callback when all complete
    # Use chord: when all tasks finish, call the callback
    callback = mark_document_indexed_task.s(str(document.id))
    job = chord(task_group)(callback)

    logger.info(f"{total_parts} Cohere embedding tasks scheduled. Processing in background...")
    logger.info("Document will be marked as indexed when all tasks complete")

    # Keep is_indexed as False until all tasks complete
    document.is_indexed = False
    document.save()

    return {
        "status": "processing",
        "total_parts": total_parts,
        "task_group_id": job.id,
        "embedding_model": "cohere-embed-v4.0",
        "vector_dimensions": COHERE_VECTOR_SIZE,
    }


def get_embedding_progress(document_id: str) -> Dict[str, Any]:
    """
    Check the progress of embedding creation for a document.

    Args:
        document_id: ID of the document

    Returns:
        Dict with progress information
    """
    document = EmbeddedDocument.objects(id=document_id).first()
    if not document:
        return {"error": "Document not found"}

    parts = EmbeddedPart.objects(document_id=str(document_id))
    total_parts = parts.count()

    # If already indexed, no need to check Qdrant
    if document.is_indexed:
        return {
            "document_id": document_id,
            "total_parts": total_parts,
            "completed_parts": total_parts,
            "progress_percentage": 100.0,
            "is_indexed": True,
            "status": "completed",
        }

    # Count this document's points inside the shared per-tenant collection.
    collection_name = docs_collection_name(document.tenant_name)

    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client = get_qdrant_client()
        count_result = client.count(
            collection_name=collection_name,
            count_filter=Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=str(document_id)))]),
            exact=True,
        )
        completed_parts = count_result.count

    except Exception as e:
        # If collection doesn't exist yet, 0 embeddings
        logger.error(f"Collection not ready yet: {e}")
        completed_parts = 0

    progress_percentage = (completed_parts / total_parts * 100) if total_parts > 0 else 0

    return {
        "document_id": document_id,
        "total_parts": total_parts,
        "completed_parts": completed_parts,
        "progress_percentage": round(progress_percentage, 2),
        "is_indexed": document.is_indexed,
        "status": "completed" if completed_parts == total_parts else "processing",
    }
