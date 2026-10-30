"""配置加载：YAML → 强类型 dataclass，缺失项给安全默认值。

为什么单独一层：爬虫所有可调参数（节流、代理、指纹、解析器）都从这里出，
换站点/换策略只改 YAML，不动代码。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent  # 01_crawler/（config.py 在 crawler/ 下）
DEFAULT_CONFIG = ROOT / "config" / "settings.yaml"


def _deep_get(d: dict, path: str, default=None):
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass
class ThrottleCfg:
    per_host_concurrency: int = 1
    min_interval_sec: float = 2.0
    max_interval_sec: float = 4.5
    burst_limit: int = 3
    burst_sleep_sec: float = 15.0
    max_retries: int = 4
    backoff_base: float = 1.8
    timeout_sec: float = 25.0
    # 单次运行的请求预算（列表页也算）。之前只有「最大页数」一个口径，
    # 结果列表翻页把预算吃完、一篇详情都没抓 —— 拆成两个数才可控。
    max_requests_per_run: int = 900
    detail_limit: int = 150


@dataclass
class ProxyCfg:
    enabled: bool = False
    providers: list[str] = field(default_factory=list)
    health_check_url: str = "http://httpbin.org/ip"
    health_check_interval_sec: int = 120
    max_fail_count: int = 3
    rotate: str = "per_request"


@dataclass
class FingerprintCfg:
    impersonate: str = "chrome124"
    header_order_profile: str = "chrome"
    enable_http2: bool = True


@dataclass
class CaptchaCfg:
    enabled: bool = True
    engine: str = "ddddocr"
    remote_api: str = ""
    max_attempts: int = 5


@dataclass
class ChunkingCfg:
    chunk_size: int = 480
    overlap: int = 80
    min_chunk_chars: int = 60


@dataclass
class ProjectCfg:
    name: str = "fujian-tourism-crawler"
    data_dir: str = "data"
    db_path: str = "data/tourism.db"
    raw_dir: str = "data/raw"
    corpus_dir: str = "data/corpus"
    report_dir: str = "data/reports"
    log_dir: str = "data/logs"
    user_agent_rotate: bool = True
    respect_robots: bool = True
    bypass_mode: bool = False


@dataclass
class Settings:
    project: ProjectCfg
    throttle: ThrottleCfg
    proxy: ProxyCfg
    fingerprint: FingerprintCfg
    captcha: CaptchaCfg
    chunking: ChunkingCfg
    sites: list[dict]
    scenic_fields: dict
    parsers: dict
    raw: dict

    # ---- 路径便捷方法（全部解析成绝对路径）----
    def p(self, key: str) -> Path:
        rel = getattr(self.project, key)
        path = Path(rel)
        return path if path.is_absolute() else (ROOT / path)

    def ensure_dirs(self) -> None:
        for key in ("data_dir", "raw_dir", "corpus_dir", "report_dir", "log_dir"):
            self.p(key).mkdir(parents=True, exist_ok=True)
        self.p("db_path").parent.mkdir(parents=True, exist_ok=True)

    def site(self, key: str) -> dict:
        for s in self.sites:
            if s.get("key") == key:
                return s
        raise KeyError(f"未配置站点: {key}")

    def apply_env_overrides(self) -> None:
        """环境变量优先级最高，便于 CI 与临时提权调试。"""
        if os.getenv("CRAWL_BYPASS", "").lower() in ("1", "true", "yes"):
            self.project.bypass_mode = True
        if os.getenv("CRAWL_PROXY"):
            self.proxy.enabled = True
            self.proxy.providers = [u.strip() for u in os.environ["CRAWL_PROXY"].split(",") if u.strip()]
        if os.getenv("CRAWL_MAX_REQUESTS"):
            self.throttle.max_requests_per_run = int(os.environ["CRAWL_MAX_REQUESTS"])
        if os.getenv("CRAWL_DETAIL_LIMIT"):
            self.throttle.detail_limit = int(os.environ["CRAWL_DETAIL_LIMIT"])
        if os.getenv("CRAWL_IMPERSONATE"):
            self.fingerprint.impersonate = os.environ["CRAWL_IMPERSONATE"]


def load_settings(path: str | Path | None = None) -> Settings:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    s = Settings(
        project=ProjectCfg(**(raw.get("project") or {})),
        throttle=ThrottleCfg(**(raw.get("throttle") or {})),
        proxy=ProxyCfg(**(raw.get("proxy") or {})),
        fingerprint=FingerprintCfg(**(raw.get("fingerprint") or {})),
        captcha=CaptchaCfg(**(raw.get("captcha") or {})),
        chunking=ChunkingCfg(**(raw.get("chunking") or {})),
        sites=raw.get("sites") or [],
        scenic_fields=raw.get("scenic_fields") or {},
        parsers=raw.get("parsers") or {"default": "gov_notice"},
        raw=raw,
    )
    s.apply_env_overrides()
    s.ensure_dirs()
    return s
