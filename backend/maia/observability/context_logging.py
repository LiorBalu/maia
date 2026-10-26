"""
Centralized contextual logging system that automatically adds tenant_name and user_id to all logs.

Context values are injected as proper LogRecord attributes (not into the message string), so:
    - Console (dev): formatter displays [tenant_name:xxx] [user_id:yyy] — same field names as JSON
    - Loki/Grafana (prod): JSON formatter exposes them as real fields → filterable labels, no regex needed

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
context_tenant_name: ContextVar[Optional[str]] = ContextVar("tenant_name", default=None)
context_phone_number: ContextVar[Optional[str]] = ContextVar("phone_number", default=None)
context_user_id: ContextVar[Optional[str]] = ContextVar("user_id", default=None)


def set_context_tenant_name(tenant_name: str):
    context_tenant_name.set(tenant_name)


def set_context_phone_number(phone_number: str):
    context_phone_number.set(phone_number)


def set_context_user_id(user_id: str):
    context_user_id.set(user_id)


def get_context_info() -> str:
    """Returns a formatted string for use in print() and other non-logger outputs."""
    parts = []
    tenant_name = context_tenant_name.get()
    if tenant_name:
        parts.append(f"[tenant_name:{tenant_name}]")
    # user_id falls back to phone_number — always displayed under the user_id key
    user_id = context_user_id.get() or context_phone_number.get()
    if user_id:
        parts.append(f"[user_id:{user_id}]")
    return " ".join(parts) if parts else ""


class ContextualFilter(logging.Filter):
    """
    Injects context values as proper attributes on every LogRecord.

    Attributes set on every record:
      - record.tenant_name: the current tenant
      - record.user_id:    explicit user_id if set, otherwise falls back to phone_number.
                           The app always uses user_id as the primary user identifier.
      - record.phone_number: raw phone_number value, also exposed as its own field.

    JSON formatter (Loki) exposes them as real label fields:
        {"tenant_name": "my-business", "user_id": "972...", "message": "..."}
      → Loki query: {tenant_name="my-business"} — no regex needed
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.tenant_name = context_tenant_name.get() or ""
        # user_id = explicit user_id OR fallback to phone_number
        record.user_id = context_user_id.get() or context_phone_number.get() or ""
        # phone_number kept as a separate attribute for Loki queries that reference it directly
        record.phone_number = context_phone_number.get() or ""
        return True


class ContextualConsoleFormatter(logging.Formatter):
    """
    Human-readable formatter for development console.
    Reads the context attributes injected by ContextualFilter and prepends them to the message.

    Output: 2026-02-18 10:30:45 - module - INFO - [file:foo.py] [func:bar] [tenant_name:my-business] [user_id:972...] message

    phone_number is not displayed separately — it is already merged into user_id.
    """

    def format(self, record: logging.LogRecord) -> str:
        parts = []
        if record.filename:
            parts.append(f"[file:{record.filename}]")
        if record.funcName:
            parts.append(f"[func:{record.funcName}]")
        if getattr(record, "tenant_name", ""):
            parts.append(f"[tenant_name:{record.tenant_name}]")
        if getattr(record, "user_id", ""):
            parts.append(f"[user_id:{record.user_id}]")

        if parts:
            # Prepend context prefix to the message — does not mutate record.msg
            original_msg = record.getMessage()
            record.msg = f"{' '.join(parts)} {original_msg}"
            record.args = None  # args already consumed above

        return super().format(record)


# Store original print function
_original_print = builtins.print


def contextual_print(*args, **kwargs):
    """
    Replacement for print() that prepends context info when available.
    Works identically to the built-in print() in all other ways.
    """
    context_info = get_context_info()
    if context_info:
        args = (context_info,) + args
    _original_print(*args, **kwargs)


def setup_contextual_logging(level=logging.INFO):
    """
    Setup contextual logging for the entire application.
    Call once at startup (main.py, workers, tasks.py).

    - Attaches ContextualFilter to the root logger so every LogRecord gets
      tenant_name / phone_number / user_id as proper attributes.
    - Uses ContextualConsoleFormatter for human-readable dev output.
    - In production with Loki, swap the handler's formatter for a JSON formatter
      (e.g. python-json-logger) — the attributes will appear as proper JSON fields.
    - Replaces built-in print() with contextual_print().
    """
    context_filter = ContextualFilter()
    formatter = ContextualConsoleFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    # Filter on the handler so all handlers benefit automatically
    console_handler.addFilter(context_filter)

    root_logger.addHandler(console_handler)

    builtins.print = contextual_print

    return root_logger


def reset_context():
    """Clear all context variables. Call in finally blocks after each request/task."""
    context_tenant_name.set(None)
    context_phone_number.set(None)
    context_user_id.set(None)


def get_logger(name: str) -> logging.Logger:
    """
    Return a standard logger by name.

    The ContextualFilter attached to the root logger at startup (via
    setup_contextual_logging) injects tenant_name / user_id onto every
    LogRecord automatically, so child loggers obtained here benefit from
    contextual enrichment without any extra setup.

    Usage:
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)
