"""Celery application of Maia: knowledge indexing (document parts, URL scraping, Cohere embeddings), Gmail watch
renewal and the deferred WAHA disconnect check.
"""

import logging
from os import getenv

import redis
from celery import Celery
from celery.schedules import crontab


# Import contextual logging system
from maia.observability.context_logging import install_contextual_logging

# Setup contextual logging for Celery tasks
install_contextual_logging(log_level=logging.INFO)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# Configure Twilio logger to only show WARNING and ERROR
twilio_http_logger = logging.getLogger("twilio.http_client")
twilio_http_logger.setLevel(logging.WARNING)
httpx_client_logger = logging.getLogger("httpx")
httpx_client_logger.setLevel(logging.WARNING)
# Discover and register all decorated tools
try:
    from maia.pipeline.tools.function_decorator import register_all_tools

    register_all_tools()
    logger.info("Tools auto-discovery completed successfully")
except Exception as discovery_error:
    logger.error(f"Error during tools auto-discovery: {discovery_error}")


REDIS_URL = getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)

# Sync Redis client used by notify_if_waha_session_still_disconnected_task
sync_redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

celery_app.conf.timezone = "Asia/Jerusalem"

celery_app.conf.update(
    result_backend=REDIS_URL,
    task_track_started=True,
    result_extended=True,
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=REDIS_URL,
    redbeat_redis_options={},
    task_annotations={"background_tasks.index_document_part_task": {"rate_limit": "30/s"}},
    # Expire task results after 1 hour to prevent Redis memory saturation
    result_expires=3600,  # 1 hour in seconds
    # Don't store results for tasks that don't need them
    task_ignore_result=False,  # Keep results by default, but override per task
    # Reduce worker log verbosity
    worker_log_format="%(message)s",
    worker_task_log_format="[%(task_name)s] %(message)s",
    broker_transport_options={"visibility_timeout": 86400},  # 24h in seconds
    result_backend_transport_options={},
    # Prevent workers from hoarding ETA tasks in memory. Default (4) causes the
    # worker to prefetch 4×concurrency messages at once; with many ETA tasks in the
    # queue this fills the worker's in-memory scheduler and delays time-sensitive
    # tasks (the deferred WAHA session check) behind hundreds of queued ETA tasks.
    worker_prefetch_multiplier=1,
)


