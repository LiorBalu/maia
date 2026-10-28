"""Redis client factories for the single Redis instance in REDIS_URL (default "redis://localhost:6379/0")."""
from __future__ import annotations

from os import getenv
from typing import Optional
from urllib.parse import urlparse


def _redis_url() -> str:
    """REDIS_URL from the environment, with the local default."""
    return getenv("REDIS_URL", "redis://localhost:6379/0")


def get_sync_redis(*, decode_responses: bool = True, **extra):
    """Build a sync `redis.Redis` client."""
    import redis

    return redis.from_url(_redis_url(), decode_responses=decode_responses, **extra)


def get_arq_redis_settings(**overrides):
    """arq RedisSettings built from REDIS_URL."""
    from arq.connections import RedisSettings

    parsed = urlparse(_redis_url())
    kwargs = dict(
        host=parsed.hostname or "redis-stack",
        port=parsed.port or 6379,
        database=int(parsed.path.strip("/")) if parsed.path and parsed.path.strip("/") else 0,
        password=parsed.password,
    )
    kwargs.update(overrides)
    return RedisSettings(**kwargs)


def make_redis_chat_history(session_id: str, key_prefix: str, ttl: Optional[int] = None):
    """Build a LangChain `RedisChatMessageHistory` on REDIS_URL."""
    from langchain_community.chat_message_histories import RedisChatMessageHistory

    return RedisChatMessageHistory(session_id=session_id, url=_redis_url(), key_prefix=key_prefix, ttl=ttl)
