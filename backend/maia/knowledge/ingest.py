"""Knowledge ingestion: load a file or URL into text, split it into parts, embed them with Cohere and upsert them into the tenant's Qdrant collection (Celery runs this)."""


import logging
from pathlib import Path
from typing import Any, Dict

# LangChain 1.0 imports - functions moved to langchain_classic
from langchain_community.document_loaders import (
    TextLoader,
    UnstructuredFileLoader,
    UnstructuredPowerPointLoader,
)
from langchain_core.documents import Document as LangchainDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client.models import FieldCondition, Filter, MatchValue

from maia.models.knowledge_documents import EmbeddedDocument, EmbeddedPart
from maia.knowledge.qdrant import (
    COHERE_EMBEDDING_DIMENSIONS,
    derive_document_part_point_id,
    derive_tenant_documents_collection_name,
    ensure_tenant_documents_collection,
    ensure_qdrant_client,
)
from maia.utils.read_csv import extract_text_from_csv
from maia.utils.read_pdf import extract_text_from_pdf
from maia.utils.read_word import extract_text_from_word
from maia.utils.read_xlsx import extract_text_from_xlsx

logger = logging.getLogger(__name__)


def _load_text_with_loader(loader) -> str:
    """Run a LangChain document loader and join the page_content of the documents it returns with a blank line."""
    loaded_documents = loader.load()
    return "\n\n".join([loaded_document.page_content for loaded_document in loaded_documents])


def _load_web_page_text(url: str) -> str | None:
    """
    The text of a web page loaded with LangChain's WebBaseLoader ("" is a valid result).

    Returns None, after logging the error, when WebBaseLoader fails: the caller then downloads the URL as a file.
    """
    from langchain_community.document_loaders import WebBaseLoader

    try:
        logger.info(f"Loading web page: {url}")
        web_page_loader = WebBaseLoader(url)
        page_text = _load_text_with_loader(web_page_loader)
        logger.info(f"Successfully loaded {len(page_text)} characters from web page")
        return page_text
    except Exception as error:
        logger.error(f"WebBaseLoader failed, trying standard download: {error}")
        return None


def _download_url_to_temp_file(url: str, file_extension: str) -> str:
    """
    Download the URL into a named temporary file ending with file_extension and return the file's path.

    The file is created with delete=False and is never removed.
    """
    import tempfile

    import requests

    # Download file from URL
    download_response = requests.get(url)
    download_response.raise_for_status()

    # Save to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
        temp_file.write(download_response.content)
        local_path = temp_file.name
    return local_path


def _extract_text_by_extension(source_path_or_url: str, local_path: str) -> str:
    """
    Extract the text with the reader or LangChain loader that matches the suffix of local_path.

    The PDF, Word, Excel and CSV readers receive source_path_or_url (they download a URL themselves); the
    LangChain loaders read local_path. A legacy .doc raises ValueError; every error is logged and re-raised.
    """
    # Get file extension
    file_extension = Path(local_path).suffix.lower()

    try:
        # Choose loader based on file extension
        if file_extension == ".pdf":
            return extract_text_from_pdf(source_path_or_url)
        elif file_extension == ".docx":
            return extract_text_from_word(source_path_or_url)
        elif file_extension == ".doc":
            # python-docx reads only the Office Open XML format; the dashboard no longer offers .doc.
            raise ValueError("Legacy .doc files are not supported; save the file as .docx")
        elif file_extension in [".xlsx", ".xls"]:
            return extract_text_from_xlsx(source_path_or_url)
        elif file_extension == ".csv":
            return extract_text_from_csv(source_path_or_url)
        elif file_extension in [".pptx", ".ppt"]:
            document_loader = UnstructuredPowerPointLoader(local_path)
        elif file_extension in [".txt", ".md"]:
            document_loader = TextLoader(local_path)
        elif file_extension in [".html", ".htm"]:
            # For local HTML files
            document_loader = TextLoader(local_path)
        else:
            # Fallback: Universal loader for any file type
            logger.info(f"Using universal loader for {file_extension}")
            document_loader = UnstructuredFileLoader(local_path)

        # Load and extract text
        return _load_text_with_loader(document_loader)

    except Exception as error:
        logger.error(f"Error loading document with Langchain: {error}")
        raise


