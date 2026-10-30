"""通用工具：URL 规范化、hash、时间、文本清洗、日志。

刻意保持无第三方依赖（除 dateutil），RAG 侧也会 import 这里的规范化逻辑。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
import unicodedata
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from dateutil import parser as dtparser

# ---------------- 时间 ----------------
CN_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    """带时区的 ISO 时间，全库统一用它，避免 naive/aware 混用。"""
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def parse_date(text: str | None) -> str | None:
    """把 2026年3月12日 / 2026-03-12 / 2026/03/12 统一成 YYYY-MM-DD。

    解析不出就返回 None —— 绝不用抓取时间冒充发布日期。
    """
    if not text:
        return None
    s = unicodedata.normalize("NFKC", str(text)).strip()
    s = re.sub(r"[年月]", "-", s).replace("日", "").strip()
    s = re.sub(r"\s+", "", s)
    m = re.search(r"(\d{4})-?(\d{1,2})-?(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    try:
        return dtparser.parse(s, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


def in_effective_range(eff_from: str | None, eff_to: str | None, at: str | None = None) -> bool:
    """当前日期是否落在有效期内。任一端为空按「无界」处理。"""
    at = at or today()
    if eff_from and at < eff_from:
        return False
    if eff_to and at > eff_to:
        return False
    return True


# ---------------- URL ----------------
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "share_token", "timestamp", "_t", "t",
}


def normalize_url(url: str, base: str | None = None) -> str:
    """规范化 URL 作为去重键：补全相对路径、小写 host、去跟踪参数、去 fragment。"""
    if base:
        url = urljoin(base, url.strip())
    p = urlparse(url.strip())
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower()
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
          if k.lower() not in TRACKING_PARAMS]
    qs.sort()
    return urlunparse((scheme, host, path, "", urlencode(qs), ""))


def url_hash(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def same_host(a: str, b: str) -> bool:
    return urlparse(a).hostname == urlparse(b).hostname


def guess_ext(url: str, content_type: str = "") -> str:
    path = urlparse(url).path.lower()
    for ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".html", ".htm"):
        if path.endswith(ext):
            return ext
    ct = (content_type or "").lower()
    mapping = {"pdf": ".pdf", "wordprocessingml": ".docx", "spreadsheetml": ".xlsx", "excel": ".xls"}
    for k, v in mapping.items():
        if k in ct:
            return v
    return ".html"


# ---------------- 文本 ----------------
WS_RE = re.compile(r"[ \t\u3000\xa0]+")
MULTI_NL = re.compile(r"\n{3,}")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def clean_text(text: str) -> str:
    """清洗 HTML 抽出的正文：去控制字符、压空白、保留段落换行。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = CTRL_RE.sub("", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = WS_RE.sub(" ", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    t = MULTI_NL.sub("\n\n", t)
    return t.strip()


def collapse_for_hash(text: str) -> str:
    """算 hash 前把无意义空白抹平，避免排版改动被误判为内容变更。"""
    return re.sub(r"\s+", "", text or "")


def content_hash(text: str) -> str:
    return hashlib.sha256(collapse_for_hash(text).encode("utf-8")).hexdigest()


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(CJK_RE.findall(text)) / max(len(text), 1)


def truncate(text: str, limit: int = 200) -> str:
    """原文摘录裁剪：不截断在半个括号/引号里，尽量落在标点。"""
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for punct in ("。", "；", "！", "？", "\n", "，"):
        idx = cut.rfind(punct)
        if idx >= limit * 0.6:
            return cut[: idx + 1]
    return cut + "…"


# ---------------- 日志 ----------------
def get_logger(name: str, log_dir: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    logger.propagate = False
    return logger
