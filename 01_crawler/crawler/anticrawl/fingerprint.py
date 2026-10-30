"""指纹伪装：让请求在网络层看起来就是一台真 Chrome。

三层伪装（缺一层就容易被 WAF 认出）：
1. TLS 指纹（JA3/JA4）—— 用 curl_cffi 的 `impersonate`，它用真实 Chrome 的
   ClientHello（cipher 顺序、扩展、椭曲线、ALPN）代替 Python 默认那套。
2. HTTP/2 帧指纹 —— curl_cffi 同时复刻 SETTINGS/WINDOW_UPDATE/优先级树，
   纯 requests/httpx 的 h2 实现与之差异明显。
3. 头部顺序与取值 —— 浏览器发头有固定顺序和一组必然存在的 sec-ch-ua*，
   这里按 Chrome 的顺序构造 OrderedDict。

只用标准库 + 可选 curl_cffi，装不上也能跑（降级为 httpx，TLS 指纹不伪装）。
"""
from __future__ import annotations

import random
from collections import OrderedDict

# ---- 真实 Chrome 版本与对应 UA / sec-ch-ua ----
CHROME_PROFILES = [
    {
        "impersonate": "chrome124",
        "version": "124.0.0.0",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    },
    {
        "impersonate": "chrome120",
        "version": "120.0.0.0",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    },
    {
        "impersonate": "chrome110",
        "version": "110.0.0.0",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"),
        "sec_ch_ua": '"Chromium";v="110", "Not A(Brand";v="24", "Google Chrome";v="110"',
    },
]

# Chrome 的头部发送顺序（顺序本身是指纹的一部分）
CHROME_HEADER_ORDER = [
    "Host", "Connection", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "Upgrade-Insecure-Requests", "User-Agent", "Accept", "Sec-Fetch-Site",
    "Sec-Fetch-Mode", "Sec-Fetch-User", "Sec-Fetch-Dest", "Referer",
    "Accept-Encoding", "Accept-Language", "Cookie",
]


def browser_headers(referer: str | None = None, *, accept_html: bool = True,
                    mobile: bool = False, extra: dict | None = None) -> OrderedDict:
    """按 Chrome 真实顺序生成请求头。

    referer 必须是同站页面的 URL —— 直接访问深链而不带 Referer 是典型爬虫特征。
    """
    prof = random.choice(CHROME_PROFILES)
    h: OrderedDict[str, str] = OrderedDict()
    h["sec-ch-ua"] = prof["sec_ch_ua"]
    h["sec-ch-ua-mobile"] = "?1" if mobile else "?0"
    h["sec-ch-ua-platform"] = '"Windows"'
    h["Upgrade-Insecure-Requests"] = "1"
    h["User-Agent"] = prof["ua"]
    h["Accept"] = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8,"
                   "application/signed-exchange;v=b3;q=0.7") if accept_html else "*/*"
    if referer:
        h["Referer"] = referer
        h["Sec-Fetch-Site"] = "same-origin"
    else:
        h["Sec-Fetch-Site"] = "none"
    h["Sec-Fetch-Mode"] = "navigate"
    h["Sec-Fetch-User"] = "?1"
    h["Sec-Fetch-Dest"] = "document"
    # 注意：不要自己压 gzip，交给底层库；这里声明即可
    h["Accept-Encoding"] = "gzip, deflate, br, zstd"
    h["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    if extra:
        h.update(extra)
    return h


class FingerprintSpoofer:
    """维护一个「浏览器会话」，同一 session 内 UA/指纹保持一致。

    为什么不能每个请求随机换 UA：同一 Cookie + 每请求换 UA 是最强的机器人信号。
    """

    def __init__(self, impersonate: str = "chrome124", enable_http2: bool = True):
        self.impersonate = impersonate
        self.enable_http2 = enable_http2
        self.profile = next(
            (p for p in CHROME_PROFILES if p["impersonate"] == impersonate),
            CHROME_PROFILES[0],
        )
        # 会话级固定：浏览器/OS/语言/屏幕，构造一份稳定的 client-hints 池
        self.platform = random.choice(["Windows", "Win32"])
        self.locale = "zh-CN"

    def headers(self, referer: str | None = None, accept_html: bool = True) -> dict:
        h = browser_headers(referer, accept_html=accept_html)
        h["User-Agent"] = self.profile["ua"]
        h["sec-ch-ua"] = self.profile["sec_ch_ua"]
        return dict(h)

    def client_kwargs(self) -> dict:
        """传给 curl_cffi.requests.Session 的参数。"""
        return {
            "impersonate": self.impersonate,
            "timeout": 25,
            "allow_redirects": True,
            "verify": True,
            # HTTP/2 与真实浏览器一致；某些老站不支持 h2，client 会回落
            "http_version": "v2" if self.enable_http2 else "v1.1",
        }

    def navigate_sequence(self, base_url: str) -> list[str]:
        """构造「像人一样的访问路径」：首页 → 栏目页 → 详情页。

        直接打详情页（无 Referer、无前序请求）是 WAF 最常见的单点识别特征，
        所以抓详情前先访问一次列表页把 Cookie 拿到手。
        """
        b = base_url.rstrip("/")
        return [b + "/", b + "/index.html"]
