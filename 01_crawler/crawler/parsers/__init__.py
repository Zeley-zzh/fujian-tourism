"""解析器注册表：parser 名 → Parser 类。

新增一个站点只要两步：在 settings.yaml 的 sites 里加一段配置，把 parser 指向某个名字；
结构差异大的站点再写一个 BaseParser 子类，在这里加一行映射。
抓取、入库、清洗、切片都不动。
"""
from __future__ import annotations

from .base import BaseParser, DocRecord
from .gov_notice import GovNoticeParser
from .scenic_list import ScenicListParser
from .wlt_gov import WltGovParser

PARSERS: dict[str, type[BaseParser]] = {
    "wlt_gov": WltGovParser,
    "gov_notice": GovNoticeParser,
    "scenic_list": ScenicListParser,
}

DEFAULT_PARSER = "gov_notice"


def get_parser(name: str, site_cfg: dict) -> BaseParser:
    cls = PARSERS.get(name) or PARSERS[DEFAULT_PARSER]
    return cls(site_cfg)


def register(name: str, cls: type[BaseParser]) -> None:
    PARSERS[name] = cls


__all__ = ["PARSERS", "DEFAULT_PARSER", "get_parser", "register", "BaseParser", "DocRecord"]
