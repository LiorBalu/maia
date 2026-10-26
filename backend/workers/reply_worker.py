"""ARQ worker: the `reply_job` that runs the reply pipeline for a saved message."""

import logging
import os
import resource
import time
from os import getenv
from urllib.parse import urlparse

from arq.connections import RedisSettings

# Import contextual logging system
from maia.observability.context_logging import (
    reset_context,
    set_context_tenant_name,
    set_context_user_id,
    setup_contextual_logging,
)
from maia.inbound.reply import reply_to_message

# Setup contextual logging for worker
setup_contextual_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# Discover and register all decorated tools
try:
    from maia.pipeline.tools.function_decorator import discover_all_tools

    discover_all_tools()
    logger.info("Tools auto-discovery completed successfully")
except Exception as e:
    logger.error(f"Error during tools auto-discovery: {e}")


async def reply_job(
    ctx,
    tenant_name: str,
    user_id: str,
    agent_config: dict = None,
    task_message_id: str = None,
    single_message_mode: bool = False,
    conversation_id: str = None,
) -> str:
    """
    ARQ task to generate user response.

    Args:
        ctx: ARQ context
        tenant_name: The name of the tenant/workspace
        user_id: The user/sender ID
        agent_config: Optional agent configuration dictionary
        task_message_id: ID of the specific MessageDocument that triggered this task.
                         Used to save the correct message to Redis even on early returns (cooldown, block AI, etc.)
        single_message_mode: When True the Redis session_id is the message id, not the conversation id.
                             Set by save functions that process isolated messages (web pages, email).
        conversation_id: Optional conversation ID. If provided, used directly instead of get_or_create.
    Returns:
        Response string from reply_to_message
    """
    # Set context for this task (automatically adds to all logs and prints)
    set_context_tenant_name(tenant_name)
    set_context_user_id(user_id)

    # --- Metrics: queue wait time ---
    enqueue_time = ctx.get("enqueue_time")
    queue_wait_s = (time.time() - enqueue_time.timestamp()) if enqueue_time else None

    # --- Metrics: CPU and memory baseline ---
    wall_start = time.monotonic()
    cpu_start = resource.getrusage(resource.RUSAGE_SELF)
    process = None
    try:
        import psutil

        process = psutil.Process(os.getpid())
        mem_before_mb = process.memory_info().rss / (1024 * 1024)
    except ImportError:
        mem_before_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)

    outcome = "unknown"
    try:
        logger.info(
            "Starting user response generation | tenant_name=%s | queue_wait=%.2fs", tenant_name, queue_wait_s or 0
        )

        response = await reply_to_message(
            tenant_name=tenant_name,
            user_id=user_id,
            agent_config=agent_config,
            task_message_id=task_message_id,
            single_message_mode=single_message_mode,
            conversation_id=conversation_id,
        )

        # Classify outcome for observability
        if response and (response.startswith("{") or response.startswith("Sent custom button")):
            outcome = "ai_response"
        else:
            outcome = "skipped"
            logger.info("Task skipped | tenant_name=%s | reason=%s", tenant_name, response)

        logger.info("Task completed | tenant_name=%s | outcome=%s", tenant_name, outcome)
        return response

    except Exception as e:
        outcome = "error"
        logger.error("Task failed | tenant_name=%s | error=%s", tenant_name, str(e), exc_info=True)
        return f"Error: {str(e)}"

    finally:
        # --- Metrics: final measurements ---
        wall_elapsed = time.monotonic() - wall_start
        cpu_end = resource.getrusage(resource.RUSAGE_SELF)
        cpu_used_s = (cpu_end.ru_utime - cpu_start.ru_utime) + (cpu_end.ru_stime - cpu_start.ru_stime)

        try:
            if process:
                mem_after_mb = process.memory_info().rss / (1024 * 1024)
            else:
                mem_after_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
        except Exception:
            mem_after_mb = 0

        await_time = wall_elapsed - cpu_used_s
        io_pct = (await_time / wall_elapsed * 100) if wall_elapsed > 0 else 0

        logger.info(
            "Task metrics | tenant_name=%s | outcome=%s | queue_wait=%.2fs | wall=%.2fs | cpu=%.3fs | "
            "await_time=%.2fs (%.0f%% I/O) | mem_before=%.1fMB | mem_after=%.1fMB | job_id=%s",
            tenant_name,
            outcome,
            queue_wait_s or 0,
            wall_elapsed,
            cpu_used_s,
            await_time,
            io_pct,
            mem_before_mb,
            mem_after_mb,
            ctx.get("job_id", "?"),
        )

        # Clean up context after task
        reset_context()


REDIS_URL = getenv("REDIS_URL", "redis://localhost:6379/0")

parsed_url = urlparse(REDIS_URL)
redis_settings_kwargs = dict(
    host=parsed_url.hostname or "redis-stack",
    port=parsed_url.port or 6379,
    database=(int(parsed_url.path.strip("/")) if parsed_url.path and parsed_url.path.strip("/") else 0),
    password=parsed_url.password,
    conn_timeout=30,
    conn_retries=3,
    conn_retry_delay=2,
)


class WorkerSettings:
    """
    Settings for the ARQ worker.
     https://arq-docs.helpmanual.io/
     https://github.com/samuelcolvin/arq
     https://arq-docs.helpmanual.io/#arq.worker.Worker
    """

    functions = [reply_job]
    redis_settings = RedisSettings(**redis_settings_kwargs)
    max_jobs = 50
    job_timeout = 600  # 10 minutes (default is 300s)
    health_check_interval = 30
    max_tries = 1
