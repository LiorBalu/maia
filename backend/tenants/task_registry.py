"""Registry for customer-defined Celery tasks.

Customers declare tasks in ``tenants/<folder>/celery_tasks.py`` with the
``@tenant_task`` decorator. This module must NOT import ``tasks.py`` (the root
Celery app) — ``tasks.py`` imports the discovery, which imports the customer
modules, which import this file; pulling ``tasks`` back in would be circular.

The decorator only records the function; ``tenants/task_discovery.py`` is what
actually binds it to the Celery app and builds the beat entry.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class RegisteredTask:
    func: Callable
    folder: str = ""  # filled in by the discovery
    name: Optional[str] = None  # explicit Celery task name (overrides the generated one)
    cron: Optional[str] = None  # "minute hour day_of_month month day_of_week"
    every: Optional[float] = None  # interval in seconds
    beat_args: tuple = ()
    beat_kwargs: dict = field(default_factory=dict)
    tenant_name: Optional[str] = None  # contextual-logging tenant; defaults to the folder name
    queue: Optional[str] = None
    options: dict = field(default_factory=dict)  # passed through to celery_app.task()


TENANT_TASKS: list[RegisteredTask] = []


def tenant_task(
    name: Optional[str] = None,
    cron: Optional[str] = None,
    every: Optional[float] = None,
    beat_args: tuple = (),
    beat_kwargs: Optional[dict] = None,
    tenant_name: Optional[str] = None,
    queue: Optional[str] = None,
    **options: Any,
):
    """Mark a function as a customer Celery task.

    Args:
        name: explicit Celery task name. Defaults to ``customer.<folder>.<func>``.
        cron: crontab expression "min hour day_of_month month day_of_week"
            (celery timezone is Asia/Jerusalem). Adds a beat entry.
        every: interval in seconds. Adds a beat entry. Exclusive with ``cron``.
        beat_args / beat_kwargs: arguments beat passes on each scheduled run.
        tenant_name: tenant used for contextual logging. Defaults to the customer folder
            name; the decorator sets it before the call and always resets it after,
            so tasks never touch set_context_tenant_name/reset_context themselves.
        queue: route the task to a specific queue.
        options: forwarded to ``celery_app.task`` (bind, max_retries, rate_limit...).

    With neither ``cron`` nor ``every`` the task is on-demand only (no beat entry).
    """
    if cron and every:
        raise ValueError("tenant_task: 'cron' and 'every' are mutually exclusive")

    def decorator(func: Callable) -> Callable:
        TENANT_TASKS.append(
            RegisteredTask(
                func=func,
                name=name,
                cron=cron,
                every=every,
                beat_args=tuple(beat_args),
                beat_kwargs=beat_kwargs or {},
                tenant_name=tenant_name,
                queue=queue,
                options=options,
            )
        )
        return func

    return decorator
