"""
Phase 3 budget enforcement tests.

Same shape as tests/test_rate_limit.py: most tests exercise BudgetTracker
directly against fakeredis (no app) for precision, a few go through the
`client` fixture (itself running on fakeredis, per conftest.py) to assert
the FastAPI 402 + structured-body wiring end to end, and one
@pytest.mark.redis test requires a real Redis instance -- excluded from the
default run via pyproject.toml's addopts, run explicitly with
`pytest -m redis` after `docker compose up redis -d`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from llm_gateway.api.schemas import Usage
from llm_gateway.budget.pricing import compute_cost_usd
from llm_gateway.budget.tracker import BudgetTracker, _monthly_window_id, _spend_key, _to_micros
from llm_gateway.config.schema import Provider
from llm_gateway.providers.registry import ProviderRegistry
from tests.test_chat import CHAT_BODY, PLATFORM_ENG_KEY, SANDBOX_KEY, FakeOpenAIProvider

# FakeOpenAIProvider (test_chat.py) always returns usage prompt_tokens=5,
# completion_tokens=2 -- with gpt-4o's pricing ($0.0025/1k in, $0.01/1k out)
# that's a fixed, easy-to-assert-on cost per call.
FAKE_CALL_COST_USD = compute_cost_usd("gpt-4o", Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7))


@pytest.fixture()
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def tracker(fake_redis: fakeredis.aioredis.FakeRedis) -> BudgetTracker:
    return BudgetTracker(fake_redis)


# --- pricing.py -------------------------------------------------------------


def test_compute_cost_known_model() -> None:
    cost = compute_cost_usd("gpt-4o", Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000))
    assert cost == pytest.approx(0.0025 + 0.010)


def test_compute_cost_unknown_model_fails_open_to_zero() -> None:
    cost = compute_cost_usd("some-future-model", Usage(prompt_tokens=1000, completion_tokens=1000, total_tokens=2000))
    assert cost == 0.0


def test_ollama_model_is_free() -> None:
    cost = compute_cost_usd("llama3", Usage(prompt_tokens=10_000, completion_tokens=10_000, total_tokens=20_000))
    assert cost == 0.0


# --- BudgetTracker unit tests (fakeredis, no app) ---------------------------


async def test_under_budget_allowed(tracker: BudgetTracker) -> None:
    result = await tracker.check_budget("teamA", daily_limit_usd=10.0, monthly_limit_usd=100.0)
    assert result.allowed


async def test_spend_recorded_and_reflected_in_status(tracker: BudgetTracker) -> None:
    await tracker.record_spend("teamA", 1.50)
    daily, monthly = await tracker.get_status("teamA", daily_limit_usd=10.0, monthly_limit_usd=100.0)
    assert daily.spend_usd == pytest.approx(1.50)
    assert monthly.spend_usd == pytest.approx(1.50)
    assert daily.remaining_usd == pytest.approx(8.50)


async def test_daily_budget_exceeded_rejected(tracker: BudgetTracker) -> None:
    await tracker.record_spend("teamA", 10.0)
    result = await tracker.check_budget("teamA", daily_limit_usd=10.0, monthly_limit_usd=1000.0)
    assert not result.allowed
    assert result.exceeded == "daily"
    assert result.reset_at is not None


async def test_monthly_budget_exceeded_rejected_even_with_daily_room(
    tracker: BudgetTracker, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    # Push only the monthly counter over its limit, bypassing record_spend
    # (which always writes both windows for real usage) so daily stays at 0
    # -- isolates "monthly exceeded, daily fine" from the tracker's own
    # internals rather than relying on record_spend's dual-write behavior.
    now = dt.datetime.now(dt.UTC)
    monthly_key = _spend_key("teamA", "monthly", _monthly_window_id(now))
    await fake_redis.set(monthly_key, _to_micros(500.0))

    result = await tracker.check_budget("teamA", daily_limit_usd=10.0, monthly_limit_usd=500.0)
    assert not result.allowed
    assert result.exceeded == "monthly"


async def test_different_teams_have_independent_budgets(tracker: BudgetTracker) -> None:
    await tracker.record_spend("teamA", 10.0)
    result_b = await tracker.check_budget("teamB", daily_limit_usd=10.0, monthly_limit_usd=100.0)
    assert result_b.allowed


async def test_concurrent_spend_recording_atomicity(tracker: BudgetTracker) -> None:
    """20 concurrent record_spend($0.10) calls must sum to exactly $2.00 --
    proves the shared INCRBY_AND_EXPIRE_SCRIPT (redis_lua.py) doesn't lose
    updates under concurrent writers, mirroring test_rate_limit.py's
    test_concurrent_burst_atomicity for the analogous rate-limit write."""
    await asyncio.gather(*[tracker.record_spend("teamA", 0.10) for _ in range(20)])
    daily, _monthly = await tracker.get_status("teamA", daily_limit_usd=1000.0, monthly_limit_usd=1000.0)
    assert daily.spend_usd == pytest.approx(2.00)


# --- End-to-end through the app ---------------------------------------------


def test_request_under_budget_succeeds_and_records_spend(client: TestClient) -> None:
    fake = FakeOpenAIProvider()
    client.app.state.provider_registry = ProviderRegistry({Provider.OPENAI: fake})

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=CHAT_BODY,
    )
    assert response.status_code == 200

    status_response = client.get(
        "/v1/teams/platform-eng/budget", headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"}
    )
    assert status_response.status_code == 200
    body = status_response.json()
    # abs tolerance, not just relative: the tracker stores spend as rounded
    # integer micro-dollars (see tracker.py's module docstring), so a cost
    # this tiny (a few hundredths of a cent) can differ from the unrounded
    # Python float by up to half a micro-dollar ($0.0000005).
    assert body["daily"]["spend_usd"] == pytest.approx(FAKE_CALL_COST_USD, abs=1e-6)


def test_over_daily_budget_returns_402(client: TestClient) -> None:
    fake = FakeOpenAIProvider()
    client.app.state.provider_registry = ProviderRegistry({Provider.OPENAI: fake})

    # sandbox's daily_usd is 5, monthly_usd is 50 (config/config.yaml).
    # Pre-seed via the TestClient's own portal (its blocking-async bridge),
    # not a freshly created event loop -- fakeredis's connection objects are
    # bound to the loop they were created on, and the app (started by the
    # `client` fixture) runs on the portal's loop, not whatever loop
    # asyncio.get_event_loop() would hand back here.
    tracker = client.app.state.budget_tracker
    assert client.portal is not None
    client.portal.call(tracker.record_spend, "sandbox", 5.0)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {SANDBOX_KEY}"},
        json={"model": "llama3", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 402
    body = response.json()["detail"]
    assert body["error"] == "budget_exceeded"
    assert body["window"] == "daily"


def test_budget_status_rejects_cross_team_lookup(client: TestClient) -> None:
    response = client.get(
        "/v1/teams/data-science/budget", headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"}
    )
    assert response.status_code == 403


def test_budget_status_requires_auth(client: TestClient) -> None:
    response = client.get("/v1/teams/platform-eng/budget")
    assert response.status_code == 401


# --- Real-Redis integration test (excluded from default runs) --------------


@pytest.mark.redis
async def test_real_redis_spend_atomicity() -> None:
    """Same atomicity guarantee as test_concurrent_spend_recording_atomicity,
    but against a real Redis instance instead of fakeredis."""
    import redis.asyncio as redis_asyncio

    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    real_client = redis_asyncio.from_url(url, decode_responses=True)
    try:
        await real_client.ping()
        tracker = BudgetTracker(real_client)
        stale_keys = await real_client.keys("budget:integration-team:*")
        if stale_keys:
            await real_client.delete(*stale_keys)

        await asyncio.gather(*[tracker.record_spend("integration-team", 0.10) for _ in range(20)])
        daily, _monthly = await tracker.get_status(
            "integration-team", daily_limit_usd=1000.0, monthly_limit_usd=1000.0
        )
        assert daily.spend_usd == pytest.approx(2.00)
    finally:
        await real_client.aclose()
