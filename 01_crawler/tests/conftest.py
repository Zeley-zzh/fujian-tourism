"""pytest 公共夹具。

原则：测试不联网、不依赖外网站点，全部用固定的 HTML 片段与纯函数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler.config import load_settings  # noqa: E402
from crawler.db import Database  # noqa: E402


@pytest.fixture()
def settings():
    s = load_settings()
    return s


@pytest.fixture()
def db(tmp_path):
    """临时库：每个测试一个独立 SQLite 文件，避免互相污染。"""
    d = Database(tmp_path / "t.db")
    d.init_schema()
    yield d
    d.close()
