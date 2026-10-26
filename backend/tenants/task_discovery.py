"""Auto-discovery of tenant Celery tasks (mirrors ``tenants/route_discovery.py``).

Imports every ``tenants/*/celery_tasks.py``, registers the functions decorated with
``@tenant_task`` on the Celery app, and returns the beat entries to merge into
``celery_app.conf.beat_schedule``.

Called once from ``tasks.py``, so both the worker and celery-beat run it and agree
on task names.
"""

import logging
import functools
import glob
import importlib
import os
from pathlib import Path

from celery.schedules import crontab

from tenants.task_registry import TENANT_TASKS

logger = logging.getLogger(__name__)


def _with_logging_context(func, tenant_name: str):
    """Bind the contextual-logging tenant around the task call.

    Done here rather than in each task body: contextvars leak to the next task on the
    same worker if a task forgets the reset, so the reset must be guaranteed.
    """
    from maia.observability.context_logging import reset_context, set_context_tenant_name

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        set_context_tenant_name(tenant_name)
        try:
            return func(*args, **kwargs)
        finally:
            reset_context()

    return wrapper


def _parse_cron(expr: str):
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"invalid cron expression {expr!r}: expected 5 fields")
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    return crontab(
        minute=minute, hour=hour, day_of_month=day_of_month, month_of_year=month_of_year, day_of_week=day_of_week
    )


def discover_tenant_tasks(celery_app) -> dict:
    """Register all tenant tasks on ``celery_app`` and return their beat schedule."""
    project_root = Path(__file__).parent.parent  # repository root
    task_files = sorted(glob.glob(str(project_root / "tenants" / "*" / "celery_tasks.py")))

    logger.info(f"Auto-discovering tenant celery tasks: Found {len(task_files)} task files")

    beat_schedule: dict = {}
    registered_count = 0

    for task_file in task_files:
        folder_name = Path(task_file).parent.name
        module_name = str(Path(task_file).relative_to(project_root).with_suffix("")).replace(os.sep, ".")
        already_registered = len(TENANT_TASKS)

        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            logger.error(f"  Failed to import {module_name}: {e}")
            continue

        # Only the entries this module just appended belong to this folder
        for reg in TENANT_TASKS[already_registered:]:
            try:
                reg.folder = folder_name
                task_name = reg.name or f"tenants.{folder_name}.{reg.func.__name__}"

                options = dict(reg.options)
                if reg.queue:
                    options["queue"] = reg.queue

                target = _with_logging_context(reg.func, reg.tenant_name or folder_name)
                celery_task = celery_app.task(name=task_name, **options)(target)
                # Rebind the module attribute so `from ...celery_tasks import x; x.delay()` works
                setattr(module, reg.func.__name__, celery_task)

                if reg.cron or reg.every:
                    beat_schedule[f"tenant_{folder_name}_{reg.func.__name__}"] = {
                        "task": task_name,
                        "schedule": _parse_cron(reg.cron) if reg.cron else reg.every,
                        "args": reg.beat_args,
                        "kwargs": reg.beat_kwargs,
                    }
                    logger.info(f"  Registered task '{task_name}' [beat: {reg.cron or f'every {reg.every}s'}]")
                else:
                    logger.info(f"  Registered task '{task_name}'")
                registered_count += 1
            except Exception as e:
                logger.error(f"  Failed to register {reg.func.__name__} from {module_name}: {e}")
                continue

    logger.info(f"Tenant task auto-discovery complete. Registered {registered_count} tasks.")
    return beat_schedule
