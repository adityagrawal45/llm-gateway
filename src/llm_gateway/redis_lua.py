"""
Small shared Lua scripts used across every Redis-backed counter in this
codebase (rate_limit/limiter.py's tokens/minute counter, budget/tracker.py's
daily/monthly spend counters). Factored out here rather than copy-pasted so
there's exactly one atomic increment-and-expire implementation to reason
about -- the whole point of using EVAL in the first place is not having to
re-derive "is this actually atomic?" per call site.

INCRBY_AND_EXPIRE_SCRIPT: atomically adds ARGV[1] to KEYS[1] and refreshes
its TTL to ARGV[2] seconds, in one round trip. This is a write with no
reject decision riding on it -- the caller already decided whether to allow
the request via a separate, cheap read-only check before the request ran
(see rate_limit/limiter.py's and budget/tracker.py's module docstrings for
why: the amount to record isn't known until after a provider call
completes, so there's nothing to gate atomically against at record time).
What atomicity buys here instead is against a *different* race: N concurrent
successful responses all calling this at once must not lose updates the way
a naive "GET current, then SET current+delta" would under concurrent writers
-- INCRBY is a single Redis command, so this is already atomic without EVAL,
but wrapping it with EXPIRE in one script keeps the "increment + refresh
TTL" step a single round trip, consistent with how the rest of this codebase
does atomic Redis operations.

Note: TTL is refreshed on every write, not just the first, so a key's actual
expiry drifts to last-write + TTL rather than window-start + TTL. Harmless
here -- window bucketing is done by the window identifier baked into the key
itself, not by TTL, which exists only for eventual cleanup.
"""

from __future__ import annotations

INCRBY_AND_EXPIRE_SCRIPT = """
local current = redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return current
"""
