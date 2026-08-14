"""
Single shared redis.asyncio connection, built once at startup and reused
across requests -- mirrors providers/registry.py's build-once pattern, except
this one needs real teardown (`.aclose()`) since a live TCP connection pool
is a resource, unlike the stateless provider clients.

Startup fails loudly (raises before FastAPI finishes lifespan startup) if
Redis is unreachable, mirroring how a bad ConfigError already aborts
startup: rate limiting has no correct fallback behavior if Redis is down --
silently allowing unlimited traffic or silently blocking everything are both
worse than refusing to boot.
"""

from __future__ import annotations

import os

import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


class RedisUnavailableError(RuntimeError):
    """Raised at startup when the initial Redis PING fails."""


def build_redis_client(url: str | None = None) -> redis.Redis:
    """Construct (but do not connect to) a redis.asyncio client. Connection is
    lazy -- call verify_connection() to actually fail fast at startup.

    Split into build vs. verify (rather than one function) so main.py's
    lifespan can construct then explicitly await-ping inside a try/except,
    and so tests can monkeypatch this factory alone to hand back a fakeredis
    client without needing a real Redis instance."""
    return redis.from_url(url or REDIS_URL, decode_responses=True)


async def verify_connection(client: redis.Redis) -> None:
    """Raises RedisUnavailableError if `client` can't reach its Redis server."""
    try:
        await client.ping()
    except Exception as exc:  # redis.exceptions.RedisError family + ConnectionError
        raise RedisUnavailableError(f"could not connect to Redis at startup: {exc}") from exc
