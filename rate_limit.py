"""Per-user token buckets that gate chat messages before any LLM call.

In-memory and per process by design: the bot runs as one process, and a
restart resetting budgets is harmless. Keys are (guild_id, user_id).
"""
import time

# Enrolled members (or anyone when opt-in is off): steady chat use.
MEMBER_RATE = (10, 60.0)
# Not yet enrolled: enough to join, ask for help or check the current read.
UNENROLLED_RATE = (3, 60.0)


class TokenBucketLimiter:
    """Allow ``capacity`` requests per ``period`` seconds per key, refilling smoothly."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._buckets: dict[tuple, tuple[float, float]] = {}
        self._notified: set[tuple] = set()

    def allow(self, key, capacity: int, period: float) -> bool:
        if len(self._buckets) > 1024:
            self.prune()
        now = self._clock()
        tokens, last = self._buckets.get(key, (float(capacity), now))
        tokens = min(float(capacity), tokens + (now - last) * capacity / period)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            self._notified.discard(key)
            return True
        self._buckets[key] = (tokens, now)
        return False

    def should_notify(self, key) -> bool:
        """True once per throttled stretch, so a flood gets one notice, not one per message."""
        if key in self._notified:
            return False
        self._notified.add(key)
        return True

    def prune(self, max_idle: float = 3600.0) -> None:
        """Drop buckets idle long enough to have fully refilled."""
        cutoff = self._clock() - max_idle
        for key in [k for k, (_, last) in self._buckets.items() if last < cutoff]:
            del self._buckets[key]
            self._notified.discard(key)