def extract_text_from_path_or_url(file_path_or_url: str) -> str:
    """
    Load document content by file extension or URL type.
    Supports: PDF, DOCX, TXT, XLSX, XLS, CSV (the dashboard's upload types), PPTX, HTML (web pages), and more.

    Args:
        file_path_or_url: Path or URL to the document

    Returns:
        Extracted text content from the document
    """
    # Check if it's a web URL (HTML page)
    if file_path_or_url.startswith(("http://", "https://")):
        # Get file extension from URL
        file_extension = Path(file_path_or_url).suffix.lower()

        # If no file extension or common web extensions, treat as HTML
        if not file_extension or file_extension in [".html", ".htm", ".php", ".asp", ".aspx", ""]:
            page_text = _load_web_page_text(file_path_or_url)
            if page_text is not None:
                return page_text
            # Fall through to download as file

        local_path = _download_url_to_temp_file(file_path_or_url, file_extension)
    else:
        local_path = file_path_or_url

    return _extract_text_by_extension(file_path_or_url, local_path)


def _delete_existing_parts(knowledge_document_id: str) -> None:
    """Delete the EmbeddedPart records a previous indexing left for this knowledge document, logging how many."""
    stale_parts_count = EmbeddedPart.objects(document_id=str(knowledge_document_id)).count()
    if stale_parts_count > 0:
        logger.info(f" Deleting {stale_parts_count} existing parts...")
        EmbeddedPart.objects(document_id=str(knowledge_document_id)).delete()


def _resolve_document_text(knowledge_doc: EmbeddedDocument) -> str | None:
    """
    The text to index: the document's stored content first, else the text loaded from its URL.

    Returns None when there is nothing to index (loading the URL failed, or the document has neither content nor
    URL); an empty text loaded from the URL is returned as is.
    """
    # Load document content
    if knowledge_doc.content:
        # Prefer existing content (e.g., from web scraping)
        logger.info(f"Using existing content ({len(knowledge_doc.content)} characters)")
        return knowledge_doc.content
    elif knowledge_doc.url:
        # Load from URL using Langchain (supports multiple formats)
        logger.info(f"Loading document from URL: {knowledge_doc.url}")
        try:
            return extract_text_from_path_or_url(knowledge_doc.url)
        except Exception as error:
            logger.error(f"Error loading with Langchain: {error}")
            return None
    else:
        logger.info(f"No content or URL found for document {knowledge_doc.id}")
        return None


def _split_text_into_chunks(knowledge_doc: EmbeddedDocument, document_text: str) -> list[LangchainDocument]:
    """
    Wrap the text in a LangChain document (metadata: document_id, tenant_name, title)
    and split it into chunks of up to 1000 characters with a 200-character overlap.
    """
    # Create a Langchain Document
    source_document = LangchainDocument(
        page_content=document_text,
        metadata={
            "document_id": str(knowledge_doc.id),
            "tenant_name": knowledge_doc.tenant_name,
            "title": knowledge_doc.title,
        },
    )

    # Split into chunks
    chunk_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = chunk_splitter.split_documents([source_document])

    logger.info(f"Document split into {len(chunks)} chunks")
    return chunks


def _reset_document_for_reindex(knowledge_doc: EmbeddedDocument, part_count: int) -> None:
    """Clear the document's stored content, mark it not indexed, record its new part count and save it."""
    # Update document metadata (without parts to avoid size limit)
    knowledge_doc.content = ""  # Clear content to save space
    knowledge_doc.is_indexed = False
    knowledge_doc.total_parts = part_count  # Store count for reference
    knowledge_doc.save()


