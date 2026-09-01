"""
The core gateway route: POST /v1/chat/completions.

Phase 1 scope: validate the team is allowed to use the requested model,
pick the first allowed provider that can actually serve it, call that
provider once, and return its normalized response.

Phase 2 scope: enforce_rate_limit (api/rate_limit.py) gates the route before
any of the above -- a request that will go on to 403 (model/provider not
allowed) still consumes a request-count credit, so probing disallowed
models isn't a way to dodge the limit. Token usage is only known once the
provider responds, so it's recorded into the limiter here, after a
successful call, not inside enforce_rate_limit itself.

Phase 3 scope: enforce_budget (api/budget.py) chains off enforce_rate_limit,
so a request now clears auth -> rate limit -> budget, in that order, before
reaching any of the model/provider checks. USD cost, like token usage, isn't
known until the provider responds, so it's computed and recorded here too,
right alongside the token-usage recording.

Phase 4 scope: provider fallback. _select_providers now returns *every*
allowed provider that can serve the model, still in team.allowed_providers
order, instead of just the first. The route tries them in that order,
catching ProviderError and moving on to the next candidate instead of
502-ing immediately -- the first successful response wins. Only if every
candidate fails does the route 502, with each provider's failure reason
included so the caller isn't left guessing which upstream(s) were down.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from llm_gateway.api.budget import enforce_budget
from llm_gateway.api.schemas import ChatCompletionRequest, ChatCompletionResponse
from llm_gateway.budget.pricing import compute_cost_usd
from llm_gateway.config.schema import TeamConfig
from llm_gateway.providers.base import ProviderClient, ProviderError

router = APIRouter(tags=["chat"])


def _select_providers(request: Request, team: TeamConfig, model: str) -> list[ProviderClient]:
    """Walk the team's allowed providers in the order configured, and return
    every one whose ProviderClient claims to support the requested model, in
    that same order. That order is the fallback order: the route tries them
    one at a time and stops at the first success."""
    registry = request.app.state.provider_registry
    candidates: list[ProviderClient] = []
    for provider in team.allowed_providers:
        try:
            candidate = registry.get(provider)
        except KeyError:
            continue
        if model in candidate.supported_models:
            candidates.append(candidate)

    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"model '{model}' is not served by any provider team '{team.name}' "
                "is allowed to use"
            ),
        )
    return candidates


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    team: TeamConfig = Depends(enforce_budget),  # noqa: B008 -- FastAPI's DI pattern, not a real bug
) -> ChatCompletionResponse:
    if body.model not in team.allowed_models:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"team '{team.name}' is not allowed to use model '{body.model}'",
        )

    candidates = _select_providers(request, team, body.model)

    response: ChatCompletionResponse | None = None
    failures: list[str] = []
    for provider_client in candidates:
        try:
            response = await provider_client.complete(body)
            break
        except ProviderError as exc:
            failures.append(f"{provider_client.name}: {exc}")

    if response is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"all providers failed for model '{body.model}': " + "; ".join(failures),
        )

    # Only a successful call recorded tokens/spend -- a failed call consumed
    # no billable tokens as far as the gateway can tell. The request-count
    # credit, by contrast, was already charged pre-call by enforce_rate_limit
    # regardless of how this turns out.
    await request.app.state.rate_limiter.record_tokens(team.name, response.usage.total_tokens)

    cost_usd = compute_cost_usd(body.model, response.usage)
    await request.app.state.budget_tracker.record_spend(team.name, cost_usd)

    return response
