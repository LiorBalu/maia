"""
Decorator for Celery tasks to automatically set logging context
"""

from functools import wraps
import inspect
from maia.observability.context_logging import (
    bind_tenant_name_to_log_context,
    bind_phone_number_to_log_context,
    bind_user_id_to_log_context,
    clear_log_context,
)


def _set_context_from_call_arguments(task_function, positional_arguments, keyword_arguments) -> None:
    """Copy the tenant_name / phone_number / user_id arguments of a call into the log context.

    The call is bound to task_function's signature (defaults applied), so positional and keyword
    arguments both count; a call that does not fit the signature raises TypeError.
    """
    # Get function signature
    task_signature = inspect.signature(task_function)
    bound_arguments = task_signature.bind(*positional_arguments, **keyword_arguments)
    bound_arguments.apply_defaults()

    # Extract context values from arguments
    if "tenant_name" in bound_arguments.arguments:
        bind_tenant_name_to_log_context(bound_arguments.arguments["tenant_name"])

    if "phone_number" in bound_arguments.arguments:
        bind_phone_number_to_log_context(bound_arguments.arguments["phone_number"])

    if "user_id" in bound_arguments.arguments:
        bind_user_id_to_log_context(bound_arguments.arguments["user_id"])


def bind_log_context_from_arguments(task_function):
    """
    Decorator for Celery/ARQ tasks that automatically sets logging context
    from task arguments (tenant_name, phone_number, user_id)

    Usage:
        @shared_task
        @bind_log_context_from_arguments
        def my_task(tenant_name: str, phone_number: str):
            logger.info("This will include [tenant:xxx] [phone:yyy]")
            ...
    """

    @wraps(task_function)
    async def async_task_with_context(*args, **kwargs):
        # Reset context at the start
        """Async variant: set the context from the bound arguments, run the task, always reset."""
        clear_log_context()

        try:
            _set_context_from_call_arguments(task_function, args, kwargs)

            # Execute the function
            return await task_function(*args, **kwargs)

        finally:
            # Clean up context after task
            clear_log_context()

    @wraps(task_function)
    def sync_task_with_context(*args, **kwargs):
        # Reset context at the start
        """Sync variant: set the context from the bound arguments, run the task, always reset."""
        clear_log_context()

        try:
            _set_context_from_call_arguments(task_function, args, kwargs)

            # Execute the function
            return task_function(*args, **kwargs)

        finally:
            # Clean up context after task
            clear_log_context()

    # Return the appropriate wrapper based on whether the function is async
    if inspect.iscoroutinefunction(task_function):
        return async_task_with_context
    else:
        return sync_task_with_context
