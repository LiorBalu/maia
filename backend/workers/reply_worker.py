"""ARQ worker: the `run_reply_pipeline_job` that runs the reply pipeline for a saved message."""

import logging
import os
import resource
import time
from dataclasses import dataclass
from os import getenv
from typing import Any
from urllib.parse import urlparse

from arq.connections import RedisSettings

# Import contextual logging system
from maia.observability.context_logging import (
    clear_log_context,
    bind_tenant_name_to_log_context,
    bind_user_id_to_log_context,
    install_contextual_logging,
)
from maia.inbound.reply import run_reply_pipeline

# Setup contextual logging for worker
install_contextual_logging(log_level=logging.INFO)
logger = logging.getLogger(__name__)

# Discover and register all decorated tools
try:
    from maia.pipeline.tools.function_decorator import register_all_tools

    register_all_tools()
    logger.info("Tools auto-discovery completed successfully")
except Exception as discovery_error:
    logger.error(f"Error during tools auto-discovery: {discovery_error}")


def _measure_queue_wait_seconds(job_context: dict) -> float | None:
    """Seconds the job waited in the ARQ queue, from the context's ``enqueue_time``; None without one."""
    enqueued_at = job_context.get("enqueue_time")
    return (time.time() - enqueued_at.timestamp()) if enqueued_at else None


def _find_current_process() -> Any | None:
    """The worker's own ``psutil.Process``; None only when psutil is missing (``unstructured`` installs it)."""
    try:
        import psutil

        return psutil.Process(os.getpid())
    except ImportError:
        return None


def _measure_memory_mb(current_process: Any | None) -> float:
    """Memory in MB: the RSS of ``current_process`` (psutil), else ``ru_maxrss`` from ``resource.getrusage``."""
    if current_process:
        return current_process.memory_info().rss / (1024 * 1024)
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


@dataclass(frozen=True)
class _JobMetricsBaseline:
    """What is measured before the pipeline runs; ``_log_job_metrics`` compares it with the final values."""

    queue_wait_seconds: float | None
    wall_started_at: float
    cpu_usage_at_start: resource.struct_rusage
    current_process: Any | None
    memory_before_mb: float


def _collect_job_metrics_baseline(job_context: dict) -> _JobMetricsBaseline:
    """Measure the queue wait, then the wall clock, CPU usage and memory at the start of the job."""
    # --- Metrics: queue wait time ---
    queue_wait_seconds = _measure_queue_wait_seconds(job_context)

    # --- Metrics: CPU and memory baseline ---
    wall_started_at = time.monotonic()
    cpu_usage_at_start = resource.getrusage(resource.RUSAGE_SELF)
    current_process = _find_current_process()
    memory_before_mb = _measure_memory_mb(current_process)
    return _JobMetricsBaseline(
        queue_wait_seconds=queue_wait_seconds,
        wall_started_at=wall_started_at,
        cpu_usage_at_start=cpu_usage_at_start,
        current_process=current_process,
        memory_before_mb=memory_before_mb,
    )


def _classify_outcome(reply_text: str | None) -> str:
    """The job outcome for the logs: "ai_response" for a JSON reply or a custom button, else "skipped"."""
    if reply_text and (reply_text.startswith("{") or reply_text.startswith("Sent custom button")):
        return "ai_response"
    return "skipped"


def _log_job_metrics(
    job_context: dict, tenant_name: str, job_outcome: str, metrics_baseline: _JobMetricsBaseline
) -> None:
    """Take the final measurements and log the 'Task metrics' line of the job."""
    wall_elapsed_seconds = time.monotonic() - metrics_baseline.wall_started_at
    cpu_usage_at_start = metrics_baseline.cpu_usage_at_start
    cpu_usage_at_end = resource.getrusage(resource.RUSAGE_SELF)
    cpu_used_seconds = (cpu_usage_at_end.ru_utime - cpu_usage_at_start.ru_utime) + (
        cpu_usage_at_end.ru_stime - cpu_usage_at_start.ru_stime
    )

    try:
        memory_after_mb = _measure_memory_mb(metrics_baseline.current_process)
    except Exception:
        memory_after_mb = 0

    io_wait_seconds = wall_elapsed_seconds - cpu_used_seconds
    io_wait_percent = (io_wait_seconds / wall_elapsed_seconds * 100) if wall_elapsed_seconds > 0 else 0

    logger.info(
        "Task metrics | tenant_name=%s | outcome=%s | queue_wait=%.2fs | wall=%.2fs | cpu=%.3fs | "
        "await_time=%.2fs (%.0f%% I/O) | mem_before=%.1fMB | mem_after=%.1fMB | job_id=%s",
        tenant_name,
        job_outcome,
        metrics_baseline.queue_wait_seconds or 0,
        wall_elapsed_seconds,
        cpu_used_seconds,
        io_wait_seconds,
        io_wait_percent,
        metrics_baseline.memory_before_mb,
        memory_after_mb,
        job_context.get("job_id", "?"),
    )


