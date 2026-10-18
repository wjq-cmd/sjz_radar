"""滑动窗口限流。

注意：这是**单进程内存**实现。用 uvicorn --workers >1 或多实例部署时，
每个 worker 各算各的，实际额度会被放大成 N 倍。那种场景要换成 Redis 版本
（INCR + EXPIRE，或用有序集合做滑动窗口）。
"""

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, window: int, limit: int) -> None:
        self.window = window
        self.limit = limit
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._ops = 0

    def allow(self, key: str) -> tuple[bool, int]:
        """返回 (是否放行, 建议重试秒数)。"""
        now = time.monotonic()
        with self._lock:
            self._maybe_purge()

            q = self._hits[key]
            cutoff = now - self.window
            while q and q[0] <= cutoff:
                q.popleft()

            if len(q) >= self.limit:
                retry_after = int(self.window - (now - q[0])) + 1
                return False, max(retry_after, 1)

            q.append(now)
            return True, 0

    def _maybe_purge(self) -> None:
        """定期清理空队列，避免 key 无限增长（调用方必须已持锁）。"""
        self._ops += 1
        if self._ops % 1000:
            return
        empty = [k for k, v in self._hits.items() if not v]
        for k in empty:
            self._hits.pop(k, None)
