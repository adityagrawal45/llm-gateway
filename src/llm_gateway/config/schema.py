"""
Pydantic models describing the shape of config/config.yaml.

Keeping this as a strict schema (extra="forbid") means a typo in the YAML
file fails fast and loud on load/reload, instead of silently being ignored.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Provider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


class RateLimitConfig(BaseModel):
    """Per-team request/token rate limits, enforced via Redis in a later phase."""

    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(gt=0, description="Max requests/min for this team")
    tokens_per_minute: int = Field(gt=0, description="Max tokens/min for this team")
    burst: int = Field(default=0, ge=0, description="Extra burst allowance above the steady rate")


class BudgetConfig(BaseModel):
    """Per-team spend caps, enforced in a later phase."""

    model_config = ConfigDict(extra="forbid")

    daily_usd: float = Field(gt=0, description="Max USD spend per rolling day")
    monthly_usd: float = Field(gt=0, description="Max USD spend per rolling month")

    @model_validator(mode="after")
    def _monthly_at_least_daily(self) -> "BudgetConfig":
        if self.monthly_usd < self.daily_usd:
            raise ValueError("monthly_usd must be >= daily_usd")
        return self


class TeamConfig(BaseModel):
    """A single team/tenant using the gateway."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    api_key: str = Field(min_length=8, description="Key clients present to the gateway (not the provider key)")
    allowed_providers: list[Provider] = Field(min_length=1)
    allowed_models: list[str] = Field(min_length=1)
    rate_limit: RateLimitConfig
    budget: BudgetConfig


class GatewayConfig(BaseModel):
    """Top-level config/config.yaml schema."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, description="Config schema version, for future migrations")
    teams: list[TeamConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_team_names(self) -> "GatewayConfig":
        names = [t.name for t in self.teams]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"duplicate team name(s) in config: {sorted(dupes)}")
        return self

    def team_by_api_key(self, api_key: str) -> TeamConfig | None:
        return next((t for t in self.teams if t.api_key == api_key), None)
