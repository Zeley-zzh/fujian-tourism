"""反爬对抗层：指纹伪装 / 代理池 / 验证码识别 / 节流 / robots 兼容。

设计原则：
1. 每类对抗手段独立成文件，通过 `AntiCrawlClient` 组合，可单独开关。
2. 所有手段都可降级——装不到 curl_cffi 就退回 httpx，代理挂了就直连，
   验证码识别失败就落人工队列，绝不让采集整体崩掉。
3. 反爬是「通过率」问题而不是「有没有」问题，所以每个组件都暴露统计口径。
"""
from __future__ import annotations

from .client import AntiCrawlClient, FetchResult
from .fingerprint import FingerprintSpoofer, browser_headers
from .proxy_pool import ProxyPool
from .throttle import HostThrottle
from .captcha import CaptchaSolver
from .robots import RobotsGate

__all__ = [
    "AntiCrawlClient",
    "FetchResult",
    "FingerprintSpoofer",
    "browser_headers",
    "ProxyPool",
    "HostThrottle",
    "CaptchaSolver",
    "RobotsGate",
]
