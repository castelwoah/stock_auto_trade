from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Thread-safe Token Bucket rate limiter.

    KIS REST limit: 20 req/s → capacity=20, refill_rate=20.0.
    """

    capacity: int
    refill_rate: float
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> None:
        if tokens <= 0 or tokens > self.capacity:
            raise ValueError(f"tokens must be in (0, {self.capacity}]")
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(
                    float(self.capacity),
                    self._tokens + elapsed * self.refill_rate,
                )
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.refill_rate
            time.sleep(wait)

    def try_acquire(self, tokens: int = 1) -> bool:
        if tokens <= 0 or tokens > self.capacity:
            raise ValueError(f"tokens must be in (0, {self.capacity}]")
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(
                float(self.capacity),
                self._tokens + elapsed * self.refill_rate,
            )
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False
