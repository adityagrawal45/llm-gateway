"""
Phase 1 tests: auth rejection, model/provider authorization rejection, and a
happy-path completion. No real provider APIs are called -- the default
LLM_GATEWAY_MOCK_PROVIDERS=true (set in conftest.py) makes the real
providers return canned responses, and the happy-path test additionally
swaps in a hand-written FakeProviderClient so it can assert the gateway
actually called the provider it was supposed to, with the request it
received.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from llm_gateway.api.schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from llm_gateway.config.schema import Provider
from llm_gateway.providers.base import ProviderClient, ProviderError
from llm_gateway.providers.registry import ProviderRegistry

# From the sample config/config.yaml shipped in the repo.
PLATFORM_ENG_KEY = "team-platform-eng-devkey-001"  # allows openai, anthropic
SANDBOX_KEY = "team-sandbox-devkey-003"  # allows ollama / llama3 only

CHAT_BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
# platform-eng also allows claude-sonnet-4-6 via anthropic -- used to exercise
# fallback across both of its allowed providers.
FALLBACK_CHAT_BODY = {
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "hi"}],
}


class FakeOpenAIProvider(ProviderClient):
    """Records every request it receives instead of talking to any network."""

    def __init__(self) -> None:
        self.received: list[ChatCompletionRequest] = []

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supported_models(self) -> frozenset[str]:
        return frozenset({"gpt-4o"})

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        self.received.append(request)
        return ChatCompletionResponse(
            id="fake-1",
            model=request.model,
            provider="openai",
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="hi there"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
        )


class FailingProvider(ProviderClient):
    """Always raises ProviderError -- stands in for a downed upstream."""

    def __init__(self, name: str, supported_models: frozenset[str]) -> None:
        self._name = name
        self._supported_models = supported_models
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_models(self) -> frozenset[str]:
        return self._supported_models

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        self.call_count += 1
        raise ProviderError(f"{self._name} is down")


class FakeAnthropicProvider(ProviderClient):
    """Records every request it receives instead of talking to any network."""

    def __init__(self) -> None:
        self.received: list[ChatCompletionRequest] = []

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supported_models(self) -> frozenset[str]:
        return frozenset({"claude-sonnet-4-6"})

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        self.received.append(request)
        return ChatCompletionResponse(
            id="fake-2",
            model=request.model,
            provider="anthropic",
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="hi from anthropic"),
                    finish_reason="stop",
                )
            ],
            usage=Usage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        )


def test_missing_api_key_rejected(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 401


def test_invalid_api_key_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer not-a-real-key"},
        json=CHAT_BODY,
    )
    assert response.status_code == 401


def test_x_api_key_header_accepted(client: TestClient) -> None:
    fake = FakeOpenAIProvider()
    client.app.state.provider_registry = ProviderRegistry({Provider.OPENAI: fake})

    response = client.post(
        "/v1/chat/completions",
        headers={"X-API-Key": PLATFORM_ENG_KEY},
        json=CHAT_BODY,
    )
    assert response.status_code == 200


def test_model_not_allowed_for_team_rejected(client: TestClient) -> None:
    # sandbox team's allowed_models is just ["llama3"] -- gpt-4o should 403.
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {SANDBOX_KEY}"},
        json=CHAT_BODY,
    )
    assert response.status_code == 403


def test_model_not_served_by_any_allowed_provider_rejected(client: TestClient) -> None:
    # platform-eng allows gpt-4o and is allowed openai+anthropic, but the
    # registered OpenAI client below doesn't claim to support it -- and
    # anthropic never supports an openai-named model -- so routing must 403,
    # not fall through to a KeyError or a wrong provider.
    class EmptyFakeProvider(FakeOpenAIProvider):
        @property
        def supported_models(self) -> frozenset[str]:
            return frozenset()

    client.app.state.provider_registry = ProviderRegistry({Provider.OPENAI: EmptyFakeProvider()})

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=CHAT_BODY,
    )
    assert response.status_code == 403


def test_happy_path_completion_routes_to_correct_provider(client: TestClient) -> None:
    fake = FakeOpenAIProvider()
    client.app.state.provider_registry = ProviderRegistry({Provider.OPENAI: fake})

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=CHAT_BODY,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openai"
    assert body["choices"][0]["message"]["content"] == "hi there"
    assert body["usage"]["total_tokens"] == 7

    assert len(fake.received) == 1
    assert fake.received[0].model == "gpt-4o"


def test_fallback_to_next_provider_on_failure(client: TestClient) -> None:
    # platform-eng allows [openai, anthropic] and claude-sonnet-4-6 is only
    # served (in this test) by anthropic -- but put a failing openai client
    # in front of it too, since fallback should just skip past a provider
    # that doesn't even claim to support the model, same as before Phase 4.
    failing_openai = FailingProvider("openai", frozenset({"gpt-4o"}))
    fake_anthropic = FakeAnthropicProvider()
    client.app.state.provider_registry = ProviderRegistry(
        {Provider.OPENAI: failing_openai, Provider.ANTHROPIC: fake_anthropic}
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=FALLBACK_CHAT_BODY,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "anthropic"
    assert len(fake_anthropic.received) == 1


def test_fallback_calls_second_provider_when_first_errors(client: TestClient) -> None:
    # Both providers claim to support the model; the first one errors, so
    # the route must retry with the second rather than 502-ing immediately.
    failing_openai = FailingProvider("openai", frozenset({"claude-sonnet-4-6"}))
    fake_anthropic = FakeAnthropicProvider()
    client.app.state.provider_registry = ProviderRegistry(
        {Provider.OPENAI: failing_openai, Provider.ANTHROPIC: fake_anthropic}
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=FALLBACK_CHAT_BODY,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "anthropic"
    assert failing_openai.call_count == 1
    assert len(fake_anthropic.received) == 1


def test_502_when_every_allowed_provider_fails(client: TestClient) -> None:
    failing_openai = FailingProvider("openai", frozenset({"claude-sonnet-4-6"}))
    failing_anthropic = FailingProvider("anthropic", frozenset({"claude-sonnet-4-6"}))
    client.app.state.provider_registry = ProviderRegistry(
        {Provider.OPENAI: failing_openai, Provider.ANTHROPIC: failing_anthropic}
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {PLATFORM_ENG_KEY}"},
        json=FALLBACK_CHAT_BODY,
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "openai" in detail
    assert "anthropic" in detail
    assert failing_openai.call_count == 1
    assert failing_anthropic.call_count == 1