def _persist_chunks_as_parts(knowledge_doc: EmbeddedDocument, chunks: list[LangchainDocument]) -> None:
    """Save one EmbeddedPart per chunk, numbered from 0, with the part's deterministic Qdrant point id as vector_id."""
    # Save each part individually to avoid document size limit
    logger.info(f"Saving {len(chunks)} parts to MongoDB...")
    for part_index, chunk in enumerate(chunks):
        point_id = derive_document_part_point_id(str(knowledge_doc.id), part_index)

        part_record = EmbeddedPart(
            part_number=part_index,
            content=chunk.page_content,
            vector_id=point_id,
            vector_for_backup=None,
            document_id=str(knowledge_doc.id),
            tenant_name=knowledge_doc.tenant_name,
        )
        part_record.save()  # Save each part independently
        logger.info(f"  Part {part_index + 1}/{len(chunks)} saved")


def index_knowledge_document(knowledge_document_id: str):
    """
    Split document into parts and create embeddings using Cohere.
    """
    knowledge_doc = EmbeddedDocument.objects(id=knowledge_document_id).first()
    if not knowledge_doc:
        logger.warning(f"Document {knowledge_document_id} not found")
        return

    # Delete existing parts for this document
    _delete_existing_parts(knowledge_document_id)

    document_text = _resolve_document_text(knowledge_doc)
    if document_text is None:
        return

    chunks = _split_text_into_chunks(knowledge_doc, document_text)
    _reset_document_for_reindex(knowledge_doc, len(chunks))
    _persist_chunks_as_parts(knowledge_doc, chunks)

    logger.info(f"Document {knowledge_document_id} split into {len(chunks)} parts and saved to MongoDB")
    logger.info("Now creating Cohere embeddings...")

    schedule_part_indexing_tasks(knowledge_document_id)


def _build_document_points_filter(knowledge_document_id: str) -> Filter:
    """The Qdrant filter that selects the points of one knowledge document (payload key document_id)."""
    return Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=knowledge_document_id))])


def _clear_document_points(qdrant_client, collection_name: str, knowledge_document_id: str) -> None:
    """Delete this document's points from the tenant's shared collection; a failure is logged, not raised."""
    # Re-indexing a document must be clean: drop only THIS document's existing
    # points (deterministic ids are overwritten on upsert anyway, but a re-split
    # can change the part count, leaving stale tail parts otherwise).
    try:
        qdrant_client.delete(
            collection_name=collection_name,
            points_selector=_build_document_points_filter(knowledge_document_id),
        )
    except Exception as error:
        logger.error(f"Could not clear previous points for document {knowledge_document_id}: {error}")


def _dispatch_embedding_chord(knowledge_doc: EmbeddedDocument, part_records, collection_name: str) -> tuple[int, Any]:
    """
    Schedule one Celery embedding task per part as a chord whose callback marks the document indexed once every
    part is done.

    Returns:
        (part_count, chord_result): the number of parts (counted once here) and the chord's AsyncResult
    """
    # Import Celery tasks
    from celery import chord

    from background_tasks import index_document_part_task, mark_knowledge_document_indexed_task

    # Prepare task arguments
    part_count = part_records.count()
    logger.info(f"Creating {part_count} Cohere embedding tasks...")

    # Create tasks for each part
    embed_signatures = []
    for part_record in part_records:
        embed_signature = index_document_part_task.s(
            knowledge_document_id=str(knowledge_doc.id),
            embedded_part_id=str(part_record.id),
            part_number=part_record.part_number,
            part_text=part_record.content,
            tenant_name=knowledge_doc.tenant_name,
            collection_name=collection_name,
        )
        embed_signatures.append(embed_signature)

    # Execute all tasks in parallel with a callback when all complete
    # Use chord: when all tasks finish, call the callback
    finalize_signature = mark_knowledge_document_indexed_task.s(str(knowledge_doc.id))
    chord_result = chord(embed_signatures)(finalize_signature)
    return part_count, chord_result


