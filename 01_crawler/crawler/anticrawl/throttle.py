"""节流器：per-host 串行 + 随机间隔 + 突发长睡 + 指数退避。

为什么不是简单的 time.sleep(2)：
1. 固定间隔本身就是机器人特征 —— 人类访问间隔服从长尾分布；
2. 服务端常按「每秒请求数」+「每 IP 总请求数」双窗口限流，
   所以既要有单次抖动，也要有周期性长睡；
3. 429/403 之后必须退避，且退避要带随机，否则恢复时刻又是整齐的。

同时提供 token bucket，供需要短时并发的场景（同一 host 多线程）使用。
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass


def human_interval(lo: float, hi: float) -> float:
    """长尾分布：多数落在 [lo, hi] 中段，偶尔出现一个明显更长的间隔。"""
    if hi <= lo:
        return lo
    base = random.uniform(lo, hi)
    if random.random() < 0.12:                       # 12% 概率「走神」
        base += random.uniform(hi, hi * 3.0)
    return base


@dataclass
class ThrottleStats:
    requests: int = 0
    slept_sec: float = 0.0
    burst_sleeps: int = 0
    backoffs: int = 0


class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: float = 1.0) -> float:
        """返回需要等待的秒数，并已扣减。"""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return 0.0
            need = (n - self._tokens) / self.rate
            self._tokens = 0.0
            self._last = now + need
            return need

    def wait(self, n: float = 1.0) -> float:
        d = self.take(n)
        if d > 0:
            time.sleep(d)
        return d


class HostThrottle:
    """按 host 分别计时：A 站慢不影响 B 站。"""

    def __init__(self, *, min_interval: float = 2.0, max_interval: float = 4.5,
                 burst_limit: int = 3, burst_sleep: float = 15.0,
                 per_host_concurrency: int = 1):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.burst_limit = burst_limit
        self.burst_sleep = burst_sleep
        self.per_host_concurrency = per_host_concurrency
        self._last: dict[str, float] = {}
        self._count: dict[str, int] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.stats = ThrottleStats()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            if host not in self._locks:
                self._locks[host] = threading.Lock()
            return self._locks[host]

    def acquire(self, host: str) -> None:
        """在真正发请求之前调用；内部会阻塞到合适时刻。"""
        lock = self._lock_for(host)
        lock.acquire()  # per_host_concurrency=1 时为严格串行
        try:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            wait = human_interval(self.min_interval, self.max_interval) - (now - last)
            if wait > 0:
                time.sleep(wait)
                self.stats.slept_sec += wait

            n = self._count.get(host, 0) + 1
            self._count[host] = n
            if self.burst_limit and n % self.burst_limit == 0:
                jitter = random.uniform(0, self.burst_sleep * 0.4)
                time.sleep(self.burst_sleep + jitter)
                self.stats.slept_sec += self.burst_sleep + jitter
                self.stats.burst_sleeps += 1

            self._last[host] = time.monotonic()
            self.stats.requests += 1
        finally:
            lock.release()

    def backoff(self, host: str, attempt: int, base: float = 1.8) -> float:
        """指数退避 + 抖动。attempt 从 0 开始。返回实际睡眠秒数。"""
        delay = (base ** attempt) * random.uniform(0.7, 1.3)
        delay = min(delay, 300.0)
        time.sleep(delay)
        self.stats.slept_sec += delay
        self.stats.backoffs += 1
        return delay

    def cool_down_for_host(self, host: str, seconds: float) -> None:
        """被判定为封禁时，把该 host 的「下一次可请求时刻」推后。"""
        with self._guard:
            self._last[host] = time.monotonic() + seconds
