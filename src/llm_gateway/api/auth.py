"""
API key auth, implemented as a FastAPI dependency rather than ASGI
middleware -- it needs `request.app.state.config_loader` (only reliably
available once routing has started) and its failure mode is "this specific
route needs a resolved team", which `Depends()` + HTTPException expresses
more directly than middleware would. Wire it into any route that requires
an authenticated team via `team: TeamConfig = Depends(require_team)`.

Accepts either `Authorization: Bearer <team_api_key>` or `X-API-Key: <team_api_key>`.
The key is looked up against the *current* hot-reloaded config on every
request (via GatewayConfig.team_by_api_key), so revoking/rotating a team's
key in config.yaml takes effect without a restart.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from llm_gateway.config.schema import TeamConfig


def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    if x_api_key:
        return x_api_key
    return None


async def require_team(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TeamConfig:
    api_key = _extract_api_key(authorization, x_api_key)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key (use 'Authorization: Bearer <key>' or 'X-API-Key: <key>')",
        )

    loader = request.app.state.config_loader
    team = loader.current.team_by_api_key(api_key)
    if team is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    request.state.team = team
    return team
