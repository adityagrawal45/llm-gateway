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
from llm_gateway.providers.base import ProviderClient
from llm_gateway.providers.registry import ProviderRegistry

# From the sample config/config.yaml shipped in the repo.
PLATFORM_ENG_KEY = "team-platform-eng-devkey-001"  # allows openai, anthropic
SANDBOX_KEY = "team-sandbox-devkey-003"  # allows ollama / llama3 only

CHAT_BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}


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
