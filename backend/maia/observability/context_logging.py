"""
Centralized contextual logging system that automatically adds tenant_name and user_id to all logs.

Context values are injected as proper LogRecord attributes (not into the message string), so:
    - Console (dev): formatter displays [tenant_name:xxx] [user_id:yyy] — same field names as JSON
    - JSON formatter (prod): exposes them as real fields → filterable labels, no regex needed

phone_number is a separate ContextVar (the middleware sets it) and is merged into user_id:
user_id = explicit_user_id or phone_number.
This means the app always identifies users by user_id regardless of which setter was called.
"""

import builtins
import logging
import sys
from contextvars import ContextVar
from typing import Optional

# Context variables — stored per async task / thread, never shared across requests
current_tenant_name: ContextVar[Optional[str]] = ContextVar("tenant_name", default=None)
current_phone_number: ContextVar[Optional[str]] = ContextVar("phone_number", default=None)
current_user_id: ContextVar[Optional[str]] = ContextVar("user_id", default=None)


def bind_tenant_name_to_log_context(tenant_name: str):
    """Set the tenant of the current task / request for the log context."""
    current_tenant_name.set(tenant_name)


def bind_phone_number_to_log_context(customer_phone_number: str):
    """Set the customer's phone number for the log context (shown as user_id)."""
    current_phone_number.set(customer_phone_number)


def bind_user_id_to_log_context(explicit_user_id: str):
    """Set the user id for the log context."""
    current_user_id.set(explicit_user_id)


def render_log_context_prefix() -> str:
    """Returns a formatted string for use in print() and other non-logger outputs."""
    prefix_tags = []
    bound_tenant_name = current_tenant_name.get()
    if bound_tenant_name:
        prefix_tags.append(f"[tenant_name:{bound_tenant_name}]")
    # user_id falls back to phone_number — always displayed under the user_id key
    effective_user_id = current_user_id.get() or current_phone_number.get()
    if effective_user_id:
        prefix_tags.append(f"[user_id:{effective_user_id}]")
    return " ".join(prefix_tags) if prefix_tags else ""


class ContextualFilter(logging.Filter):
    """
    Injects context values as proper attributes on every LogRecord.

    Attributes set on every record:
      - record.tenant_name: the current tenant
      - record.user_id:    explicit user_id if set, otherwise falls back to phone_number.
                           The app always uses user_id as the primary user identifier.
      - record.phone_number: raw phone_number value, also exposed as its own field.

    A JSON formatter exposes them as real fields:
        {"tenant_name": "my-business", "user_id": "972...", "message": "..."}
      → filter the logs by tenant_name="my-business", no regex needed
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach tenant_name, user_id and phone_number to the record; never drops it."""
        record.tenant_name = current_tenant_name.get() or ""
        # user_id = explicit user_id OR fallback to phone_number
        record.user_id = current_user_id.get() or current_phone_number.get() or ""
        # phone_number kept as a separate attribute so log filters can reference it directly
        record.phone_number = current_phone_number.get() or ""
        return True


def _collect_record_prefix_tags(record: logging.LogRecord) -> list[str]:
    """Return the record's [file] [func] [tenant_name] [user_id] tags in display order (empty when none apply).

    tenant_name / user_id are the attributes ContextualFilter set on the record; the record is only read.
    """
    prefix_tags = []
    if record.filename:
        prefix_tags.append(f"[file:{record.filename}]")
    if record.funcName:
        prefix_tags.append(f"[func:{record.funcName}]")
    if getattr(record, "tenant_name", ""):
        prefix_tags.append(f"[tenant_name:{record.tenant_name}]")
    if getattr(record, "user_id", ""):
        prefix_tags.append(f"[user_id:{record.user_id}]")
    return prefix_tags


class ContextualConsoleFormatter(logging.Formatter):
    """
    Human-readable formatter for development console.
    Reads the context attributes injected by ContextualFilter and prepends them to the message.

    Output: 2026-02-18 10:30:45 - module - INFO - [file:foo.py] [func:bar] [tenant_name:my-business] [user_id:972...] message

    phone_number is not displayed separately — it is already merged into user_id.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Prefix the message with [file] [func] [tenant_name] [user_id] before the standard formatting."""
        prefix_tags = _collect_record_prefix_tags(record)

        if prefix_tags:
            # Prepend context prefix to the message — mutates record.msg and record.args in place
            rendered_message = record.getMessage()
            record.msg = f"{' '.join(prefix_tags)} {rendered_message}"
            record.args = None  # args already consumed above

        return super().format(record)


# Store original print function
_builtin_print = builtins.print


def print_with_log_context(*objects, **print_options):
    """
    Replacement for print() that prepends context info when available.
    Works identically to the built-in print() in all other ways.
    """
    context_prefix = render_log_context_prefix()
    if context_prefix:
        objects = (context_prefix,) + objects
    _builtin_print(*objects, **print_options)


def _build_console_handler(log_level: int) -> logging.StreamHandler:
    """Build the stdout handler: ContextualConsoleFormatter layout plus a ContextualFilter of its own.

    The filter is attached to this handler, so it adds tenant_name / phone_number / user_id to every
    record the handler emits.
    """
    log_context_filter = ContextualFilter()
    console_formatter = ContextualConsoleFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.setFormatter(console_formatter)
    # Filter on this handler: it enriches the records this handler emits
    stdout_handler.addFilter(log_context_filter)
    return stdout_handler


def install_contextual_logging(log_level=logging.INFO):
    """
    Setup contextual logging for the entire application.
    Call once at startup (main.py, workers, background_tasks.py).

    - Attaches ContextualFilter to the root logger so every LogRecord gets
      tenant_name / phone_number / user_id as proper attributes.
    - Uses ContextualConsoleFormatter for human-readable dev output.
    - In production, swap the handler's formatter for a JSON formatter
      (e.g. python-json-logger) — the attributes will appear as proper JSON fields.
    - Replaces built-in print() with print_with_log_context().
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    root_logger.addHandler(_build_console_handler(log_level))

    builtins.print = print_with_log_context

    return root_logger


def clear_log_context():
    """Clear all context variables. Call in finally blocks after each request/task."""
    current_tenant_name.set(None)
    current_phone_number.set(None)
    current_user_id.set(None)


def resolve_logger(logger_name: str) -> logging.Logger:
    """
    Return a standard logger by name.

    The ContextualFilter attached to the root logger at startup (via
    install_contextual_logging) injects tenant_name / user_id onto every
    LogRecord automatically, so child loggers obtained here benefit from
    contextual enrichment without any extra setup.

    Usage:
        logger = resolve_logger(__name__)
    """
    return logging.getLogger(logger_name)
