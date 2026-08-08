"""
The core gateway route: POST /v1/chat/completions.

Phase 1 scope only -- validate the team is allowed to use the requested
model, pick the first allowed provider that can actually serve it, call
that provider once, and return its normalized response. No retries, no
fallback to a second provider, no rate limiting, no budget checks; those
are later phases.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from llm_gateway.api.auth import require_team
from llm_gateway.api.schemas import ChatCompletionRequest, ChatCompletionResponse
from llm_gateway.config.schema import TeamConfig
from llm_gateway.providers.base import ProviderClient, ProviderError

router = APIRouter(tags=["chat"])


def _select_provider(request: Request, team: TeamConfig, model: str) -> ProviderClient:
    """Walk the team's allowed providers in the order configured, and use the
    first one whose ProviderClient claims to support the requested model.
    Order matters: it's what later becomes fallback order in Phase 4."""
    registry = request.app.state.provider_registry
    for provider in team.allowed_providers:
        try:
            candidate = registry.get(provider)
        except KeyError:
            continue
        if model in candidate.supported_models:
            return candidate

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"model '{model}' is not served by any provider team '{team.name}' "
            "is allowed to use"
        ),
    )


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    team: TeamConfig = Depends(require_team),  # noqa: B008 -- FastAPI's DI pattern, not a real bug
) -> ChatCompletionResponse:
    if body.model not in team.allowed_models:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"team '{team.name}' is not allowed to use model '{body.model}'",
        )

    provider_client = _select_provider(request, team, body.model)

    try:
        return await provider_client.complete(body)
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