async def run_reply_pipeline_job(
    job_context,
    tenant_name: str,
    sender_id: str,
    agent_config_override: dict = None,
    triggering_message_id: str = None,
    conversation_id: str = None,
) -> str:
    """
    ARQ task to generate user response.

    Args:
        job_context: ARQ context
        tenant_name: The name of the tenant/workspace
        sender_id: The user/sender ID
        agent_config_override: Optional agent configuration dictionary
        triggering_message_id: ID of the specific MessageDocument that triggered this task.
                         Used to save the correct message to Redis even on early returns (cooldown, block AI, etc.)
        conversation_id: Optional conversation ID. If provided, used directly instead of get_or_create.
    Returns:
        Response string from run_reply_pipeline
    """
    # Set context for this task (automatically adds to all logs and prints)
    bind_tenant_name_to_log_context(tenant_name)
    bind_user_id_to_log_context(sender_id)

    metrics_baseline = _collect_job_metrics_baseline(job_context)

    job_outcome = "unknown"
    try:
        logger.info(
            "Starting user response generation | tenant_name=%s | queue_wait=%.2fs",
            tenant_name,
            metrics_baseline.queue_wait_seconds or 0,
        )

        reply_text = await run_reply_pipeline(
            tenant_name=tenant_name,
            user_id=sender_id,
            agent_config_override=agent_config_override,
            triggering_message_id=triggering_message_id,
            conversation_id=conversation_id,
        )

        # Classify outcome for observability
        job_outcome = _classify_outcome(reply_text)
        if job_outcome == "skipped":
            logger.info("Task skipped | tenant_name=%s | reason=%s", tenant_name, reply_text)

        logger.info("Task completed | tenant_name=%s | outcome=%s", tenant_name, job_outcome)
        return reply_text

    except Exception as error:
        job_outcome = "error"
        logger.error("Task failed | tenant_name=%s | error=%s", tenant_name, str(error), exc_info=True)
        return f"Error: {str(error)}"

    finally:
        # --- Metrics: final measurements ---
        _log_job_metrics(job_context, tenant_name, job_outcome, metrics_baseline)

        # Clean up context after task
        clear_log_context()


def _build_worker_redis_settings() -> RedisSettings:
    """ARQ RedisSettings of the worker from REDIS_URL, with long connection timeouts (30 s, 3 retries 2 s apart)."""
    worker_redis_url = getenv("REDIS_URL", "redis://localhost:6379/0")

    parsed_redis_url = urlparse(worker_redis_url)
    arq_redis_settings_kwargs = dict(
        host=parsed_redis_url.hostname or "redis-stack",
        port=parsed_redis_url.port or 6379,
        database=(int(parsed_redis_url.path.strip("/")) if parsed_redis_url.path and parsed_redis_url.path.strip("/") else 0),
        password=parsed_redis_url.password,
        conn_timeout=30,
        conn_retries=3,
        conn_retry_delay=2,
    )
    return RedisSettings(**arq_redis_settings_kwargs)


class WorkerSettings:
    """
    Settings for the ARQ worker.
     https://arq-docs.helpmanual.io/
     https://github.com/samuelcolvin/arq
     https://arq-docs.helpmanual.io/#arq.worker.Worker
    """

    functions = [run_reply_pipeline_job]
    redis_settings = _build_worker_redis_settings()
    max_jobs = 50
    job_timeout = 600  # 10 minutes (default is 300s)
    health_check_interval = 30
    max_tries = 1
