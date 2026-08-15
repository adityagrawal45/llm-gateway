"""
Phase 3: per-team budget enforcement dependency, plus a self-service budget
status endpoint.

enforce_budget chains off enforce_rate_limit (api/rate_limit.py), the same
way that chains off require_team -- FastAPI's per-request dependency caching
means auth and rate-limit checks each still run exactly once even though
budget enforcement depends on both transitively. Returns 402 Payment
Required (not 429) on violation: 429 already has a specific, distinct
meaning in this codebase (too many requests/tokens in a time window,
api/rate_limit.py), and conflating "you're over budget" with "you're
sending requests too fast" would make the two failure modes harder for a
client to tell apart and react to differently (backoff-and-retry vs.
stop-until-next-billing-window). 402 is the closest standard HTTP status to
"this would cost money you don't have left."

GET /v1/teams/{team_name}/budget is self-service only, not an admin
endpoint: it's gated by the same require_team auth as every other route (no
separate admin role exists anywhere in this codebase), and a team may only
ever look up its own budget -- {team_name} must match the authenticated
team or the request 403s. There is no cross-team lookup capability by
design; if an admin view is ever needed, it should be a distinct
authentication mechanism, not a relaxation of this one.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from llm_gateway.api.auth import require_team
from llm_gateway.api.rate_limit import enforce_rate_limit
from llm_gateway.budget.tracker import BudgetTracker
from llm_gateway.config.schema import TeamConfig

router = APIRouter(tags=["budget"])


async def enforce_budget(
    request: Request,
    team: TeamConfig = Depends(enforce_rate_limit),  # noqa: B008 -- FastAPI's DI pattern, not a real bug
) -> TeamConfig:
    tracker: BudgetTracker = request.app.state.budget_tracker
    result = await tracker.check_budget(
        team.name, team.budget.daily_usd, team.budget.monthly_usd
    )
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "budget_exceeded",
                "window": result.exceeded,
                "current_usd": round(result.current_usd, 6),
                "limit_usd": result.limit_usd,
                "reset_at": result.reset_at.isoformat() if result.reset_at else None,
            },
        )
    return team


class BudgetWindowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spend_usd: float
    limit_usd: float
    remaining_usd: float
    reset_at: dt.datetime


class BudgetStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: str
    daily: BudgetWindowResponse
    monthly: BudgetWindowResponse


@router.get("/v1/teams/{team_name}/budget", response_model=BudgetStatusResponse)
async def get_budget_status(
    team_name: str,
    request: Request,
    team: TeamConfig = Depends(require_team),  # noqa: B008 -- FastAPI's DI pattern, not a real bug
) -> BudgetStatusResponse:
    # Self-service only -- see module docstring. Deliberately does not chain
    # off enforce_rate_limit/enforce_budget: polling your own budget status
    # shouldn't itself consume a request-count credit or be blocked by being
    # over budget (that would make it impossible to see *why* you're
    # rejected once you actually are).
    if team_name != team.name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cannot view another team's budget",
        )

    tracker: BudgetTracker = request.app.state.budget_tracker
    daily, monthly = await tracker.get_status(
        team.name, team.budget.daily_usd, team.budget.monthly_usd
    )
    return BudgetStatusResponse(
        team=team.name,
        daily=BudgetWindowResponse(**daily.__dict__),
        monthly=BudgetWindowResponse(**monthly.__dict__),
    )
