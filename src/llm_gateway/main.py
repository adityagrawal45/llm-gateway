"""
FastAPI application entrypoint.

Phase 0 scope: app boots, loads + hot-reloads config, exposes /healthz.
Phase 1 scope: provider abstraction + POST /v1/chat/completions, routed and
auth-checked against the loaded config. Rate limiting, budgets, provider
fallback, and telemetry export wiring still don't exist -- those land in
later phases.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from llm_gateway.api.chat import router as chat_router
from llm_gateway.api.health import router as health_router
from llm_gateway.config.loader import ConfigError, ConfigLoader
from llm_gateway.providers.registry import build_registry

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

    logger.info("llm-gateway started")

    yield

    await loader.stop_watching()
    logger.info("llm-gateway stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(chat_router)
    return app


app = create_app()
