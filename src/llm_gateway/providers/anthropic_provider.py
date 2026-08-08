"""
Anthropic Messages API integration.

Two shape differences from the gateway's OpenAI-flavored
ChatCompletionRequest that this translation layer exists specifically to
paper over:

  1. Anthropic has no "system" role inside `messages` -- system content is a
     separate top-level `system` string. System messages are pulled out of
     the request and joined into that field.
  2. `max_tokens` is required on every Anthropic request (no server-side
     default), unlike the gateway's optional field -- a default is applied
     here when the caller didn't supply one.
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


class AnthropicProvider(ProviderClient):
    """Talks to the Anthropic Messages API.

    Real HTTP calls are gated behind LLM_GATEWAY_MOCK_PROVIDERS (default: on).
    Flip it off and set ANTHROPIC_API_KEY to hit the real API instead.
    """

    _BASE_URL = "https://api.anthropic.com/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"
    _DEFAULT_MAX_TOKENS = 1024
    _SUPPORTED_MODELS = frozenset({"claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"})

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key: str = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supported_models(self) -> frozenset[str]:
        return self._SUPPORTED_MODELS

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if _MOCK:
            return build_mock_response(provider="anthropic", request=request)

        system_text = "\n".join(m.content for m in request.messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]

        payload = {
            "model": request.model,
            "messages": turns,
            "max_tokens": request.max_tokens or self._DEFAULT_MAX_TOKENS,
            "temperature": request.temperature,
            **({"system": system_text} if system_text else {}),
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                resp = await http_client.post(self._BASE_URL, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc

        data = resp.json()
        try:
            text = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
            usage = data["usage"]
            return ChatCompletionResponse(
                id=data["id"],
                model=data["model"],
                provider="anthropic",
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ChatMessage(role="assistant", content=text),
                        finish_reason=data.get("stop_reason") or "stop",
                    )
                ],
                usage=Usage(
                    prompt_tokens=usage["input_tokens"],
                    completion_tokens=usage["output_tokens"],
                    total_tokens=usage["input_tokens"] + usage["output_tokens"],
                ),
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected anthropic response shape: {exc}") from exc
