"""FastAPI entry point of Maia: logging, CORS, the routers of every channel and of the management API, startup
discovery of tools, and the request-logging middleware.
"""

import logging
import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response as StarletteResponse

logger = logging.getLogger(__name__)

load_dotenv()

from fastapi.middleware.cors import CORSMiddleware
from mongoengine.errors import ValidationError as MongoEngineValidationError

from maia.voice.webhook import router as voice_webhook_router
from maia.inbound.routes import router as inbound_router
from maia.api.openapi_docs import router as docs_router
from maia.api.router import router as management_router
from maia.api.auth import router as auth_router

from maia.api.sse import sse_router
from maia.voice.media_stream import router as media_stream_router

from maia.observability.context_logging import install_contextual_logging

# Same console format as the workers: every line carries tenant_name / phone_number when known.
install_contextual_logging(log_level=logging.INFO)

twilio_http_logger = logging.getLogger("twilio.http_client")
twilio_http_logger.setLevel(logging.WARNING)

google_discovery_cache_logger = logging.getLogger("googleapiclient.discovery_cache")
google_discovery_cache_logger.setLevel(logging.ERROR)
httpx_client_logger = logging.getLogger("httpx")
httpx_client_logger.setLevel(logging.WARNING)


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


allowed_cors_origins = [os.environ.get("FRONTEND_URL", "http://localhost:5173"), "http://127.0.0.1:5173", "*"]

app.add_middleware(
    CORSMiddleware, allow_origins=allowed_cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)


# Routers
app.include_router(voice_webhook_router, prefix="/api/v1", tags=["Phone (Twilio Voice)"])
app.include_router(inbound_router, prefix="/api/async", tags=["Inbound messages (all platforms)"])
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(media_stream_router, prefix="/api", tags=["Call Stream"])
app.include_router(management_router, prefix="/management/v1")
app.include_router(sse_router, prefix="/management/v1", tags=["SSE"])
app.include_router(docs_router, prefix="")


@app.get("/")
async def fetch_liveness_message():
    """Liveness message at the root URL."""
    return {"message": "The Maia API is running"}


@app.get("/health")
async def fetch_health_status():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": time.time()}


def _register_llm_tools():
    """Discover and register every ``@llm_tool`` function; a failure is logged, never raised."""
    # Discover and register all decorated tools
    try:
        from maia.pipeline.tools.function_decorator import register_all_tools

        register_all_tools()
        logger.info("Tools auto-discovery completed successfully")
    except Exception as discovery_error:
        logger.error(f"Error during tools auto-discovery: {discovery_error}")


async def _start_sse_broadcast_listener_or_log_failure():
    """Subscribe the SSE manager to the Redis broadcast channel; a failure is logged, never raised."""
    # Subscribe the SSE manager to the Redis broadcast channel (live inbox updates)
    try:
        from maia.api.sse import start_sse_broadcast_listener

        await start_sse_broadcast_listener()
    except Exception as sse_error:
        logger.error(f"Error initializing the SSE manager: {sse_error}")


@app.on_event("startup")
async def bootstrap_app_on_startup():
    """Startup: register the @llm_tool functions and connect the SSE manager to Redis."""
    logger.info("Starting up application...")
    _register_llm_tools()
    await _start_sse_broadcast_listener_or_log_failure()


@app.on_event("shutdown")
async def tear_down_app_on_shutdown():
    """Shutdown: stop the SSE manager and close its connections."""
    logger.info("Shutting down application...")

    try:
        from maia.api.sse import stop_sse_broadcast_listener

        await stop_sse_broadcast_listener()
    except Exception as sse_error:
        logger.error(f"Error shutting down the SSE manager: {sse_error}")

    logger.info("Application shutdown complete")


# Paths to skip for error logging (health-check probes; browsers ask for a favicon the backend does not serve)
_PATHS_EXEMPT_FROM_ERROR_LOGGING = frozenset({"/health", "/favicon.ico"})

# Max body bytes to log
_MAX_LOGGED_BODY_BYTES = 10_000


def _decode_body_for_logging(body_bytes: bytes, max_bytes: int = _MAX_LOGGED_BODY_BYTES) -> str:
    """Decode bytes to a truncated UTF-8 string, safe for logging."""
    if not body_bytes:
        return ""
    try:
        return body_bytes[:max_bytes].decode("utf-8", errors="replace")
    except Exception:
        return "<binary data>"


async def _read_request_body_for_logging(http_request: Request) -> bytes:
    """The body of a POST / PUT / PATCH / DELETE request, read now so it can be logged on an error response; ``b""``
    for the other methods or when reading fails.
    """
    raw_request_body = b""
    if http_request.method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            raw_request_body = await http_request.body()
        except Exception:
            pass
    return raw_request_body


