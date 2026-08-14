"""
The core gateway route: POST /v1/chat/completions.

Phase 1 scope: validate the team is allowed to use the requested model,
pick the first allowed provider that can actually serve it, call that
provider once, and return its normalized response. No retries, no fallback
to a second provider; that's a later phase.

Phase 2 scope: enforce_rate_limit (api/rate_limit.py) gates the route before
any of the above -- a request that will go on to 403 (model/provider not
allowed) still consumes a request-count credit, so probing disallowed
models isn't a way to dodge the limit. Token usage is only known once the
provider responds, so it's recorded into the limiter here, after a
successful call, not inside enforce_rate_limit itself. No budget checks yet;
that's Phase 3.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from llm_gateway.api.rate_limit import enforce_rate_limit
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
    team: TeamConfig = Depends(enforce_rate_limit),  # noqa: B008 -- FastAPI's DI pattern, not a real bug
) -> ChatCompletionResponse:
    if body.model not in team.allowed_models:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"team '{team.name}' is not allowed to use model '{body.model}'",
        )

    provider_client = _select_provider(request, team, body.model)

    try:
        response = await provider_client.complete(body)
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # Only a successful call recorded tokens against the team's tokens/minute
    # window -- a failed call consumed no billable tokens as far as the
    # gateway can tell. The request-count credit, by contrast, was already
    # charged pre-call by enforce_rate_limit regardless of how this turns out.
    await request.app.state.rate_limiter.record_tokens(team.name, response.usage.total_tokens)

    return response
