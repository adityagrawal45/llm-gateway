"""
Provider registry: builds each ProviderClient exactly once at app startup and
hands out shared instances by Provider enum, instead of every request
constructing (and tearing down) its own client.
"""

from __future__ import annotations

from llm_gateway.config.schema import Provider
from llm_gateway.providers.anthropic_provider import AnthropicProvider
from llm_gateway.providers.base import ProviderClient
from llm_gateway.providers.ollama_provider import OllamaProvider
from llm_gateway.providers.openai_provider import OpenAIProvider


class ProviderRegistry:
    def __init__(self, clients: dict[Provider, ProviderClient]):
        self._clients = clients

    def get(self, provider: Provider) -> ProviderClient:
        """Raises KeyError if no client is registered for `provider`."""
        return self._clients[provider]


def build_registry() -> ProviderRegistry:
    """Default registry wiring all three built-in providers. Called once from
    main.py's lifespan and stashed on app.state.provider_registry."""
    return ProviderRegistry(
        {
            Provider.OPENAI: OpenAIProvider(),
            Provider.ANTHROPIC: AnthropicProvider(),
            Provider.OLLAMA: OllamaProvider(),
        }
    )