def _build_gmail_pubsub_topic_path(pubsub_project_id: str) -> str:
    """The Pub/Sub topic path Gmail watches publish to: projects/<project>/topics/email_inbound_<slug>, the slug being
    BACKEND_URL without its scheme with '.', '-' and ':' replaced by '_' (the topic register_gmail_watch creates in
    maia/api/email.py).
    """
    backend_base_url = str(getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    topic_domain_slug = (
        backend_base_url.replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
        .replace(".", "_")
        .replace("-", "_")
        .replace(":", "_")
    )
    pubsub_topic_name = f"email_inbound_{topic_domain_slug}"
    return f"projects/{pubsub_project_id}/topics/{pubsub_topic_name}"


def _renew_gmail_watch(gmail_integration, pubsub_topic_path: str) -> bool:
    """Renew the Gmail watch of one GoogleShared integration. True when renewed; False when its tokens are missing
    (warning) or the Gmail API call fails (error log). The import stays lazy (FastAPI modules stay out of the Celery
    import time).
    """
    from maia.api.email import build_gmail_client

    if not gmail_integration.access_token or not gmail_integration.refresh_token:
        logger.warning(
            f"[renew_gmail_watches] Skipping {gmail_integration.tenant_name} / {gmail_integration.email}: missing tokens"
        )
        return False
    try:
        gmail_service = build_gmail_client(gmail_integration.access_token, gmail_integration.refresh_token)
        gmail_service.users().watch(userId="me", body={"labelIds": ["INBOX"], "topicName": pubsub_topic_path}).execute()
        logger.info(f"[renew_gmail_watches] ✅ Renewed watch for {gmail_integration.tenant_name} / {gmail_integration.email}")
        return True
    except Exception as error:
        logger.error(f"[renew_gmail_watches] ❌ Failed for {gmail_integration.tenant_name} / {gmail_integration.email}: {error}")
        return False


@celery_app.task
def renew_enabled_gmail_watches_task():
    """
    Renew Gmail watch (pub/sub) for all email integrations registered in the DB.
    Gmail watches expire after ~7 days.
    This task is scheduled via crontab on fixed days of the month (every ~3 days)
    so restarts never affect the firing date.
    """
    from maia.models.google_accounts import GoogleShared
    from maia.api.email import PUBSUB_PROJECT_ID

    pubsub_topic_path = _build_gmail_pubsub_topic_path(PUBSUB_PROJECT_ID)

    gmail_integrations = list(GoogleShared.objects(gmail_watch_enabled=True))

    renewed_count = 0
    failed_count = 0
    for gmail_integration in gmail_integrations:
        if _renew_gmail_watch(gmail_integration, pubsub_topic_path):
            renewed_count += 1
        else:
            failed_count += 1

    logger.info(f"[renew_gmail_watches] Done — renewed: {renewed_count}, failed: {failed_count}")
    return {"renewed": renewed_count, "failed": failed_count}


@celery_app.task
def index_knowledge_document_task(knowledge_document_id: str):
    """Celery task: split a knowledge document into parts and schedule their embeddings (see
    maia/knowledge/ingest.py).
    """
    from maia.knowledge.ingest import index_knowledge_document

    index_knowledge_document(knowledge_document_id)


@celery_app.task
def scrape_and_index_url_task(
    knowledge_document_id: str, start_url: str, max_crawl_depth: int = 3, max_page_count: int = 500, should_stay_on_same_domain: bool = True
):
    """Celery task: scrape a website into a knowledge document, then index it."""
    from maia.knowledge.web_pages import scrape_url_and_schedule_indexing

    scrape_url_and_schedule_indexing(knowledge_document_id, start_url, max_crawl_depth, max_page_count, should_stay_on_same_domain)


celery_app.conf.beat_schedule = {
    "renew_gmail_watches_every_3_days": {
        "task": "background_tasks.renew_enabled_gmail_watches_task",
        # Fixed days of month every ~3 days — immune to worker restarts
        "schedule": crontab(hour=3, minute=0, day_of_month="1,4,7,10,13,16,19,22,25,28"),
    },
}


def _is_private_knowledge_document(knowledge_document_id: str) -> bool:
    """The is_private flag of the knowledge document, stored on every part for query-time filtering (False when the
    document does not exist).
    """
    from maia.models.knowledge_documents import EmbeddedDocument

    knowledge_document = EmbeddedDocument.objects(id=knowledge_document_id).first()
    return bool(knowledge_document.is_private) if knowledge_document else False


def _build_part_point(knowledge_document_id: str, part_number: int, part_text: str, tenant_name: str, part_vector: list[float]):
    """The Qdrant PointStruct of one document part: its deterministic id, the embedding vector and the payload
    (page_content, part_number, document_id, tenant_name, is_private). Loads the knowledge document for the private
    flag.
    """
    from qdrant_client.models import PointStruct

    from maia.knowledge.qdrant import derive_document_part_point_id

    # Document-level metadata drives query-time filtering in the shared
    # collection (private exclusion).
    is_private_document = _is_private_knowledge_document(knowledge_document_id)

    return PointStruct(
        id=derive_document_part_point_id(knowledge_document_id, part_number),
        vector=part_vector,
        payload={
            "page_content": part_text,
            "part_number": part_number,
            "document_id": knowledge_document_id,
            "tenant_name": tenant_name,
            "is_private": is_private_document,
        },
    )


def _persist_backup_vector(embedded_part_id: str, part_vector: list[float]) -> None:
    """Store the part's embedding in EmbeddedPart.vector_for_backup (the MongoDB backup of the Qdrant vector); nothing
    happens when the part does not exist.
    """
    from maia.models.knowledge_documents import EmbeddedPart

    embedded_part_doc = EmbeddedPart.objects(id=embedded_part_id).first()
    if embedded_part_doc:
        embedded_part_doc.vector_for_backup = part_vector
        embedded_part_doc.save()


@celery_app.task
def index_document_part_task(
    knowledge_document_id: str, embedded_part_id: str, part_number: int, part_text: str, tenant_name: str, collection_name: str
):
    """
    Celery task to create Cohere embedding for a single document part.

    Uses Cohere embed-v4.0 (1024 dimensions) for embedding generation.
    """
    from maia.knowledge.embeddings import CohereEmbeddingsV4
    from maia.knowledge.qdrant import ensure_qdrant_client

    try:
        # Create embedding with Cohere
        cohere_embedder = CohereEmbeddingsV4(input_type="search_document")
        part_vector = cohere_embedder.embed_query(part_text)

        # Connect to Qdrant using centralized helper
        qdrant_client = ensure_qdrant_client()

        # Upsert point with Cohere-compatible payload. Deterministic point id
        # so parts of different documents never collide in the shared collection.
        part_point = _build_part_point(knowledge_document_id, part_number, part_text, tenant_name, part_vector)
        qdrant_client.upsert(collection_name=collection_name, points=[part_point])

        # Update MongoDB with backup vector
        _persist_backup_vector(embedded_part_id, part_vector)

        logger.info(f"✅ Created Cohere embedding for part {part_number + 1}")
        return {"success": True, "part_number": part_number}

    except Exception as error:
        logger.error(f"❌ Error creating embedding for part {part_number}: {str(error)}", exc_info=True)
        return {"success": False, "error": str(error), "part_number": part_number}


@celery_app.task
def mark_knowledge_document_indexed_task(embedding_results, knowledge_document_id: str):
    """
    Callback task to mark document as indexed after all embedding tasks complete.

    Args:
        embedding_results: List of results from all embedding tasks (passed by chord)
        knowledge_document_id: ID of the document to mark as indexed
    """
    from maia.models.knowledge_documents import EmbeddedDocument

    try:
        # Log results summary
        successful_count = sum(1 for part_result in embedding_results if part_result.get("success", False))
        logger.info(f"📊 Embedding tasks completed: {successful_count}/{len(embedding_results)} successful")

        knowledge_document = EmbeddedDocument.objects(id=knowledge_document_id).first()
        if not knowledge_document:
            logger.error(f"Document {knowledge_document_id} not found")
            return {"success": False, "error": "Document not found"}

        # Mark as indexed
        knowledge_document.is_indexed = True
        knowledge_document.save()

        logger.info(f"✅ Document {knowledge_document_id} marked as fully indexed")
        return {"success": True, "document_id": knowledge_document_id}

    except Exception as error:
        logger.error(f"❌ Error marking document {knowledge_document_id} as indexed: {str(error)}", exc_info=True)
        return {"success": False, "error": str(error)}


def _fetch_waha_session_status(tenant_name: str, tenant_phone_number: str) -> str:
    """The status WAHA reports for the tenant's session ('WORKING', 'FAILED', 'UNKNOWN' when it reports none, ...).
    When WAHA cannot be reached or the session is gone, the warning is logged and 'FAILED' comes back (treated as
    not working).
    """
    from maia.services.waha import WahaService

    try:
        return WahaService(tenant_name, phone_number=tenant_phone_number).sync_session_status().get("status") or "UNKNOWN"
    except Exception as status_error:  # WAHA down or the session is gone: treat as not working
        logger.warning("notify_if_waha_session_still_disconnected_task: %s/%s status unavailable (%s)", tenant_name, tenant_phone_number, status_error)
        return "FAILED"


@celery_app.task
def notify_if_waha_session_still_disconnected_task(tenant_name: str, tenant_phone_number: str):
    """Deferred WAHA disconnect check, scheduled by ``maia/inbound/save/waha.py`` ten minutes after a
    session reported a non-working status. If the session is still not ``WORKING`` (or WAHA cannot be
    reached), the tenant's users are emailed once; ``waha_notif_sent:*`` keeps it to one mail per
    outage and ``waha_pending_check:*`` is the scheduling lock, both cleared when the session recovers.
    """
    from maia.inbound.save.waha import notify_tenant_users_of_session_status
    from maia.models.tenants import TenantDocument

    pending_check_key = f"waha_pending_check:{tenant_name}:{tenant_phone_number}"
    notification_sent_key = f"waha_notif_sent:{tenant_name}:{tenant_phone_number}"
    try:
        tenant_doc = TenantDocument.objects(name=tenant_name).first()
        if not tenant_doc or not tenant_doc.find_waha_account_by_phone_number(tenant_phone_number):
            logger.info("notify_if_waha_session_still_disconnected_task: %s/%s has no WAHA account, nothing to check", tenant_name, tenant_phone_number)
            return {"checked": False, "reason": "no such tenant or number"}
        session_status = _fetch_waha_session_status(tenant_name, tenant_phone_number)
        if session_status == "WORKING":
            sync_redis_client.delete(notification_sent_key)
            logger.info("notify_if_waha_session_still_disconnected_task: %s/%s recovered", tenant_name, tenant_phone_number)
            return {"checked": True, "status": session_status, "notified": False}
        should_notify = bool(sync_redis_client.set(notification_sent_key, "1", nx=True, ex=24 * 3600))
        if should_notify:
            notify_tenant_users_of_session_status(tenant_doc, tenant_name, tenant_phone_number, session_status)
            logger.warning("notify_if_waha_session_still_disconnected_task: %s/%s still %s after 10 minutes, users notified", tenant_name, tenant_phone_number, session_status)
        return {"checked": True, "status": session_status, "notified": should_notify}
    finally:
        sync_redis_client.delete(pending_check_key)
