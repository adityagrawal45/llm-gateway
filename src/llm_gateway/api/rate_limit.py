"""
FastAPI dependency enforcing per-team rate limits.

Chains directly off require_team (rather than re-parsing headers itself),
so FastAPI's per-request dependency caching means auth resolves exactly
once even though both this and require_team ask for it. Deliberately runs
*before* chat.py's model/provider-allowed checks: a request a team turns
out not to be allowed to make still consumes a request-count credit, so
probing disallowed models isn't a way to dodge the rate limit.

Checks both dimensions pre-call and raises 429 with a Retry-After header on
violation -- no silent throttling, the client always learns why it was
rejected and how long to back off. Does not record token usage itself:
that only happens after chat.py gets a successful provider response (see
chat.py), since token counts aren't known until then.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from llm_gateway.api.auth import require_team
from llm_gateway.config.schema import TeamConfig
from llm_gateway.rate_limit.limiter import RateLimiter


async def enforce_rate_limit(
    request: Request,
    team: TeamConfig = Depends(require_team),  # noqa: B008 -- FastAPI's DI pattern, not a real bug
) -> TeamConfig:
    limiter: RateLimiter = request.app.state.rate_limiter
    rl = team.rate_limit

    req_result = await limiter.check_and_increment_requests(
        team.name, rl.requests_per_minute, rl.burst
    )
    if not req_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"requests/minute rate limit exceeded for team '{team.name}' "
                f"({req_result.current}/{req_result.limit})"
            ),
            headers={"Retry-After": str(req_result.retry_after_seconds)},
        )

    tok_result = await limiter.check_tokens(team.name, rl.tokens_per_minute, rl.burst)
    if not tok_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"tokens/minute rate limit exceeded for team '{team.name}' "
                f"({tok_result.current}/{tok_result.limit})"
            ),
            headers={"Retry-After": str(tok_result.retry_after_seconds)},
        )

    return team
