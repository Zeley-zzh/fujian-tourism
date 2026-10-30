"""AntiCrawlClient：把指纹 / 代理 / 节流 / 验证码 / 重试组装成一个 fetch 入口。

对外只有一个方法 `fetch(url, referer=...) -> FetchResult`，上层抓取器不关心
底下用了什么手段。四个关键行为：

1. 会话连续性：同一站点复用 session（Cookie 池 + 固定指纹 + sticky 代理），
   不然后台一点 JS 挑战就永远过不去。
2. 拦截识别：状态码（403/429/503）+ 正文特征（验证码/滑块/雷池/安全狗/拦截页）
   双路判定，识别到就走 solve_* 流程。
3. 分级退避：429 走退避、403 先换代理再退避、连续封禁则整站熔断。
4. 额外请求头：政务站的检索接口要求 `X-Requested-With: XMLHttpRequest` 才返回 JSON，
   由调用方通过 `extra_headers` 传入，客户端不硬编码任何站点特征。

curl_cffi 存在时用它（TLS 指纹伪装）；否则降级 httpx，功能不减、伪装变弱。
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ..utils import get_logger, normalize_url
from .captcha import CaptchaSolver
from .fingerprint import FingerprintSpoofer
from .proxy_pool import ProxyNode, ProxyPool
from .robots import RobotsGate
from .throttle import HostThrottle

try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    HAS_CURL_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CURL_CFFI = False

import httpx


# 常见拦截页 / 验证码页的正文特征（政府采购站与政务云 WAF 常用这几家）
CAPTCHA_MARKERS = (
    "验证码", "请输入验证码", "captcha", "verify you are human", "滑动验证",
    "拖动滑块", "人机验证", "安全验证", "点击完成验证",
)
BLOCK_MARKERS = (
    "访问被拦截", "访问受限", "您的访问过于频繁", "请求过于频繁", "拒绝访问",
    "access denied", "forbidden", "已被网站管理员禁止", "安全狗", "safedog",
    "waf", "云锁", "长亭雷池", "tengine", "您的IP已被封禁",
)
RATE_LIMIT_MARKERS = ("429", "too many requests", "访问频率", "超出限制",
                      "过于频繁", "请求频繁", "稍后再试", "请稍后", "rate limit")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int = 0
    content: bytes = b""
    text: str = ""
    content_type: str = ""
    headers: dict = field(default_factory=dict)
    ok: bool = False
    error: str = ""
    attempts: int = 0
    elapsed_ms: int = 0
    blocked: bool = False
    captcha_solved: bool = False
    proxy_used: str | None = None

    @property
    def is_pdf(self) -> bool:
        return "pdf" in self.content_type.lower() or self.content[:4] == b"%PDF"


class AntiCrawlClient:
    def __init__(self, settings, *, log_dir: Path | None = None):
        self.s = settings
        self.log = get_logger("anticrawl", log_dir or settings.p("log_dir"))
        self.throttle = HostThrottle(
            min_interval=settings.throttle.min_interval_sec,
            max_interval=settings.throttle.max_interval_sec,
            burst_limit=settings.throttle.burst_limit,
            burst_sleep=settings.throttle.burst_sleep_sec,
            per_host_concurrency=settings.throttle.per_host_concurrency,
        )
        self.proxy = ProxyPool.from_settings(settings.proxy)
        self.spoofer = FingerprintSpoofer(
            impersonate=settings.fingerprint.impersonate,
            enable_http2=settings.fingerprint.enable_http2,
        )
        self.captcha = CaptchaSolver(
            engine=settings.captcha.engine,
            manual_dir=settings.p("data_dir") / "manual_captcha",
            remote_api=settings.captcha.remote_api,
        )
        self.robots = RobotsGate(respect=settings.project.respect_robots)

        self._sessions: dict[str, object] = {}
        self.cookies: dict[str, dict] = {}
        self.blocked_hosts: dict[str, float] = {}
        self.stats = {"ok": 0, "failed": 0, "blocked": 0, "captcha": 0, "retried": 0}
        if not HAS_CURL_CFFI:
            self.log.warning("未安装 curl_cffi，TLS 指纹伪装降级为 httpx（建议 pip install curl_cffi）")

    # ------------------------------------------------------------------ 会话
    def _session(self, host: str):
        if host in self._sessions:
            return self._sessions[host]
        if HAS_CURL_CFFI:
            sess = cffi_requests.Session()
            try:
                sess.headers.update(self.spoofer.headers())
            except Exception:  # noqa: BLE001
                pass
        else:
            sess = httpx.Client(
                timeout=self.s.throttle.timeout_sec,
                headers=self.spoofer.headers(),
                follow_redirects=True,
                http2=self.s.fingerprint.enable_http2,
            )
        self._sessions[host] = sess
        return sess

    def _apply_proxy(self, host: str, node: ProxyNode | None) -> None:
        sess = self._session(host)
        proxies = self.proxy.requests_proxies(node)
        try:
            if HAS_CURL_CFFI:
                sess.proxies = proxies or {}
            else:
                # httpx 不支持实例级热切换，用 transport 重建
                if getattr(self, "_httpx_proxy", None) != (proxies or None):
                    self._httpx_proxy = proxies or None
                    self._sessions.pop(host, None)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 主入口
    def fetch(self, url: str, *, referer: str | None = None,
              method: str = "GET", data: dict | None = None,
              is_asset: bool = False, allow_retry: bool = True,
              extra_headers: dict | None = None) -> FetchResult:
        t0 = time.time()
        url = normalize_url(url)
        host = urlparse(url).netloc

        # 熔断检查
        until = self.blocked_hosts.get(host, 0)
        if until > time.time():
            return FetchResult(url=url, final_url=url, ok=False,
                               error=f"站点熔断中，剩余 {int(until - time.time())}s",
                               elapsed_ms=int((time.time() - t0) * 1000))

        # robots
        if not self.robots.can_fetch(url):
            self.log.warning("robots 禁止抓取: %s", url)
            return FetchResult(url=url, final_url=url, ok=False, error="robots_disallow",
                               elapsed_ms=int((time.time() - t0) * 1000))

        last_err = ""
        total_attempts = self.s.throttle.max_retries if allow_retry else 1

        for attempt in range(total_attempts):
            self.throttle.acquire(host)
            node = self.proxy.acquire(host) if self.proxy else None
            self._apply_proxy(host, node)
            sess = self._session(host)

            try:
                headers = self.spoofer.headers(referer=referer,
                                               accept_html=not is_asset)
                if is_asset:
                    headers["Accept"] = "*/*"
                    headers["Sec-Fetch-Dest"] = "document"
                    headers["Sec-Fetch-Mode"] = "navigate"
                if extra_headers:
                    headers.update(extra_headers)

                if HAS_CURL_CFFI:
                    resp = sess.request(
                        method, url, headers=headers, data=data,
                        timeout=self.s.throttle.timeout_sec,
                        impersonate=self.spoofer.impersonate,
                        allow_redirects=True,
                    )
                    status = resp.status_code
                    content = resp.content
                    ctype = resp.headers.get("content-type", "")
                    final = str(resp.url)
                    rheaders = dict(resp.headers)
                else:
                    resp = sess.request(method, url, headers=headers, data=data)
                    status = resp.status_code
                    content = resp.content
                    ctype = resp.headers.get("content-type", "")
                    final = str(resp.url)
                    rheaders = dict(resp.headers)

                text = self._decode(content, ctype)
                blocked, kind = self._classify(status, text, content)

                # 成功
                if 200 <= status < 300 and not blocked:
                    self.proxy.report_ok(node)
                    self.stats["ok"] += 1
                    return FetchResult(
                        url=url, final_url=final, status=status, content=content,
                        text=text, content_type=ctype, headers=rheaders, ok=True,
                        attempts=attempt + 1, elapsed_ms=int((time.time() - t0) * 1000),
                        proxy_used=str(node) if node else None,
                    )

                # 验证码：尝试解一次
                if kind == "captcha":
                    solved = self._handle_captcha(url, text, final, host, sess, referer)
                    if solved:
                        self.stats["captcha"] += 1
                        continue
                    last_err = "captcha_unsolved"

                # 风控/限流
                if kind in ("rate_limit", "block") or status in (403, 429, 503):
                    self.stats["blocked"] += 1
                    self.proxy.report_fail(node, hard=(status in (403, 407)))
                    if status == 429 or kind == "rate_limit":
                        self.throttle.cool_down_for_host(host, 60)
                        self.throttle.backoff(host, attempt, self.s.throttle.backoff_base)
                    else:
                        # 403 先换出口再试，多次仍 403 就熔断该站
                        self.throttle.backoff(host, attempt, self.s.throttle.backoff_base)
                        if attempt >= 2 and len(self.proxy.alive_nodes()) == 0:
                            self.blocked_hosts[host] = time.time() + 600
                            last_err = f"http_{status}_blocked"
                            break
                    last_err = f"http_{status}_{kind}"
                    self.stats["retried"] += 1
                    continue

                # 5xx / 4xx 普通错误
                if status >= 500 or status == 408:
                    last_err = f"http_{status}"
                    self.throttle.backoff(host, attempt, self.s.throttle.backoff_base)
                    self.stats["retried"] += 1
                    continue

                # 404 等不重试
                last_err = f"http_{status}"
                break

            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                self.proxy.report_fail(node, hard=True)
                self.log.debug("请求异常 %s attempt=%d: %s", url, attempt, e)
                if attempt + 1 < total_attempts:
                    self.throttle.backoff(host, attempt, self.s.throttle.backoff_base)
                    self.stats["retried"] += 1
                continue

        self.stats["failed"] += 1
        return FetchResult(url=url, final_url=url, ok=False, error=last_err,
                           elapsed_ms=int((time.time() - t0) * 1000),
                           attempts=total_attempts)

    # ------------------------------------------------------------------ 判定
    @staticmethod
    def _classify(status: int, text: str, content: bytes) -> tuple[bool, str]:
        if status in (403, 401):
            return True, "block"
        if status == 429:
            return True, "rate_limit"
        if status == 503:
            low = text.lower()
            if any(m in low for m in RATE_LIMIT_MARKERS):
                return True, "rate_limit"
            return True, "block"

        # 200 但正文是拦截/验证页（最常见，必须查正文）
        if len(content) < 4096:
            low = (text or "")[:2000].lower()
            if any(m.lower() in low for m in CAPTCHA_MARKERS):
                return True, "captcha"
            if any(m.lower() in low for m in BLOCK_MARKERS):
                return True, "block"
        return False, "ok"

    def _handle_captcha(self, url: str, text: str, final: str, host: str,
                        sess, referer: str | None) -> bool:
        """在拦截页里找图片验证码 → 识别 → 带答案重发。

        这里只能处理「图片码 + 表单一并返回」的无状态验证码；
        需要 JS 渲染或滑块交互的，交给上层用 Playwright 兜底（见 browser_fallback）。
        """
        if not self.s.captcha.enabled:
            return False
        import re
        m = re.search(r'<img[^>]+src=["\']([^"\']*(?:captcha|verify|code|yzm)[^"\']*)["\']',
                      text, flags=re.I)
        if not m:
            self.log.info("拦截页未找到可直接识别的验证码图片: %s", url)
            return False
        img_url = m.group(1)
        if img_url.startswith("/") or not img_url.startswith("http"):
            p = urlparse(final)
            img_url = f"{p.scheme}://{p.netloc}{img_url if img_url.startswith('/') else '/' + img_url}"

        r = self.fetch(img_url, referer=final, is_asset=True, allow_retry=False)
        if not r.ok or not r.content:
            return False
        res = self.captcha.solve_image(r.content)
        if not res.ok:
            self.log.info("验证码识别失败: %s", res.error)
            return False

        # 找到表单提交地址与字段名，带答案重放
        fm = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', text, flags=re.I)
        nm = re.search(r'<input[^>]+name=["\']([^"\']+?)["\'][^>]*(?:captcha|verify|code|yzm)',
                       text, flags=re.I) or re.search(
            r'<input[^>]+(?:captcha|verify|code|yzm)[^>]+name=["\']([^"\']+)["\']',
            text, flags=re.I)
        if not fm or not nm:
            self.log.info("拦截页表单结构无法自动解析，转人工")
            return False

        action = fm.group(1)
        if not action.startswith("http"):
            p = urlparse(final)
            action = f"{p.scheme}://{p.netloc}{action if action.startswith('/') else '/' + action}"
        field = nm.group(1)
        rr = self.fetch(action, referer=final, method="POST",
                        data={field: res.text}, allow_retry=False)
        ok = rr.ok and not self._classify(rr.status, rr.text, rr.content)[0]
        self.log.info("验证码提交 %s → %s", res.text, "通过" if ok else "未通过")
        return ok

    # ------------------------------------------------------------------ 浏览器兜底
    def browser_fallback(self, url: str, *, wait_ms: int = 3500) -> FetchResult:
        """遇到 JS 渲染 / 滑块 / 强指纹站点时用真浏览器拿页面。

        只在 fetch 连续失败后调用：Playwright 启动成本约 1-2 秒，
        不能当主路径用。装了 playwright 才有，否则返回失败。
        """
        t0 = time.time()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(url=url, final_url=url, ok=False,
                               error="playwright 未安装，无法浏览器兜底",
                               elapsed_ms=int((time.time() - t0) * 1000))
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ])
                ctx = browser.new_context(
                    locale="zh-CN",
                    user_agent=self.spoofer.profile["ua"],
                    viewport={"width": 1440, "height": 900},
                    timezone_id="Asia/Shanghai",
                )
                # 抹掉 navigator.webdriver，这是最常被检测的一项
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "window.chrome={runtime:{}};"
                    "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(wait_ms / 1000)
                html = page.content()
                browser.close()
                return FetchResult(url=url, final_url=url, status=200, text=html,
                                   content=html.encode("utf-8"), content_type="text/html",
                                   ok=True, elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            return FetchResult(url=url, final_url=url, ok=False, error=f"浏览器兜底失败: {e}",
                               elapsed_ms=int((time.time() - t0) * 1000))

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _decode(content: bytes, ctype: str) -> str:
        if not content:
            return ""
        if "pdf" in ctype.lower() or content[:4] == b"%PDF":
            return ""  # PDF 由 parser 处理
        for enc in ("utf-8", "gb18030", "gbk", "big5"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")

    def close(self) -> None:
        for sess in self._sessions.values():
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()

    def report(self) -> dict:
        return {
            "fetch_stats": dict(self.stats),
            "throttle": self.throttle.stats.__dict__,
            "proxy": self.proxy.stats() if self.proxy else {"total": 0},
            "captcha": dict(self.captcha.stats),
            "curl_cffi": HAS_CURL_CFFI,
        }
