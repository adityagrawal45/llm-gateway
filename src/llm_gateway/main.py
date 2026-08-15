"""
FastAPI application entrypoint.

Phase 0 scope: app boots, loads + hot-reloads config, exposes /healthz.
Phase 1 scope: provider abstraction + POST /v1/chat/completions, routed and
auth-checked against the loaded config. Phase 2 scope: per-team Redis-backed
rate limiting (see rate_limit/limiter.py) wired in here via a shared
redis.asyncio connection built once at startup. Phase 3 scope: per-team
budget enforcement (see budget/tracker.py), reusing that same Redis
connection rather than opening a second one -- budget and rate-limit state
are both just Redis-backed counters, no reason to duplicate the client.
Provider fallback and telemetry export wiring still don't exist -- those
land in later phases.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from llm_gateway.api.budget import router as budget_router
from llm_gateway.api.chat import router as chat_router
from llm_gateway.api.health import router as health_router
from llm_gateway.budget.tracker import BudgetTracker
from llm_gateway.config.loader import ConfigError, ConfigLoader
from llm_gateway.providers.registry import build_registry
from llm_gateway.rate_limit.limiter import RateLimiter
from llm_gateway.redis_client import RedisUnavailableError, build_redis_client, verify_connection

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("llm_gateway")

CONFIG_PATH = os.getenv("CONFIG_PATH", "config/config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    loader = ConfigLoader(CONFIG_PATH)
    try:
        loader.load()
    except ConfigError as exc:
        logger.error("startup config load failed: %s", exc)
        raise

    await loader.start_watching()
    app.state.config_loader = loader

    # Built once at startup and reused across requests (see providers/registry.py) --
    # not re-created per call.
    app.state.provider_registry = build_registry()

    # Rate limiting (Phase 2) has no correct fallback if Redis is unreachable --
    # fail startup loudly here, the same way a bad config load already does,
    # instead of booting into a state where limits silently don't work.
    redis_client = build_redis_client()
    try:
        await verify_connection(redis_client)
    except RedisUnavailableError as exc:
        logger.error("startup Redis check failed: %s", exc)
        await redis_client.aclose()
        raise
    app.state.redis_client = redis_client
    app.state.rate_limiter = RateLimiter(redis_client)
    # Same Redis connection as the rate limiter -- see module docstring.
    app.state.budget_tracker = BudgetTracker(redis_client)

    logger.info("llm-gateway started")

    yield

    await loader.stop_watching()
    await redis_client.aclose()
    logger.info("llm-gateway stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(budget_router)
    return app


app = create_app()
