"""
OpenAI Chat Completions integration.

OpenAI's wire format is close enough to the gateway's own
ChatCompletionRequest/Response shape (it's what the gateway's shape was
modeled on) that this translation layer is mostly pass-through -- unlike
Anthropic and Ollama, which need real reshaping.
"""

from __future__ import annotations

import os

import httpx

from llm_gateway.api.schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from llm_gateway.providers.base import ProviderClient, ProviderError, build_mock_response

_MOCK = os.getenv("LLM_GATEWAY_MOCK_PROVIDERS", "true").strip().lower() in ("1", "true", "yes")


class OpenAIProvider(ProviderClient):
    """Talks to the OpenAI Chat Completions API.

    Real HTTP calls are gated behind LLM_GATEWAY_MOCK_PROVIDERS (default: on).
    Phase 1 is about the routing/abstraction layer, not production provider
    integration -- flip the env var off and set OPENAI_API_KEY to hit the
    real API instead.
    """

    _BASE_URL = "https://api.openai.com/v1/chat/completions"
    _SUPPORTED_MODELS = frozenset({"gpt-4o", "gpt-4o-mini", "gpt-4-turbo"})

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key: str = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supported_models(self) -> frozenset[str]:
        return self._SUPPORTED_MODELS

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if _MOCK:
            return build_mock_response(provider="openai", request=request)

        payload = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            **({"max_tokens": request.max_tokens} if request.max_tokens else {}),
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                resp = await http_client.post(self._BASE_URL, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai request failed: {exc}") from exc

        data = resp.json()
        try:
            choice = data["choices"][0]
            return ChatCompletionResponse(
                id=data["id"],
                model=data["model"],
                provider="openai",
                choices=[
                    ChatCompletionChoice(
                        index=choice.get("index", 0),
                        message=ChatMessage(**choice["message"]),
                        finish_reason=choice.get("finish_reason") or "stop",
                    )
                ],
                usage=Usage(**data["usage"]),
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected openai response shape: {exc}") from exc