def schedule_part_indexing_tasks(knowledge_document_id: str):
    """
    Create Cohere embeddings for all parts of a document using Celery tasks for parallel processing.
    Uses Cohere embed-v4.0 with int8 quantization.
    """
    knowledge_doc = EmbeddedDocument.objects(id=knowledge_document_id).first()
    if not knowledge_doc:
        logger.warning(f"Document {knowledge_document_id} not found")
        return

    part_records = EmbeddedPart.objects(document_id=str(knowledge_document_id)).order_by("part_number")

    if not part_records:
        logger.warning(f"No parts found for document {knowledge_document_id}. Run index_knowledge_document first.")
        return

    # Use centralized Qdrant client
    qdrant_client = ensure_qdrant_client()

    # Shared per-tenant collection. Create it (with payload indexes) if missing;
    # never drop it — it holds every other document in the tenant.
    collection_name = ensure_tenant_documents_collection(qdrant_client, knowledge_doc.tenant_name)

    _clear_document_points(qdrant_client, collection_name, str(knowledge_doc.id))

    part_count, chord_result = _dispatch_embedding_chord(knowledge_doc, part_records, collection_name)

    logger.info(f"{part_count} Cohere embedding tasks scheduled. Processing in background...")
    logger.info("Document will be marked as indexed when all tasks complete")

    # Keep is_indexed as False until all tasks complete
    knowledge_doc.is_indexed = False
    knowledge_doc.save()

    return {
        "status": "processing",
        "total_parts": part_count,
        "task_group_id": chord_result.id,
        "embedding_model": "cohere-embed-v4.0",
        "vector_dimensions": COHERE_EMBEDDING_DIMENSIONS,
    }


def _count_embedded_points(collection_name: str, knowledge_document_id: str) -> int:
    """
    The exact number of this document's points in the tenant's shared collection; 0, after logging the error, when
    the count fails (e.g. the collection does not exist yet).
    """
    try:
        qdrant_client = ensure_qdrant_client()
        point_count_result = qdrant_client.count(
            collection_name=collection_name,
            count_filter=_build_document_points_filter(str(knowledge_document_id)),
            exact=True,
        )
        return point_count_result.count

    except Exception as error:
        # If collection doesn't exist yet, 0 embeddings
        logger.error(f"Collection not ready yet: {error}")
        return 0


def compute_indexing_progress(knowledge_document_id: str) -> Dict[str, Any]:
    """
    Check the progress of embedding creation for a document.

    Args:
        knowledge_document_id: ID of the document

    Returns:
        Dict with progress information
    """
    knowledge_doc = EmbeddedDocument.objects(id=knowledge_document_id).first()
    if not knowledge_doc:
        return {"error": "Document not found"}

    part_records = EmbeddedPart.objects(document_id=str(knowledge_document_id))
    part_count = part_records.count()

    # If already indexed, no need to check Qdrant
    if knowledge_doc.is_indexed:
        return {
            "document_id": knowledge_document_id,
            "total_parts": part_count,
            "completed_parts": part_count,
            "progress_percentage": 100.0,
            "is_indexed": True,
            "status": "completed",
        }

    # Count this document's points inside the shared per-tenant collection.
    collection_name = derive_tenant_documents_collection_name(knowledge_doc.tenant_name)
    embedded_part_count = _count_embedded_points(collection_name, knowledge_document_id)

    progress_percent = (embedded_part_count / part_count * 100) if part_count > 0 else 0

    return {
        "document_id": knowledge_document_id,
        "total_parts": part_count,
        "completed_parts": embedded_part_count,
        "progress_percentage": round(progress_percent, 2),
        "is_indexed": knowledge_doc.is_indexed,
        "status": "completed" if embedded_part_count == part_count else "processing",
    }
