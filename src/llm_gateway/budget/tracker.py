"""
Redis-backed per-team spend tracking, enforced along two windows: daily and
monthly, both read from TeamConfig.budget (config/schema.py) fresh on every
check -- same "no cached limit, only per-call arguments" shape as
rate_limit/limiter.py, so a hot-reloaded budget change takes effect on the
very next request (see "Hot reload" note below).

## Windowing: calendar-aligned, not rolling

Both windows are calendar boundaries in UTC -- "today" (UTC midnight to UTC
midnight) and "this month" (1st of the UTC month to the 1st of the next) --
rather than a rolling 24h/30d lookback from each request's timestamp.
Calendar alignment is simpler to reason about and matches how organizations
already think about a "daily" or "monthly" budget (it lines up with how a
provider's own invoice is typically itemized, by calendar month); a rolling
window is more precise moment-to-moment but is a materially worse answer to
"why was our budget considered exceeded at 3pm on the 14th" in a dispute --
a calendar window has one unambiguous, quotable answer ("cumulative spend
since 00:00 UTC on the 14th"), a rolling window's answer depends on exactly
which prior 24h of requests happened to fall inside the lookback at that
instant. Trade-off accepted: a team that spends its entire daily budget at
23:59 can spend it again one minute later at 00:01 -- the same boundary-burst
effect rate_limit/limiter.py's fixed-window counters have, and accepted for
the same reason (simplicity of a single counter + TTL over a weighted log).

Calendar day/month is computed in UTC specifically, not a team's local
timezone (this repo has no per-team timezone concept) -- documented here as
a known simplification, not silently assumed.

## Enforcement strategy: check-before / record-after, same as tokens/minute

Actual USD cost isn't known until the provider responds (it depends on
prompt_tokens and completion_tokens, and completion_tokens in particular
can't be bounded any better than the tokens/minute limiter could -- see
rate_limit/limiter.py's docstring for why a pre-call reservation using
max_tokens as a worst case was rejected there). Budget enforcement makes the
identical call, for the identical reason, and deliberately mirrors that
design rather than inventing a second concurrency strategy for what is
structurally the same problem: pre-call, read (don't write) the window's
spend counter and reject if already at/over the limit from prior requests
this window; post-call (api/chat.py, after a successful response), add the
actual computed cost (budget/pricing.py::compute_cost_usd). Consequence,
same shape as tokens/minute: a team can overshoot a budget window by up to
one in-flight request's worth of spend before the *next* request is
rejected. Accepted for consistency with the rest of the codebase and because
the alternative (reserve max_tokens-implied cost, refund the difference
after) has the same failure-mode cost Phase 2 already rejected: a null-able
max_tokens with no reliable input-token estimate either.

## Atomicity

The check is a plain read (GET) -- nothing to protect there, same as
check_tokens(). The write (record_spend) uses the same
INCRBY_AND_EXPIRE_SCRIPT Lua script rate_limit/limiter.py's record_tokens
uses (see redis_lua.py): a burst of concurrent successful responses all
recording spend at once cannot lose an update to a naive read-then-write
race, because INCRBY is a single atomic Redis command. See
test_concurrent_spend_recording_atomicity in tests/test_budget.py, which
fires N concurrent record_spend() calls of a fixed amount each and asserts
the total is exactly N times that amount.

## Storage: integer micro-dollars, not float USD

Redis counters here store an integer count of micro-dollars (1 USD =
1_000_000 micros), not a float dollar amount, specifically to avoid
compounding floating-point rounding error across many small per-request
INCRBYs over a day or a month -- integer INCRBY is exact; repeated float
addition is not guaranteed to be. Converted back to a USD float only at the
read boundary (BudgetStatus, check results).

## Hot reload

Same behavior and same rationale as rate_limit/limiter.py: TeamConfig.budget
.daily_usd/.monthly_usd are read fresh from the hot-reloaded config on every
check, and the Redis counters store only a spend amount, never a limit, so
there's nothing to invalidate here on reload. If a team's budget is lowered
mid-window, already-accumulated spend for that window is kept as-is -- the
new, lower limit is simply compared against it starting on the very next
request. Already-in-flight requests that passed their pre-call check right
before the reload still complete and still record their spend normally.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import redis.asyncio as redis

from llm_gateway.redis_lua import INCRBY_AND_EXPIRE_SCRIPT

MICROS_PER_USD = 1_000_000

BudgetWindow = Literal["daily", "monthly"]


def _to_micros(usd: float) -> int:
    return round(usd * MICROS_PER_USD)


def _to_usd(micros: int) -> float:
    return micros / MICROS_PER_USD


def _daily_window_id(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _monthly_window_id(now: dt.datetime) -> str:
    return now.strftime("%Y-%m")


def _daily_reset_at(now: dt.datetime) -> dt.datetime:
    return dt.datetime(now.year, now.month, now.day, tzinfo=dt.UTC) + dt.timedelta(days=1)


def _monthly_reset_at(now: dt.datetime) -> dt.datetime:
    if now.month == 12:
        return dt.datetime(now.year + 1, 1, 1, tzinfo=dt.UTC)
    return dt.datetime(now.year, now.month + 1, 1, tzinfo=dt.UTC)


def _spend_key(team: str, window: BudgetWindow, window_id: str) -> str:
    return f"budget:{team}:{window}:{window_id}"


@dataclass(frozen=True)
class BudgetCheckResult:
    allowed: bool
    exceeded: BudgetWindow | None
    current_usd: float
    limit_usd: float
    reset_at: dt.datetime | None


@dataclass(frozen=True)
class BudgetWindowStatus:
    spend_usd: float
    limit_usd: float
    remaining_usd: float
    reset_at: dt.datetime


class BudgetTracker:
    def __init__(self, redis_client: redis.Redis):
        self._redis = redis_client
        self._incrby = redis_client.register_script(INCRBY_AND_EXPIRE_SCRIPT)

    async def _get_spend_usd(self, team: str, window: BudgetWindow, now: dt.datetime) -> float:
        window_id = _daily_window_id(now) if window == "daily" else _monthly_window_id(now)
        key = _spend_key(team, window, window_id)
        raw = await self._redis.get(key)
        micros = int(raw) if raw is not None else 0
        return _to_usd(micros)

    async def check_budget(
        self, team: str, daily_limit_usd: float, monthly_limit_usd: float
    ) -> BudgetCheckResult:
        """Read-only pre-call check across both windows -- does NOT record
        anything. Daily is checked first; if it's already over, monthly isn't
        even read (either way the request is rejected, and the daily reason
        is more specific/actionable). A monthly-only overage (daily has room,
        monthly doesn't) is still caught, since daily passing falls through
        to the monthly check rather than short-circuiting to "allowed"."""
        now = dt.datetime.now(dt.UTC)

        daily_spend = await self._get_spend_usd(team, "daily", now)
        if daily_spend >= daily_limit_usd:
            return BudgetCheckResult(
                allowed=False,
                exceeded="daily",
                current_usd=daily_spend,
                limit_usd=daily_limit_usd,
                reset_at=_daily_reset_at(now),
            )

        monthly_spend = await self._get_spend_usd(team, "monthly", now)
        if monthly_spend >= monthly_limit_usd:
            return BudgetCheckResult(
                allowed=False,
                exceeded="monthly",
                current_usd=monthly_spend,
                limit_usd=monthly_limit_usd,
                reset_at=_monthly_reset_at(now),
            )

        return BudgetCheckResult(
            allowed=True,
            exceeded=None,
            current_usd=daily_spend,
            limit_usd=daily_limit_usd,
            reset_at=_daily_reset_at(now),
        )

    async def record_spend(self, team: str, amount_usd: float) -> None:
        """Post-call: add actual cost to both the daily and monthly counters.
        Called from api/chat.py only after a provider call succeeds and its
        cost has been computed (budget/pricing.py::compute_cost_usd)."""
        if amount_usd <= 0:
            return
        now = dt.datetime.now(dt.UTC)
        micros = _to_micros(amount_usd)

        daily_key = _spend_key(team, "daily", _daily_window_id(now))
        daily_ttl = int((_daily_reset_at(now) - now).total_seconds()) + 86_400  # +1 day buffer
        await self._incrby(keys=[daily_key], args=[micros, daily_ttl])

        monthly_key = _spend_key(team, "monthly", _monthly_window_id(now))
        monthly_ttl = int((_monthly_reset_at(now) - now).total_seconds()) + 2_678_400  # +31d buffer
        await self._incrby(keys=[monthly_key], args=[micros, monthly_ttl])

    async def get_status(
        self, team: str, daily_limit_usd: float, monthly_limit_usd: float
    ) -> tuple[BudgetWindowStatus, BudgetWindowStatus]:
        """Returns (daily, monthly) status for the self-service budget
        endpoint -- read-only, records nothing, safe to poll freely."""
        now = dt.datetime.now(dt.UTC)

        daily_spend = await self._get_spend_usd(team, "daily", now)
        monthly_spend = await self._get_spend_usd(team, "monthly", now)

        daily = BudgetWindowStatus(
            spend_usd=daily_spend,
            limit_usd=daily_limit_usd,
            remaining_usd=max(0.0, daily_limit_usd - daily_spend),
            reset_at=_daily_reset_at(now),
        )
        monthly = BudgetWindowStatus(
            spend_usd=monthly_spend,
            limit_usd=monthly_limit_usd,
            remaining_usd=max(0.0, monthly_limit_usd - monthly_spend),
            reset_at=_monthly_reset_at(now),
        )
        return daily, monthly
