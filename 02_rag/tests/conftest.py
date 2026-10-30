"""02_rag 测试夹具。

跑法（在 02_rag 目录下）：
    python -m pytest -q

两个隔离：
1. **库隔离** —— 每个用例一个临时 SQLite，沿用 01_crawler 的 schema.sql，
   保证 DDL 与生产完全一致，且不碰真实 data/tourism.db。
2. **配置隔离** —— autouse 夹具把 RAG_CONFIG_PATH 指到用例自己的临时目录，
   并清掉 LLM_* / OPENAI_API_KEY / EMBEDDING_* 环境变量。
   没有这层隔离，本机若真配过 Key，测试就会去打真实模型。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent          # 02_rag
PROJECT = ROOT.parent                                  # fujian-tourism
CRAWLER = PROJECT / "01_crawler"

for p in (str(ROOT), str(CRAWLER)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """把所有配置读写关进临时目录，测试不会碰到真实凭据。"""
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("RAG_CONFIG_PATH", str(cfg))
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
                 "EMBEDDING_BACKEND", "EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)
    from app import settings_store
    settings_store.save({"embedding_backend": "hash", "embedding_model": ""})
    yield cfg


@pytest.fixture(scope="session")
def schema_path() -> Path:
    p = CRAWLER / "crawler" / "db" / "schema.sql"
    assert p.exists(), f"缺少 schema: {p}"
    return p


@pytest.fixture()
def db(tmp_path, schema_path):
    """空库 + 建表；用 01_crawler 的 Database，保证 DDL 与生产完全一致。"""
    from crawler.db import Database
    d = Database(tmp_path / "t.db")
    d.init_schema()
    yield d
    d.close()


def seed(db, *, doc_id="d0001", url="https://example.gov.cn/a.html",
         title="甲峰景区门票与开放时间公告",
         text="甲峰景区开放时间 08:00—17:30。门票价格 65 元。",
         publish_date="2026-03-20"):
    """插一篇文档 + 一条切片，返回 (doc_id, chunk_id)。"""
    from crawler.parsers.base import DocRecord
    rec = DocRecord(url=url, title=title, content_text=text,
                    content_html=f"<p>{text}</p>", doc_type="notice",
                    publish_date=publish_date)
    got, _ = db.upsert_document(rec, raw_path=None)
    chunk_id = f"chunk_{got}_0000"
    db.replace_chunks(got, [{
        "chunk_id": chunk_id, "chunk_index": 0, "text": text,
        "heading_path": "一、门票", "page_no": None, "token_estimate": len(text) / 1.4,
    }])
    return got, chunk_id
