"""
Decorator for Celery tasks to automatically set logging context
"""

from functools import wraps
import inspect
from maia.observability.context_logging import (
    set_context_tenant_name,
    set_context_phone_number,
    set_context_user_id,
    reset_context,
)


def contextual_task(func):
    """
    Decorator for Celery/ARQ tasks that automatically sets logging context
    from task arguments (tenant_name, phone_number, user_id)

    Usage:
        @shared_task
        @contextual_task
        def my_task(tenant_name: str, phone_number: str):
            logger.info("This will include [tenant:xxx] [phone:yyy]")
            ...
    """

    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        # Reset context at the start
        reset_context()

        try:
            # Get function signature
            sig = inspect.signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()

            # Extract context values from arguments
            if "tenant_name" in bound_args.arguments:
                set_context_tenant_name(bound_args.arguments["tenant_name"])

            if "phone_number" in bound_args.arguments:
                set_context_phone_number(bound_args.arguments["phone_number"])

            if "user_id" in bound_args.arguments:
                set_context_user_id(bound_args.arguments["user_id"])

            # Execute the function
            return await func(*args, **kwargs)

        finally:
            # Clean up context after task
            reset_context()

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        # Reset context at the start
        reset_context()

        try:
            # Get function signature
            sig = inspect.signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()

            # Extract context values from arguments
            if "tenant_name" in bound_args.arguments:
                set_context_tenant_name(bound_args.arguments["tenant_name"])

            if "phone_number" in bound_args.arguments:
                set_context_phone_number(bound_args.arguments["phone_number"])

            if "user_id" in bound_args.arguments:
                set_context_user_id(bound_args.arguments["user_id"])

            # Execute the function
            return func(*args, **kwargs)

        finally:
            # Clean up context after task
            reset_context()

    # Return the appropriate wrapper based on whether the function is async
    if inspect.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper
