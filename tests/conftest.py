from __future__ import annotations

import os
from pathlib import Path

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

# Must be set before llm_gateway.main is imported, since main.py reads
# CONFIG_PATH at module load time.
REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONFIG_PATH", str(REPO_ROOT / "config" / "config.yaml"))

# Providers read this at import time too (see providers/*_provider.py) --
# keep tests hermetic and off the network regardless of the developer's
# local .env.
os.environ.setdefault("LLM_GATEWAY_MOCK_PROVIDERS", "true")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Unit tests must not require a real Redis: patch the factory main.py's
    # lifespan calls so the startup PING check succeeds against an
    # in-memory fakeredis server instead of dialing REDIS_URL. Must patch
    # the name as imported into llm_gateway.main's namespace, not
    # llm_gateway.redis_client's, since that's what lifespan() actually
    # calls.
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("llm_gateway.main.build_redis_client", lambda: fake_redis)

    from llm_gateway.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
