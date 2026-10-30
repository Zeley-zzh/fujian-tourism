"""解析器基类与注册表。

解析器只关心「一页 HTML → 结构化 Document」这件事，不负责发请求、不入库。
这样换站点 = 新写一个 Parser 子类 + 在 registry 注册，抓取/入库/清洗全都不用改。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DocRecord:
    """解析器输出的最小单元，字段与 documents 表对齐。"""
    url: str
    title: str
    content_text: str = ""
    content_html: str = ""
    doc_type: str = "notice"
    issuer: str | None = None
    doc_no: str | None = None
    publish_date: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    attachments: list[str] = field(default_factory=list)
    raw_path: str | None = None
    extra: dict = field(default_factory=dict)

    def is_useful(self) -> bool:
        return bool(self.title.strip()) and len(self.content_text.strip()) >= 40


class BaseParser(ABC):
    """站点解析器接口。"""

    name = "base"

    def __init__(self, site_cfg: dict):
        self.cfg = site_cfg
        self.sel = site_cfg.get("detail_selector") or {}

    @abstractmethod
    def parse_list(self, html: str, page_url: str) -> list[dict]:
        """列表页 → [{url, title, date}]"""

    @abstractmethod
    def parse_detail(self, html: str, url: str) -> DocRecord:
        """详情页 → DocRecord"""

    # ---- 公共工具：所有站点都会用到 ---- #
    @staticmethod
    def text_of(node) -> str:
        if node is None:
            return ""
        return re.sub(r"[ \t\u3000]+", " ", node.get_text("\n", strip=True)).strip()

    @staticmethod
    def first_match(patterns: list[str], text: str) -> str | None:
        for p in patterns:
            m = re.search(p, text or "")
            if m:
                return m.group(1) if m.groups() else m.group(0)
        return None

    def _abs(self, href: str, page_url: str) -> str:
        from urllib.parse import urljoin
        return urljoin(page_url, (href or "").strip())
