"""Redis client factories for the single Redis instance in REDIS_URL (default "redis://localhost:6379/0")."""
from __future__ import annotations

from os import getenv
from typing import Optional
from urllib.parse import urlparse


def _resolve_redis_url() -> str:
    """REDIS_URL from the environment, with the local default."""
    return getenv("REDIS_URL", "redis://localhost:6379/0")


def build_sync_redis_client(*, should_decode_responses: bool = True, **client_options):
    """Build a sync `redis.Redis` client."""
    import redis

    return redis.from_url(_resolve_redis_url(), decode_responses=should_decode_responses, **client_options)


def build_async_redis_client(*, should_decode_responses: bool = True, **client_options):
    """Build an asyncio `redis.asyncio.Redis` client (the SSE listener)."""
    import redis.asyncio as aioredis

    return aioredis.from_url(_resolve_redis_url(), decode_responses=should_decode_responses, **client_options)


def _parse_database_index(url_path: str) -> int:
    """Redis database index from the path of REDIS_URL ("/3" -> 3); 0 when the path is empty or just "/"."""
    index_text = url_path.strip("/") if url_path else ""
    return int(index_text) if index_text else 0


def build_arq_redis_settings(**settings_overrides):
    """arq RedisSettings built from REDIS_URL."""
    from arq.connections import RedisSettings

    parsed_url = urlparse(_resolve_redis_url())
    settings_fields = dict(
        host=parsed_url.hostname or "redis-stack",
        port=parsed_url.port or 6379,
        database=_parse_database_index(parsed_url.path),
        password=parsed_url.password,
    )
    settings_fields.update(settings_overrides)
    return RedisSettings(**settings_fields)


def build_redis_chat_history_store(history_session_id: str, history_key_prefix: str, expiry_seconds: Optional[int] = None):
    """Build a LangChain `RedisChatMessageHistory` on REDIS_URL."""
    from langchain_community.chat_message_histories import RedisChatMessageHistory

    return RedisChatMessageHistory(session_id=history_session_id, url=_resolve_redis_url(), key_prefix=history_key_prefix, ttl=expiry_seconds)