def _format_elapsed_since(started_at: float) -> str:
    """Seconds elapsed since ``started_at`` (a ``time.time()`` value), with 6 decimals (e.g. ``"0.012345"``)."""
    elapsed_seconds = time.time() - started_at
    return f"{elapsed_seconds:.6f}"


def _log_metrics_line(http_request: Request, status_code: int, elapsed_text: str, log_level: int = logging.INFO):
    """Log the one-line ``HTTP metrics`` summary of a request (status, method, path, duration) at ``log_level``."""
    logger.log(log_level, f"HTTP metrics | status={status_code} | method={http_request.method}"
        f" | path={http_request.url.path} | duration={elapsed_text}s")


def _collect_validation_errors(validation_error: MongoEngineValidationError) -> list[str]:
    """Log the field errors of a mongoengine ValidationError; return ``[str(validation_error)]`` when it has no
    ``errors`` attribute, otherwise an empty list.
    """
    error_messages = []
    if hasattr(validation_error, "errors"):
        if callable(validation_error.errors):  # Check whether 'errors' is a method
            for field_error in validation_error.errors():
                field_name = field_error.get("loc")
                field_message = field_error.get("msg")
                logger.error(f"Error in field {field_name}: {field_message}")
        elif isinstance(validation_error.errors, dict):  # If 'errors' is a dictionary
            for field_name, field_message in validation_error.errors.items():
                logger.error(f"Error in field {field_name}: {field_message}")
    else:
        # Log the exception without 'errors'
        error_messages.append(str(validation_error))
        logger.error(f"Unhandled exception: {str(validation_error)}")
    return error_messages


def _build_validation_error_response(
    http_request: Request, validation_error: MongoEngineValidationError, started_at: float
) -> JSONResponse:
    """The 400 JSON response for a mongoengine ValidationError raised by the app, after logging the metrics line (at
    ERROR level) and the field errors.
    """
    # Log the process time before returning the error
    elapsed_text = _format_elapsed_since(started_at)
    _log_metrics_line(http_request, 400, elapsed_text, log_level=logging.ERROR)

    # Format the error message and return it as a JSON response with status code 400
    # Extract and format error details
    error_messages = _collect_validation_errors(validation_error)

    # Return the formatted error response
    return JSONResponse(status_code=400, content={"errors": error_messages})


async def _log_and_replay_error_response(
    http_request: Request, http_response: StarletteResponse, raw_request_body: bytes, elapsed_text: str
) -> StarletteResponse:
    """Log an error response in one structured line (with both bodies, truncated) and return an equivalent response.

    Reading the body consumes the response's iterator, so the response is rebuilt from the bytes read (same content,
    status code, headers and media type).
    """
    # Read the response body (consuming the iterator)
    raw_response_body = b""
    async for body_chunk in http_response.body_iterator:
        raw_response_body += body_chunk

    request_body_text = _decode_body_for_logging(raw_request_body)
    response_body_text = _decode_body_for_logging(raw_response_body)
    log_level_label = "error" if http_response.status_code >= 500 else "warning"

    # Single structured log line for error requests
    query_params = dict(http_request.query_params)
    logger.info(f"HTTP metrics | level={log_level_label} | status={http_response.status_code} | method={http_request.method}"
        f" | url={http_request.url}"
        f" | path={http_request.url.path}"
        f" | duration={elapsed_text}s"
        f" | client_ip={http_request.client.host if http_request.client else None}"
        f" | query_params={query_params}"
        f" | request_body={request_body_text[:2000]}"
        f" | response_body={response_body_text[:2000]}")
    return StarletteResponse(
        content=raw_response_body,
        status_code=http_response.status_code,
        headers=dict(http_response.headers),
        media_type=http_response.media_type,
    )


@app.middleware("http")
async def log_http_request_metrics(http_request: Request, forward_request):
    """HTTP middleware: time every request and log one metrics line; mongoengine validation errors become a 400 JSON,
    and the bodies of error responses (4xx / 5xx and the 27x webhook codes) are logged.
    """
    started_at = time.time()

    # Cache request body so we can log it on error responses
    raw_request_body = await _read_request_body_for_logging(http_request)

    try:
        http_response = await forward_request(http_request)
    except MongoEngineValidationError as validation_error:
        return _build_validation_error_response(http_request, validation_error, started_at)

    elapsed_text = _format_elapsed_since(started_at)

    # ---- Log & report 270-299 and 4xx / 5xx responses ----
    is_error_response = http_response.status_code >= 400 or (270 <= http_response.status_code <= 299)
    if is_error_response and http_request.url.path not in _PATHS_EXEMPT_FROM_ERROR_LOGGING:
        return await _log_and_replay_error_response(http_request, http_response, raw_request_body, elapsed_text)

    # Structured log line for every request
    _log_metrics_line(http_request, http_response.status_code, elapsed_text)
    http_response.headers["X-Process-Time"] = elapsed_text
    return http_response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
