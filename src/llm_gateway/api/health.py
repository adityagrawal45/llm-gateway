"""Liveness/readiness endpoints. No provider or routing logic here yet (Phase 0)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    config_teams: int


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    """Liveness probe. Also reports how many teams are currently loaded from config,
    which doubles as a cheap sanity check that hot reload is working."""
    loader = request.app.state.config_loader
    team_count = len(loader.current.teams) if loader is not None else 0
    return HealthResponse(status="ok", config_teams=team_count)
