import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict


class SlidingWindowLimiter:
    """Small single-process limiter for the single API worker deployment.

    Production deployments with multiple API replicas can replace this with a
    shared store without changing the route contract.
    """

    def __init__(self) -> None:
        self._events: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            if len(self._events) > 10000:
                self._events = defaultdict(deque, {k: v for k, v in self._events.items() if v})
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


auth_limiter = SlidingWindowLimiter()
