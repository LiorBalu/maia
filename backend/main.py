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

from maia.observability.context_logging import setup_contextual_logging

# Same console format as the workers: every line carries tenant_name / phone_number when known.
setup_contextual_logging(level=logging.INFO)

twilio_logger = logging.getLogger("twilio.http_client")
twilio_logger.setLevel(logging.WARNING)

googleapi_logger = logging.getLogger("googleapiclient.discovery_cache")
googleapi_logger.setLevel(logging.ERROR)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


origins = [os.environ.get("FRONTEND_URL", "http://localhost:5173"), "http://127.0.0.1:5173", "*"]

app.add_middleware(
    CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
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
async def root():
    """Liveness message at the root URL."""
    return {"message": "The Maia API is running"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": time.time()}


@app.on_event("startup")
async def startup_event():
    """Startup: register the @gpt_tool functions, mount the tenants' custom routers and connect the SSE manager to
    Redis.
    """
    logger.info("Starting up application...")

    # Discover and register all decorated tools
    try:
        from maia.pipeline.tools.function_decorator import discover_all_tools

        discover_all_tools()
        logger.info("Tools auto-discovery completed successfully")
    except Exception as e:
        logger.error(f"Error during tools auto-discovery: {e}")

    # Subscribe the SSE manager to the Redis broadcast channel (live inbox updates)
    try:
        from maia.api.sse import start_sse_manager

        await start_sse_manager()
    except Exception as e:
        logger.error(f"Error initializing the SSE manager: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown: stop the SSE manager and close its connections."""
    logger.info("Shutting down application...")

    try:
        from maia.api.sse import stop_sse_manager

        await stop_sse_manager()
    except Exception as e:
        logger.error(f"Error shutting down the SSE manager: {e}")

    logger.info("Application shutdown complete")


# Paths to skip for error logging (health-check probes; browsers ask for a favicon the backend does not serve)
_SKIP_ERROR_REPORT_PATHS = frozenset({"/health", "/favicon.ico"})

# Max body bytes to log
_MAX_BODY_LOG = 10_000


def _safe_decode(data: bytes, max_len: int = _MAX_BODY_LOG) -> str:
    """Decode bytes to a truncated UTF-8 string, safe for logging."""
    if not data:
        return ""
    try:
        return data[:max_len].decode("utf-8", errors="replace")
    except Exception:
        return "<binary data>"


@app.middleware("http")
async def log_request_time(request: Request, call_next):
    """HTTP middleware: time every request and log one metrics line; mongoengine validation errors become a 400 JSON,
    and the bodies of error responses (4xx / 5xx and the 27x webhook codes) are logged.
    """
    start_time = time.time()

    # Cache request body so we can log it on error responses
    request_body_bytes = b""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            request_body_bytes = await request.body()
        except Exception:
            pass

    try:
        response = await call_next(request)
    except MongoEngineValidationError as e:
        # Log the process time before returning the error
        process_time = time.time() - start_time
        process_time_formatted = f"{process_time:.6f}"
        logger.error(f"HTTP metrics | status=400 | method={request.method}"
            f" | path={request.url.path} | duration={process_time_formatted}s")

        # Format the error message and return it as a JSON response with status code 400
        # Extract and format error details
        errors = []
        if hasattr(e, "errors"):
            if callable(e.errors):  # Vérifie si 'errors' est une méthode
                for error in e.errors():
                    field = error.get("loc")
                    message = error.get("msg")
                    logger.error(f"Error in field {field}: {message}")
            elif isinstance(e.errors, dict):  # Si 'errors' est un dictionnaire
                for field, message in e.errors.items():
                    logger.error(f"Error in field {field}: {message}")
        else:
            # Log the exception without 'errors'
            errors.append(str(e))
            logger.error(f"Unhandled exception: {str(e)}")

        # Return the formatted error response
        return JSONResponse(status_code=400, content={"errors": errors})

    process_time = time.time() - start_time
    process_time_formatted = f"{process_time:.6f}"

    # ---- Log & report 270-299 and 4xx / 5xx responses ----
    _is_error_status = response.status_code >= 400 or (270 <= response.status_code <= 299)
    if _is_error_status and request.url.path not in _SKIP_ERROR_REPORT_PATHS:
        # Read the response body (consuming the iterator)
        response_body = b""
        async for chunk in response.body_iterator:
            response_body += chunk

        request_body_str = _safe_decode(request_body_bytes)
        response_body_str = _safe_decode(response_body)
        level = "error" if response.status_code >= 500 else "warning"

        # Single structured log line for error requests
        query_params_str = dict(request.query_params)
        logger.info(f"HTTP metrics | level={level} | status={response.status_code} | method={request.method}"
            f" | url={request.url}"
            f" | path={request.url.path}"
            f" | duration={process_time_formatted}s"
            f" | client_ip={request.client.host if request.client else None}"
            f" | query_params={query_params_str}"
            f" | request_body={request_body_str[:2000]}"
            f" | response_body={response_body_str[:2000]}")
        return StarletteResponse(
            content=response_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    # Structured log line for every request
    logger.info(f"HTTP metrics | status={response.status_code} | method={request.method}"
        f" | path={request.url.path} | duration={process_time_formatted}s")
    response.headers["X-Process-Time"] = process_time_formatted
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
