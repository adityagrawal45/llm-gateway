"""
Ollama integration, for locally/self-hosted models.

Unlike OpenAI/Anthropic this talks to a base URL rather than a fixed
provider domain (OLLAMA_BASE_URL, defaulting to a local instance), and
authenticates with nothing at all -- there's no API key concept for a local
Ollama server. Its native response also reports token counts under
different field names (prompt_eval_count/eval_count) and has no request id,
so one is synthesized.
"""

from __future__ import annotations

import os
import uuid

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


class OllamaProvider(ProviderClient):
    """Talks to a local/self-hosted Ollama server's /api/chat endpoint.

    Real HTTP calls are gated behind LLM_GATEWAY_MOCK_PROVIDERS (default: on).
    Flip it off and point OLLAMA_BASE_URL at a running Ollama instance to hit
    it for real.
    """

    _SUPPORTED_MODELS = frozenset({"llama3", "llama3.1", "mistral"})

    def __init__(self, base_url: str | None = None) -> None:
        resolved = base_url if base_url is not None else os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self._base_url = resolved.rstrip("/")

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def supported_models(self) -> frozenset[str]:
        return self._SUPPORTED_MODELS

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        if _MOCK:
            return build_mock_response(provider="ollama", request=request)

        options = {"temperature": request.temperature}
        if request.max_tokens:
            options["num_predict"] = request.max_tokens

        payload = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "options": options,
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as http_client:
                resp = await http_client.post(f"{self._base_url}/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama request failed: {exc}") from exc

        data = resp.json()
        try:
            prompt_tokens = data.get("prompt_eval_count", 0)
            completion_tokens = data.get("eval_count", 0)
            return ChatCompletionResponse(
                id=f"ollama-{uuid.uuid4().hex[:12]}",
                model=data.get("model", request.model),
                provider="ollama",
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ChatMessage(**data["message"]),
                        finish_reason="stop" if data.get("done", True) else "length",
                    )
                ],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
            )
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"unexpected ollama response shape: {exc}") from exc
