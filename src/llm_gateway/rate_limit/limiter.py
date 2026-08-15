"""
Redis-backed per-team rate limiter, enforced along two dimensions:
requests/minute and tokens/minute, both read from TeamConfig.rate_limit
(config/schema.py) fresh on every check -- there is no cached copy of the
limit here, only per-call arguments, so a hot-reloaded config change takes
effect on the very next request (see "Hot reload" note below).

## Algorithm: fixed 60s window counter, not token-bucket

Each check is scoped to a window key of `floor(now / 60)`. This needs only
an atomic increment-with-TTL per window, which is cheaper (one key, one
round trip) and maps more directly to "N requests per minute" than a
token-bucket's persisted refill-rate/fractional-tokens state would.

Trade-off, deliberately accepted: this is a *fixed* window, not a weighted
sliding log (which would need a sorted set of per-request timestamps,
trimmed on every check -- more memory and a second round trip). A fixed
window lets a client burst up to ~2x the steady-state limit right at a
window boundary (e.g. hit the limit at :59.9, then hit it again at :00.1).
`RateLimitConfig.burst` is treated as the allowance for exactly this edge
effect -- added on top of the steady-state limit -- rather than as a
separate mechanism, so the implementation stays a single counter+TTL.

## Requests/minute: atomic check-and-increment via one Lua EVAL

This is the one place in the project where a subtle bug means a team
silently gets 2x its limit under load: a naive "GET current, compare,
INCR if under" is two round trips with a gap in between where N concurrent
requests can all read the same pre-increment value and all decide they're
under the limit. Redis executes a single EVAL as one atomic unit (its
command execution is single-threaded), so wrapping "increment, then set TTL
if this was the first write to the window" in one script closes that gap
entirely -- no two concurrent callers can ever observe the same
pre-increment count. See test_concurrent_burst_atomicity in
tests/test_rate_limit.py, which proves this with a 20-way concurrent gather
against a limit of 10 and asserts exactly 10 are allowed.

The script still increments the request that tips the count over the limit
(rather than checking-then-skipping-the-increment), specifically so the
*next* request in the same window correctly sees an already-over count
instead of being allowed to slip in first.

## Tokens/minute: check-before (read-only), record-after (increment)

Token cost isn't known until the provider responds (ChatCompletionResponse
.usage), and there's no tokenizer dependency in this repo to estimate it
pre-call, so a reserve-then-adjust scheme would need a guessed reservation
plus a compensating "release" path for when the provider call fails after
reserving -- meaningful complexity for an estimate that could be wrong in
either direction anyway.

Instead: pre-call, read (don't increment) the window's token counter and
reject only if it's already at/over the limit from *prior* requests this
window; post-call (api/chat.py, after a successful provider response),
increment by the actual usage via the shared INCRBY_AND_EXPIRE_SCRIPT
(redis_lua.py -- also used by budget/tracker.py's spend recording, so
there's one atomic-increment implementation in the codebase, not two).
Consequence, documented rather than hidden: a team can overshoot
tokens/minute by up to one in-flight request's worth of tokens before the
limiter catches it on the request *after* that one. This is judged an
acceptable trade-off against the complexity/failure-mode cost of
reservation, given the repo's current constraints (no estimator,
optional/nullable max_tokens on the request). budget/tracker.py makes the
same call for the same reason -- see its module docstring.

## Hot reload

No extra wiring is needed for a config edit to take effect: callers always
pass in `team.rate_limit.requests_per_minute` / `.tokens_per_minute` /
`.burst` freshly resolved from the hot-reloaded config on every request (see
api/rate_limit.py). The counters themselves store only a count, never a
limit, so there's nothing here to invalidate on reload. The only subtlety:
a request that already passed its pre-call check right before a reload
lowers the limit will still complete and still record its tokens into the
window -- that's fine, since the very next request re-reads the new,
already-lower limit and compares it against the same counter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import redis.asyncio as redis

from llm_gateway.redis_lua import INCRBY_AND_EXPIRE_SCRIPT

WINDOW_SECONDS = 60

# KEYS[1] = requests counter key for this team+window
# ARGV[1] = effective limit (requests_per_minute + burst) -- unused inside
#           the script itself (the allow/reject decision is made by the
#           Python caller, which needs the post-increment count anyway to
#           compute Retry-After), kept as an argument only for symmetry /
#           future use (e.g. server-side short-circuiting).
# ARGV[2] = window TTL in seconds
#
# Atomically increments the counter, sets its TTL only on the first write to
# this window (so later writes don't keep resetting the window's expiry past
# window_start + TTL), and returns [new_count, ttl_remaining] in one round
# trip -- see module docstring for why this must be a single EVAL.
INCR_AND_CHECK_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""

# Post-call write only for the tokens/minute counter -- record_tokens() has
# no reject decision riding on it (that already happened pre-call via
# check_tokens()'s read), so there's no check-then-write race to close here.
# Shared with budget/tracker.py's spend recording -- see redis_lua.py for
# why this is factored out rather than redefined per module.
INCRBY_SCRIPT = INCRBY_AND_EXPIRE_SCRIPT


def _window_start(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // WINDOW_SECONDS)


def _requests_key(team: str, window: int) -> str:
    return f"ratelimit:{team}:requests:{window}"


def _tokens_key(team: str, window: int) -> str:
    return f"ratelimit:{team}:tokens:{window}"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int
    limit: int
    current: int


class RateLimiter:
    def __init__(self, redis_client: redis.Redis):
        self._redis = redis_client
        self._incr_and_check = redis_client.register_script(INCR_AND_CHECK_SCRIPT)
        self._incrby = redis_client.register_script(INCRBY_SCRIPT)

    async def check_and_increment_requests(
        self, team: str, limit: int, burst: int = 0
    ) -> RateLimitResult:
        """Atomic pre-call check for the requests/minute dimension. Always
        increments, even on the request that goes over -- see module docstring."""
        window = _window_start()
        key = _requests_key(team, window)
        effective_limit = limit + burst
        current, ttl = await self._incr_and_check(
            keys=[key], args=[effective_limit, WINDOW_SECONDS]
        )
        current = int(current)
        ttl = int(ttl) if int(ttl) > 0 else WINDOW_SECONDS
        return RateLimitResult(
            allowed=current <= effective_limit,
            retry_after_seconds=ttl,
            limit=effective_limit,
            current=current,
        )

    async def check_tokens(self, team: str, limit: int, burst: int = 0) -> RateLimitResult:
        """Read-only pre-call check for the tokens/minute dimension -- does NOT
        increment. See module docstring for why tokens are check-before/
        record-after rather than reserve-then-adjust."""
        window = _window_start()
        key = _tokens_key(team, window)
        effective_limit = limit + burst
        raw = await self._redis.get(key)
        current = int(raw) if raw is not None else 0
        ttl = await self._redis.ttl(key)
        ttl = ttl if ttl and ttl > 0 else WINDOW_SECONDS
        return RateLimitResult(
            allowed=current < effective_limit,
            retry_after_seconds=ttl,
            limit=effective_limit,
            current=current,
        )

    async def record_tokens(self, team: str, tokens: int) -> None:
        """Post-call: add actual usage to this window's token counter. Called
        from api/chat.py only after a provider call succeeds -- see chat.py
        for why a failed call doesn't record anything."""
        if tokens <= 0:
            return
        window = _window_start()
        key = _tokens_key(team, window)
        await self._incrby(keys=[key], args=[tokens, WINDOW_SECONDS])
