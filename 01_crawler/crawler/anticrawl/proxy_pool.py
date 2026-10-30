"""代理池：健康检查、轮换、失败下线、自动恢复。

三种轮换策略：
- per_request  ：每个请求换一个出口 IP（最隐蔽，但同站 Cookie 会漂，慎用）
- per_session  ：一次会话固定一个 IP（推荐，配合 Cookie 才有「同一个人」的连续性）
- sticky_5min  ：同一个 host 在 5 分钟内固定同一 IP（长会话/分页场景最稳）

失败处理：连续失败到阈值就把代理扔进 quarantine，冷却后自动回归，
所以池子会「自己修」，长时间跑不会全池变黑。
"""
from __future__ import annotations

import queue
import random
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class ProxyNode:
    url: str
    fail_count: int = 0
    ok_count: int = 0
    last_ok_ts: float = 0.0
    last_check_ts: float = 0.0
    quarantine_until: float = 0.0
    bound_host: str | None = None
    bound_at: float = 0.0

    @property
    def alive(self) -> bool:
        return time.time() >= self.quarantine_until

    @property
    def latency_hint(self) -> float:
        """越小越优先：连续成功过、且最近成功的排前面。"""
        if self.ok_count == 0:
            return 1e6
        return 1.0 / self.ok_count + max(0.0, time.time() - self.last_ok_ts) * 1e-6

    def __str__(self) -> str:
        p = urlparse(self.url)
        # 打印时脱敏密码，日志里不允许出现凭据
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        return f"{p.scheme}://{netloc}"


class ProxyPool:
    def __init__(self, providers: list[str] | None = None, *, rotate: str = "per_session",
                 max_fail_count: int = 3, cooldown_sec: float = 180.0):
        self.nodes = [ProxyNode(url=u.strip()) for u in (providers or []) if u.strip()]
        self.rotate = rotate
        self.max_fail_count = max_fail_count
        self.cooldown_sec = cooldown_sec
        self._lock = threading.Lock()
        self._rr = 0

    # ---------- 基本属性 ----------
    def __bool__(self) -> bool:
        return bool(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    def alive_nodes(self) -> list[ProxyNode]:
        return [n for n in self.nodes if n.alive]

    def stats(self) -> dict:
        return {
            "total": len(self.nodes),
            "alive": len(self.alive_nodes()),
            "quarantined": len(self.nodes) - len(self.alive_nodes()),
            "best": str(min(self.alive_nodes(), key=lambda n: n.latency_hint)) if self.alive_nodes() else None,
        }

    # ---------- 选路 ----------
    def acquire(self, host: str | None = None) -> ProxyNode | None:
        with self._lock:
            pool = self.alive_nodes()
            if not pool:
                return None

            if self.rotate == "sticky_5min" and host:
                for n in pool:
                    if n.bound_host == host and time.time() - n.bound_at < 300:
                        return n
                node = min(pool, key=lambda n: n.latency_hint)
                node.bound_host, node.bound_at = host, time.time()
                return node

            if self.rotate == "per_session":
                # 会话内优先复用已绑定且健康的节点
                bound = [n for n in pool if n.bound_host == host]
                if bound:
                    return bound[0]
                node = min(pool, key=lambda n: n.latency_hint)
                if host:
                    node.bound_host, node.bound_at = host, time.time()
                return node

            # per_request：轮转
            self._rr = (self._rr + 1) % len(pool)
            return pool[self._rr]

    # ---------- 反馈 ----------
    def report_ok(self, node: ProxyNode | None) -> None:
        if not node:
            return
        with self._lock:
            node.ok_count += 1
            node.fail_count = 0
            node.last_ok_ts = time.time()
            node.last_check_ts = time.time()

    def report_fail(self, node: ProxyNode | None, *, hard: bool = False) -> None:
        """hard=True 表示确认是代理本身的问题（连接拒绝/超时），立即隔离。"""
        if not node:
            return
        with self._lock:
            node.fail_count += 1
            node.last_check_ts = time.time()
            if hard or node.fail_count >= self.max_fail_count:
                node.quarantine_until = time.time() + self.cooldown_sec * min(node.fail_count, 5)
                node.bound_host = None

    def revive_all(self) -> None:
        """清空隔离状态（运维手动救池）。"""
        with self._lock:
            for n in self.nodes:
                n.quarantine_until = 0.0
                n.fail_count = 0

    def requests_proxies(self, node: ProxyNode | None) -> dict | None:
        if node is None:
            return None
        return {"http": node.url, "https": node.url}

    def curl_proxy_arg(self, node: ProxyNode | None) -> str | None:
        return node.url if node else None

    @classmethod
    def from_settings(cls, cfg) -> "ProxyPool":
        return cls(
            providers=list(cfg.providers or []),
            rotate=cfg.rotate,
            max_fail_count=cfg.max_fail_count,
        )
