"""
Common interface every provider integration implements, plus the shared mock
response builder used by all three implementations.

Translating between the gateway's provider-agnostic ChatCompletionRequest/
ChatCompletionResponse and a provider's native wire format happens entirely
inside that provider's ProviderClient -- nothing above this layer (routes,
routing logic) should ever construct or inspect a provider-native payload.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from llm_gateway.api.schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)


class ProviderError(RuntimeError):
    """Raised when a provider call fails: network error, non-2xx response, or a
    response shape the gateway doesn't know how to normalize. Phase 1 has no
    fallback logic, so callers currently just turn this into a 502."""


class ProviderClient(ABC):
    """One instance per configured upstream provider, built once at startup by
    the provider registry and reused across requests (so e.g. connection
    pooling in a real SDK client would be shared, not rebuilt per call)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Machine-readable provider name; matches config.schema.Provider values."""

    @property
    @abstractmethod
    def supported_models(self) -> frozenset[str]:
        """Models this client knows how to translate requests/responses for.
        Used to pick which allowed provider actually serves a given model when
        a team is allowed more than one (see api/chat.py)."""

    @abstractmethod
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Perform a chat completion and return it in gateway-normalized form.
        Raises ProviderError on any failure -- never lets a provider-native
        exception type leak past this boundary."""


def build_mock_response(provider: str, request: ChatCompletionRequest) -> ChatCompletionResponse:
    """Deterministic canned reply, used by every provider when
    LLM_GATEWAY_MOCK_PROVIDERS is on (the default). Phase 1 is about the
    routing/abstraction layer, not shipping production provider integrations,
    so this keeps the gateway runnable and testable with zero credentials and
    zero network access. Flip the env var off per-provider to hit the real API."""
    last_user_msg = next(
        (m.content for m in reversed(request.messages) if m.role == "user"), ""
    )
    reply_text = f"[mock {provider} reply to: {last_user_msg[:80]!r}]"

    prompt_tokens = sum(len(m.content.split()) for m in request.messages)
    completion_tokens = len(reply_text.split())

    return ChatCompletionResponse(
        id=f"mock-{uuid.uuid4().hex[:12]}",
        model=request.model,
        provider=provider,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=reply_text),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )
