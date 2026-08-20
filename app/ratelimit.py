"""A small in-process sliding-window rate limiter.

Good enough for a single-worker deployment, which is what this server assumes
(the collaboration hub is in-memory too). Put a real limiter in front of it if
you ever run more than one worker.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from .config import settings


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits[key]
        self._prune(bucket, now)

        if len(bucket) >= self.limit:
            return False

        bucket.append(now)

        # Keep the dict from growing without bound on a busy or hostile host.
        if len(self._hits) > self.max_keys:
            self._evict_empty(now)
        return True

    def retry_after(self, key: str) -> int:
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        return max(1, int(self.window - (time.monotonic() - bucket[0])))

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _evict_empty(self, now: float) -> None:
        for key in list(self._hits):
            self._prune(self._hits[key], now)
            if not self._hits[key]:
                del self._hits[key]


def client_ip(request: Request) -> str:
    """Best-effort client address.

    ``X-Forwarded-For`` is only consulted when TRUST_PROXY_HEADERS says a
    proxy is in front. On a directly exposed server the header is attacker
    controlled, and honouring it would let anyone mint a fresh rate-limit
    bucket per request.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(limiter: RateLimiter, request: Request, scope: str) -> None:
    key = f"{scope}:{client_ip(request)}"
    if not limiter.allow(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests, slow down",
            headers={"Retry-After": str(limiter.retry_after(key))},
        )
