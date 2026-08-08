"""
Provider-agnostic request/response models for the chat completions route.

These are deliberately shaped like the OpenAI Chat Completions API (the
closest thing to a lingua franca for this kind of endpoint) since clients of
the gateway shouldn't need to know or care which upstream provider actually
served a given model. Each ProviderClient is responsible for translating to
and from its own provider's native wire format at the edges -- nothing
outside src/llm_gateway/providers/ should ever see a provider-native shape.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    model: str
    provider: str
    choices: list[ChatCompletionChoice] = Field(min_length=1)
    usage: Usage
