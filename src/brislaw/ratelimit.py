"""Token bucket rate limiter for CourtListener API calls.

Enforces the 5,000 requests/hour free tier limit with thread-safe
token bucket algorithm.
"""

from __future__ import annotations

import time
import threading


class TokenBucket:
    """Token bucket rate limiter.

    Limits requests to a specified capacity per time window.
    Default: 5,000 requests per hour (CourtListener free tier).

    Thread-safe via internal lock.
    """

    def __init__(
        self,
        capacity: int = 5000,
        refill_rate: float = 5000 / 3600,
    ) -> None:
        """Initialize the token bucket.

        Args:
            capacity: Maximum number of tokens (burst limit).
            refill_rate: Tokens added per second.
        """
        self.capacity = capacity
        self.tokens: float = float(capacity)
        self.refill_rate = refill_rate
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> None:
        """Block until the requested number of tokens are available.

        Args:
            tokens: Number of tokens to consume (default 1).
        """
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
            time.sleep(0.1)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_rate,
        )
        self.last_refill = now

    @property
    def remaining(self) -> int:
        """Current number of available tokens (after refill)."""
        with self._lock:
            self._refill()
            return int(self.tokens)
