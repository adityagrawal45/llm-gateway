"""
Phase 2 rate limiting tests.

Most tests exercise RateLimiter directly against fakeredis (no app, no real
Redis) for speed and precision about exactly what's being asserted. A
couple go through the `client` fixture (which itself runs on fakeredis, per
conftest.py) to assert the FastAPI 429 + Retry-After wiring end to end. One
test is marked @pytest.mark.redis and requires a real Redis instance (see
docker-compose.yml's `redis` service) -- it's excluded from the default
`pytest` run via pyproject.toml's `addopts`, run it explicitly with
`pytest -m redis` after `docker compose up redis -d`.
"""

from __future__ import annotations

import asyncio
import os

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from llm_gateway.config.schema import Provider
from llm_gateway.providers.registry import ProviderRegistry
from llm_gateway.rate_limit.limiter import RateLimiter
from tests.test_chat import CHAT_BODY, PLATFORM_ENG_KEY, SANDBOX_KEY, FakeOpenAIProvider


@pytest.fixture()
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def limiter(fake_redis: fakeredis.aioredis.FakeRedis) -> RateLimiter:
    return RateLimiter(fake_redis)


# --- RateLimiter unit tests (fakeredis, no app) ---------------------------


async def test_requests_under_limit_allowed(limiter: RateLimiter) -> None:
    for _ in range(5):
        result = await limiter.check_and_increment_requests("teamA", limit=5)
        assert result.allowed


async def test_requests_over_limit_rejected(limiter: RateLimiter) -> None:
    for _ in range(5):
        await limiter.check_and_increment_requests("teamA", limit=5)
    result = await limiter.check_and_increment_requests("teamA", limit=5)
    assert not result.allowed
    assert result.retry_after_seconds > 0


async def test_burst_extends_effective_limit(limiter: RateLimiter) -> None:
    result = None
    for _ in range(7):
        result = await limiter.check_and_increment_requests("teamA", limit=5, burst=2)
    assert result is not None
    assert result.allowed  # 7th request, effective limit is 5 + 2 = 7


async def test_tokens_under_limit_allowed(limiter: RateLimiter) -> None:
    result = await limiter.check_tokens("teamA", limit=1000)
    assert result.allowed
    await limiter.record_tokens("teamA", 500)
    result = await limiter.check_tokens("teamA", limit=1000)
    assert result.allowed


async def test_tokens_over_limit_rejected_on_next_check(limiter: RateLimiter) -> None:
    # Mirrors the documented check-before/record-after trade-off: a request
    # that pushes the counter over the limit is still allowed to go through
    # (it already happened by the time record_tokens runs); only the *next*
    # check_tokens() call sees the counter already at/over the limit.
    await limiter.record_tokens("teamA", 1200)
    result = await limiter.check_tokens("teamA", limit=1000)
    assert not result.allowed


async def test_different_teams_have_independent_counters(limiter: RateLimiter) -> None:
    for _ in range(5):
        await limiter.check_and_increment_requests("teamA", limit=5)
    result_b = await limiter.check_and_increment_requests("teamB", limit=5)
    assert result_b.allowed


async def test_concurrent_burst_atomicity(limiter: RateLimiter) -> None:
    """20 concurrent callers against a limit of 10 must yield exactly 10
    allowed -- proves the Lua EVAL round trip is atomic (no read-then-
    increment race letting extras slip through under load)."""
    results = await asyncio.gather(
        *[limiter.check_and_increment_requests("teamA", limit=10) for _ in range(20)]
    )
    allowed_count = sum(1 for r in results if r.allowed)
    assert allowed_count == 10


# --- End-to-end through the app --------------------------------------------


def test_over_request_limit_returns_429(client: TestClient) -> None:
    # sandbox is only allowed the ollama provider (config/config.yaml), so
    # unlike the other tests here this deliberately does NOT override
    # provider_registry -- it rides the default registry's OllamaProvider,
    # which returns mock responses because LLM_GATEWAY_MOCK_PROVIDERS=true
    # (conftest.py), same as the plain smoke/chat tests do.
    body = {"model": "llama3", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"Authorization": f"Bearer {SANDBOX_KEY}"}

    # sandbox's requests_per_minute is 30 with burst 0 (config/config.yaml).
    # Every call goes through TestClient's own portal/event loop, so (unlike
    # driving the limiter directly from the test) there's no cross-event-loop
    # mismatch with the fakeredis connection the app opened at startup.
    for _ in range(30):
        response = client.post("/v1/chat/completions", headers=headers, json=body)
        assert response.status_code == 200

    response = client.post("/v1/chat/completions", headers=headers, json=body)
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_token_usage_recorded_after_successful_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOpenAIProvider()
    client.app.state.provider_registry = ProviderRegistry({Provider.OPENAI: fake})

    # Spy on RateLimiter.record_tokens rather than reading the counter back
    # afterward through a second, independently-created client -- this
    # verifies chat.py's post-call recording call directly, on the same
    # event loop the request itself ran on.
    recorded: list[tuple[str, int]] = []
    original_record_tokens = RateLimiter.record_tokens

    async def spy_record_tokens(self: RateLimiter, team: str, tokens: int) -> None:
        recorded.append((team, tokens))
        await original_record_tokens(self, team, tokens)

    monkeypatch.setattr(RateLimiter, "record_tokens", spy_record_tokens)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=CHAT_BODY,
    )
    assert response.status_code == 200
    # FakeOpenAIProvider always returns usage.total_tokens == 7 (test_chat.py).
    assert recorded == [("platform-eng", 7)]


# --- Real-Redis integration test (excluded from default runs) -------------


@pytest.mark.redis
async def test_real_redis_atomicity() -> None:
    """Same atomicity guarantee as test_concurrent_burst_atomicity, but
    against a real Redis instance (docker-compose's `redis` service, or
    whatever REDIS_URL points at) instead of fakeredis, to catch anything
    fakeredis's Lua emulation might paper over."""
    import redis.asyncio as redis_asyncio

    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    real_client = redis_asyncio.from_url(url, decode_responses=True)
    try:
        await real_client.ping()
        limiter = RateLimiter(real_client)
        stale_keys = await real_client.keys("ratelimit:integration-team:*")
        if stale_keys:
            await real_client.delete(*stale_keys)

        results = await asyncio.gather(
            *[limiter.check_and_increment_requests("integration-team", limit=10) for _ in range(20)]
        )
        assert sum(1 for r in results if r.allowed) == 10
    finally:
        await real_client.aclose()
