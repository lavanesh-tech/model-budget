"""
Distributed rate limiting via Redis: an atomic sliding-window-log
limiter, implemented as a single Lua script so the "evict expired
entries, count, and conditionally add" sequence executes as one
indivisible operation on the Redis server -- no read-then-write race is
possible, and this is correct across any number of concurrent FastAPI
instances, since Redis (not any single process) is the shared state.

Algorithm (sliding window log, via a per-team Redis sorted set): each
allowed request adds one member to a sorted set keyed by team, scored by
a Redis-server-side timestamp (via the Lua TIME command, not the
calling application's clock -- this deliberately avoids clock-skew
issues between multiple app instances, which a client-timestamped
approach would not). Before counting, every member older than
`window_seconds` is evicted (ZREMRANGEBYSCORE). If the remaining count
is below the limit, the new member is added and the request is allowed;
otherwise it is rejected and nothing is added. This is a TRUE sliding
window -- unlike a fixed-window counter, it does not allow a burst of up
to 2x the limit at a window boundary.

Member uniqueness: each call's Lua ZADD member is the caller-supplied
`member` string, expected to be the request's own correlation ID (see
app.logging_config / app.main.RequestIDMiddleware) -- already unique per
request, so no additional ID generation is needed here.

Fail-closed policy (explicit, tested): if Redis cannot be reached or
errors out, RateLimitUnavailableError is raised. The caller (see
app.api.chat_completions) MUST treat this as a reason to reject the
request with a safe 503 -- NEVER as license to allow the request through
unlimited. This is a deliberate choice for a budget-protection gateway:
an unreachable rate limiter is exactly the situation where uncontrolled
request volume could do the most damage (unbounded OpenAI spend), so
"fail open" would defeat the entire purpose of this component. A
non-financial, purely cosmetic feature might reasonably choose to fail
open instead -- this one does not.

No secrets in scope: this module never touches API keys, prompts, or
completions. The Redis connection URL (which may embed an AUTH token for
managed Redis such as AWS ElastiCache) is handled exclusively by
app.config as a SecretStr and app.main's lifespan wiring -- it is never
passed into or logged by this module.
"""

import time
from dataclasses import dataclass
from typing import Protocol

from redis.exceptions import RedisError

_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local window_seconds = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]

local time_result = redis.call('TIME')
local now = tonumber(time_result[1]) + (tonumber(time_result[2]) / 1000000)
local window_start = now - window_seconds

redis.call('ZREMRANGEBYSCORE', key, '-inf', '(' .. tostring(window_start))
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, (window_seconds + 1) * 1000)
    return {1, count + 1, limit, 0}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_score = now
    if #oldest > 0 then
        oldest_score = tonumber(oldest[2])
    end
    return {0, count, limit, oldest_score}
end
"""


class RateLimitValidationError(ValueError):
    """Raised when a rate-limit input fails validation before any Redis call executes."""


class RateLimitUnavailableError(RuntimeError):
    """Raised when the Redis backend cannot be reached or errors out.
    Callers MUST fail closed (reject the request, e.g. with a safe 503)
    -- see the module docstring's fail-closed policy.
    """


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    current_count: int
    window_seconds: int
    reset_at: float  # unix timestamp (float seconds)

    @property
    def remaining(self) -> int:
        return max(self.limit - self.current_count, 0)

    @property
    def retry_after_seconds(self) -> int:
        return max(int(self.reset_at - time.time()), 0)


class RateLimiter(Protocol):
    async def check(self, *, team_id: str, window_seconds: int, limit: int, member: str) -> RateLimitResult: ...


def _validate_inputs(team_id: str, window_seconds: int, limit: int, member: str) -> None:
    if not isinstance(team_id, str) or not team_id.strip():
        raise RateLimitValidationError("team_id must be a non-blank str")
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or window_seconds < 1:
        raise RateLimitValidationError("window_seconds must be a plain int >= 1")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise RateLimitValidationError("limit must be a plain int >= 1")
    if not isinstance(member, str) or not member.strip():
        raise RateLimitValidationError("member must be a non-blank str")


class RedisRateLimiter:
    """Production RateLimiter, backed by a real (or fake-for-tests)
    async Redis client. The client itself is constructed once, at
    application startup (see app.main's lifespan), and injected here --
    this class never creates or closes a connection itself.
    """

    def __init__(self, client, *, key_prefix: str = "ratelimit:") -> None:
        self._client = client
        self._key_prefix = key_prefix
        self._script = client.register_script(_SLIDING_WINDOW_LUA)

    async def check(self, *, team_id: str, window_seconds: int, limit: int, member: str) -> RateLimitResult:
        _validate_inputs(team_id, window_seconds, limit, member)
        key = f"{self._key_prefix}{team_id}"
        try:
            raw = await self._script(keys=[key], args=[window_seconds, limit, member])
        except RedisError as exc:
            raise RateLimitUnavailableError("rate limiter backend unavailable") from exc
        # asyncio.CancelledError and any exception type not a RedisError
        # subclass is intentionally NOT caught above -- it propagates.

        allowed_flag, current_count, returned_limit, oldest_score = raw
        allowed = bool(int(allowed_flag))
        current_count = int(current_count)
        returned_limit = int(returned_limit)
        oldest_score = float(oldest_score)

        reset_at = oldest_score + window_seconds if not allowed else time.time() + window_seconds
        return RateLimitResult(
            allowed=allowed,
            limit=returned_limit,
            current_count=current_count,
            window_seconds=window_seconds,
            reset_at=reset_at,
        )
