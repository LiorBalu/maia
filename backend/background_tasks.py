"""Celery application of Maia: knowledge indexing (document parts, URL scraping, Cohere embeddings), Gmail watch
renewal and the deferred WAHA disconnect check.
"""

import logging
from os import getenv

import redis
from celery import Celery
from celery.schedules import crontab


# Import contextual logging system
from maia.observability.context_logging import setup_contextual_logging

# Setup contextual logging for Celery tasks
setup_contextual_logging(level=logging.INFO)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# Configure Twilio logger to only show WARNING and ERROR
twilio_logger = logging.getLogger("twilio.http_client")
twilio_logger.setLevel(logging.WARNING)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
# Discover and register all decorated tools
try:
    from maia.pipeline.tools.function_decorator import discover_all_tools

    discover_all_tools()
    logger.info("Tools auto-discovery completed successfully")
except Exception as e:
    logger.error(f"Error during tools auto-discovery: {e}")


REDIS_URL = getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)

# Sync Redis client used by check_waha_session_task
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

celery_app.conf.timezone = "Asia/Jerusalem"

celery_app.conf.update(
    result_backend=REDIS_URL,
    task_track_started=True,
    result_extended=True,
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=REDIS_URL,
    redbeat_redis_options={},
    task_annotations={"background_tasks.embed_chunk_task": {"rate_limit": "30/s"}},
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


@celery_app.task
def renew_gmail_watches_task():
    """
    Renew Gmail watch (pub/sub) for all email integrations registered in the DB.
    Gmail watches expire after ~7 days.
    This task is scheduled via crontab on fixed days of the month (every ~3 days)
    so restarts never affect the firing date.
    """
    from maia.models.google_accounts import GoogleShared
    from maia.api.email import GCP_PROJECT_ID, get_email_service

    base_url_str = str(getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    domain = (
        base_url_str.replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
        .replace(".", "_")
        .replace("-", "_")
        .replace(":", "_")
    )
    topic_name = f"email_inbound_{domain}"
    email_topic_path = f"projects/{GCP_PROJECT_ID}/topics/{topic_name}"

    integrations = list(GoogleShared.objects(gmail_watch_enabled=True))

    renewed = 0
    failed = 0
    for integration in integrations:
        if not integration.access_token or not integration.refresh_token:
            logger.warning(
                f"[renew_gmail_watches] Skipping {integration.tenant_name} / {integration.email}: missing tokens"
            )
            failed += 1
            continue
        try:
            service = get_email_service(integration.access_token, integration.refresh_token)
            service.users().watch(userId="me", body={"labelIds": ["INBOX"], "topicName": email_topic_path}).execute()
            logger.info(f"[renew_gmail_watches] ✅ Renewed watch for {integration.tenant_name} / {integration.email}")
            renewed += 1
        except Exception as e:
            logger.error(f"[renew_gmail_watches] ❌ Failed for {integration.tenant_name} / {integration.email}: {e}")
            failed += 1

    logger.info(f"[renew_gmail_watches] Done — renewed: {renewed}, failed: {failed}")
    return {"renewed": renewed, "failed": failed}


@celery_app.task
def index_document_task(document_id: str):
    """Celery task: split a knowledge document into parts and schedule their embeddings (see
    maia/knowledge/ingest.py).
    """
    from maia.knowledge.ingest import create_embedding_parts_for_document

    create_embedding_parts_for_document(document_id)


@celery_app.task
def index_url_task(
    document_id: str, url: str, max_depth: int = 3, max_pages: int = 500, same_domain_only: bool = True
):
    """Celery task: scrape a website into a knowledge document, then index it."""
    from maia.knowledge.web_pages import scrape_url_and_embed

    scrape_url_and_embed(document_id, url, max_depth, max_pages, same_domain_only)


celery_app.conf.beat_schedule = {
    "renew_gmail_watches_every_3_days": {
        "task": "background_tasks.renew_gmail_watches_task",
        # Fixed days of month every ~3 days — immune to worker restarts
        "schedule": crontab(hour=3, minute=0, day_of_month="1,4,7,10,13,16,19,22,25,28"),
    },
}


@celery_app.task
def embed_chunk_task(
    document_id: str, part_id: str, part_number: int, content: str, tenant_name: str, collection_name: str
):
    """
    Celery task to create Cohere embedding for a single document part.

    Uses Cohere embed-v4.0 (1024 dimensions) for embedding generation.
    """
    from qdrant_client.models import PointStruct

    from maia.knowledge.embeddings import CohereEmbeddingsV4
    from maia.models.knowledge_documents import EmbeddedDocument, EmbeddedPart
    from maia.knowledge.qdrant import doc_point_id, get_qdrant_client

    try:
        # Create embedding with Cohere
        embeddings = CohereEmbeddingsV4(input_type="search_document")
        embedding_vector = embeddings.embed_query(content)

        # Connect to Qdrant using centralized helper
        client = get_qdrant_client()

        # Document-level metadata drives query-time filtering in the shared
        # collection (category filter / private exclusion).
        document = EmbeddedDocument.objects(id=document_id).first()
        categories = [c for c in (document.categories or []) if c] if document else []
        is_private = bool(document.is_private) if document else False

        # Upsert point with Cohere-compatible payload. Deterministic point id
        # so parts of different documents never collide in the shared collection.
        client.upsert(
            collection_name=collection_name,
            points=[
                PointStruct(
                    id=doc_point_id(document_id, part_number),
                    vector=embedding_vector,
                    payload={
                        "page_content": content,
                        "part_number": part_number,
                        "document_id": document_id,
                        "tenant_name": tenant_name,
                        "categories": categories,
                        "is_private": is_private,
                    },
                )
            ],
        )

        # Update MongoDB with backup vector
        embedded_part = EmbeddedPart.objects(id=part_id).first()
        if embedded_part:
            embedded_part.vector_for_backup = embedding_vector
            embedded_part.save()

        logger.info(f"✅ Created Cohere embedding for part {part_number + 1}")
        return {"success": True, "part_number": part_number}

    except Exception as e:
        logger.error(f"❌ Error creating embedding for part {part_number}: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e), "part_number": part_number}


@celery_app.task
def mark_document_indexed_task(results, document_id: str):
    """
    Callback task to mark document as indexed after all embedding tasks complete.

    Args:
        results: List of results from all embedding tasks (passed by chord)
        document_id: ID of the document to mark as indexed
    """
    from maia.models.knowledge_documents import EmbeddedDocument

    try:
        # Log results summary
        successful = sum(1 for r in results if r.get("success", False))
        logger.info(f"📊 Embedding tasks completed: {successful}/{len(results)} successful")

        document = EmbeddedDocument.objects(id=document_id).first()
        if not document:
            logger.error(f"Document {document_id} not found")
            return {"success": False, "error": "Document not found"}

        # Mark as indexed
        document.is_indexed = True
        document.save()

        logger.info(f"✅ Document {document_id} marked as fully indexed")
        return {"success": True, "document_id": document_id}

    except Exception as e:
        logger.error(f"❌ Error marking document {document_id} as indexed: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}


@celery_app.task
def check_waha_session_task(tenant_name: str, phone_number: str):
    """Deferred WAHA disconnect check, scheduled by ``maia/inbound/save/waha.py`` ten minutes after a
    session reported a non-working status. If the session is still not ``WORKING`` (or WAHA cannot be
    reached), the tenant's users are emailed once; ``waha_notif_sent:*`` keeps it to one mail per
    outage and ``waha_pending_check:*`` is the scheduling lock, both cleared when the session recovers.
    """
    from maia.inbound.save.waha import _send_session_status_notification
    from maia.models.tenants import TenantDocument
    from maia.services.waha import WahaService

    pending_key = f"waha_pending_check:{tenant_name}:{phone_number}"
    notif_key = f"waha_notif_sent:{tenant_name}:{phone_number}"
    try:
        tenant = TenantDocument.objects(name=tenant_name).first()
        if not tenant or not tenant.get_waha_by_phone_number(phone_number):
            logger.info("check_waha_session_task: %s/%s has no WAHA account, nothing to check", tenant_name, phone_number)
            return {"checked": False, "reason": "no such tenant or number"}
        try:
            status = WahaService(tenant_name, phone_number=phone_number).get_client_status().get("status") or "UNKNOWN"
        except Exception as exc:  # WAHA down or the session is gone: treat as not working
            logger.warning("check_waha_session_task: %s/%s status unavailable (%s)", tenant_name, phone_number, exc)
            status = "FAILED"
        if status == "WORKING":
            redis_client.delete(notif_key)
            logger.info("check_waha_session_task: %s/%s recovered", tenant_name, phone_number)
            return {"checked": True, "status": status, "notified": False}
        notified = bool(redis_client.set(notif_key, "1", nx=True, ex=24 * 3600))
        if notified:
            _send_session_status_notification(tenant, tenant_name, phone_number, status)
            logger.warning("check_waha_session_task: %s/%s still %s after 10 minutes, users notified", tenant_name, phone_number, status)
        return {"checked": True, "status": status, "notified": notified}
    finally:
        redis_client.delete(pending_key)
