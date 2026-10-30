"""robots.txt 与站点地图解析。

默认尊重 robots（respect_robots=true）。bypass_mode 打开时不读 robots，
但仍然保留「是否有 sitemap」的探测能力——sitemap 是成本最低的入口，
有它就优先走它，没必要一页页翻目录。
"""
from __future__ import annotations

import re
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

UA = "Mozilla/5.0 (compatible; FujianTourismResearchBot/1.0)"


@dataclass
class RobotsRules:
    allow_all: bool = True
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float = 0.0
    sitemaps: list[str] = field(default_factory=list)

    def allowed(self, path: str) -> bool:
        if self.allow_all:
            return True
        best_len = -1
        verdict = True
        for allow, pat in self.rules:
            rx = re.escape(pat).replace(r"\*", ".*")
            if re.match(rx, path):
                if len(pat) > best_len:
                    best_len = len(pat)
                    verdict = allow
        return verdict


class RobotsGate:
    def __init__(self, *, respect: bool = True, timeout: float = 10.0):
        self.respect = respect
        self.timeout = timeout
        self._cache: dict[str, RobotsRules] = {}

    def rules_for(self, url: str) -> RobotsRules:
        host = urlparse(url).netloc
        if host in self._cache:
            return self._cache[host]
        if not self.respect:
            r = RobotsRules(allow_all=True)
            self._cache[host] = r
            return r
        r = self._fetch(url)
        self._cache[host] = r
        return r

    def can_fetch(self, url: str) -> bool:
        if not self.respect:
            return True
        rules = self.rules_for(url)
        return rules.allowed(urlparse(url).path or "/")

    def delay(self, url: str) -> float:
        if not self.respect:
            return 0.0
        return self.rules_for(url).crawl_delay

    def _fetch(self, url: str) -> RobotsRules:
        p = urlparse(url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        try:
            r = httpx.get(robots_url, timeout=self.timeout,
                          headers={"User-Agent": UA}, follow_redirects=True)
            if r.status_code != 200:
                return RobotsRules(allow_all=True)
            return self.parse(r.text)
        except Exception:  # noqa: BLE001
            # 取不到 robots 时按「全允许」处理，但要记日志（上层负责）
            return RobotsRules(allow_all=True)

    @staticmethod
    def parse(text: str) -> RobotsRules:
        rs = RobotsRules(allow_all=False)
        active = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                active = v == "*"
            elif k == "sitemap":
                if v:
                    rs.sitemaps.append(v)
            elif active and k == "disallow" and v:
                rs.rules.append((False, v))
            elif active and k == "allow" and v:
                rs.rules.append((True, v))
            elif active and k == "crawl-delay":
                try:
                    rs.crawl_delay = float(v)
                except ValueError:
                    pass
        return rs

    def sitemap_entries(self, site_cfg: dict, max_urls: int = 5000) -> list[str]:
        """优先读配置的 sitemap，其次 robots 里声明的，最后 <base>/sitemap.xml。"""
        candidates: list[str] = []
        if site_cfg.get("sitemap"):
            candidates.append(site_cfg["sitemap"])
        base = site_cfg.get("base_url", "")
        if base:
            candidates.extend(self.rules_for(base).sitemaps)
            candidates.append(base.rstrip("/") + "/sitemap.xml")

        urls: list[str] = []
        seen: set[str] = set()
        for sm in dict.fromkeys(candidates):
            if not sm or sm in seen:
                continue
            seen.add(sm)
            urls.extend(self._read_sitemap(sm, depth=0))
            if len(urls) >= max_urls:
                break
        return urls[:max_urls]

    def _read_sitemap(self, url: str, depth: int = 0,
                      _seen: set[str] | None = None) -> list[str]:
        if depth > 2:
            return []
        _seen = _seen if _seen is not None else set()
        if url in _seen:
            return []
        _seen.add(url)
        try:
            r = httpx.get(url, timeout=self.timeout, headers={"User-Agent": UA},
                          follow_redirects=True)
            if r.status_code != 200:
                return []
            body = r.text
        except Exception:  # noqa: BLE001
            return []

        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, flags=re.I)
        if re.search(r"<sitemapindex", body, flags=re.I):
            out: list[str] = []
            for loc in locs:
                out.extend(self._read_sitemap(loc, depth + 1, _seen))
            return out
        return locs

    @staticmethod
    def legacy_robotparser_ok(url: str, robots_url: str) -> bool:
        """备用：标准库实现（当站点 robots 语法不规范时用它交叉判一次）。"""
        rp = robotparser.RobotFileParser()
        try:
            rp.set_url(robots_url)
            rp.read()
            return rp.can_fetch(UA, url)
        except Exception:  # noqa: BLE001
            return True
