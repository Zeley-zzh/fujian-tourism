"""HTTP 层：路由全部有真实数据、三态渲染、无空页。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.embeddings import Embedder
from app.ingest import build_index
from app.store import TourismDB

from conftest import seed


@pytest.fixture()
def client(tmp_path, schema_path, monkeypatch):
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "faiss"))

    from crawler.db import Database
    d = Database(tmp_path / "web.db")
    d.init_schema()
    d1, _ = seed(d, doc_id="d0001", url="https://example.gov.cn/1.html",
                 title="甲峰景区门票公告",
                 text="甲峰景区开放时间：08:00—17:30。门票价格：65 元。", publish_date="2026-03-20")
    d2, _ = seed(d, doc_id="d0002", url="https://example.gov.cn/2.html",
                 title="甲峰景区调价通知",
                 text="甲峰景区开放时间：08:30—17:00。门票价格：80 元。", publish_date="2026-05-25")
    d.conn.commit()
    sid = d.upsert_spot(name="甲峰景区", city="福州市", level="5A")
    for val, doc, title, date, st in [
        ("65 元", d1, "甲峰景区门票公告", "2026-03-20", "已核验"),
        ("80 元", d2, "甲峰景区调价通知", "2026-05-25", "待核验"),
    ]:
        d.add_provenance(spot_id=sid, field_name="ticket_price", field_value=val,
                         source_doc_id=doc, source_url=f"https://example.gov.cn/{doc[-1]}.html",
                         doc_title=title, issuer="省文化和旅游厅", publish_date=date,
                         effective_from=None, effective_to=None,
                         quote=f"门票价格：{val}。", status=st)
    d.add_provenance(spot_id=sid, field_name="phone", field_value="0591-00000000",
                     source_doc_id=d1, source_url="https://example.gov.cn/1.html",
                     doc_title="甲峰景区门票公告", issuer="省文化和旅游厅",
                     publish_date="2026-03-20", effective_from=None, effective_to=None,
                     quote="咨询电话：0591-00000000。", status="已核验")
    # 复核队列：门票价格两个取值互相矛盾，等人工裁决
    prov_id = d.conn.execute(
        "SELECT prov_id FROM field_provenance WHERE spot_id=? AND field_value='80 元'",
        (sid,)).fetchone()[0]
    d.conn.execute(
        "INSERT INTO review_queue(prov_id, doc_id, reason, severity, created_at) "
        "VALUES (?,?,?,?,datetime('now','localtime'))",
        (prov_id, d2, "conflict", "high"))
    d.conn.commit()
    d.close()

    app_main._db = TourismDB(tmp_path / "web.db")
    build_index(app_main._db, Embedder(backend="hash"), force=True)
    from app.llm import LLMClient
    from app.qa import QAService
    app_main._qa = QAService(app_main._db, llm=LLMClient())
    app_main._index_note = "built: test"

    with TestClient(app_main.app) as c:
        yield c, sid, d1, d2

    app_main._db.close()
    app_main._db = None
    app_main._qa = None


def test_home_renders_stats_and_spot(client):
    c, sid, *_ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "甲峰景区" in r.text
    assert "数据来源：福建省文化和旅游厅公开文件" in r.text
    assert "未接入模型" in r.text              # 模型未接入必须显式标注
    assert f"/spot/{sid}" in r.text


def test_spots_list_and_filter(client):
    c, sid, *_ = client
    r = c.get("/spots")
    assert r.status_code == 200 and "甲峰景区" in r.text
    r2 = c.get("/spots", params={"city": "不存在的市"})
    assert "没有匹配的景区" in r2.text
    r3 = c.get("/spots", params={"status": "冲突"})
    assert "甲峰景区" in r3.text


def test_spot_detail_shows_three_states_and_conflict(client):
    c, sid, *_ = client
    r = c.get(f"/spot/{sid}")
    assert r.status_code == 200
    txt = r.text
    assert "门票价格" in txt and "咨询电话" in txt
    assert "65 元" in txt and "80 元" in txt      # 冲突双值并列，不合并
    assert "冲突" in txt
    assert "原文摘录" in txt                       # 溯源弹层字段齐全
    assert "发文机关" in txt and "发布日期" in txt and "抓取时间" in txt
    assert "冲突与变更对比" in txt


def test_spot_detail_404(client):
    c, *_ = client
    assert c.get("/spot/s9999").status_code == 404


def test_doc_detail_shows_versions_and_chunks(client):
    c, sid, d1, _ = client
    r = c.get(f"/doc/{d1}")
    assert r.status_code == 200
    assert "版本历史" in r.text
    assert f"chunk_{d1}_0000" in r.text
    assert "该文档提供的字段证据" in r.text
    assert "65 元" in r.text


def test_doc_detail_404(client):
    c, *_ = client
    assert c.get("/doc/d9999").status_code == 404


def test_ask_get_form(client):
    c, *_ = client
    r = c.get("/ask")
    assert r.status_code == 200
    assert 'name="question"' in r.text
    assert "未接入模型" in r.text


def test_ask_post_produces_answer_with_citations(client):
    c, *_ = client
    r = c.post("/ask", data={"question": "甲峰景区门票多少钱？", "top_k": "3"})
    assert r.status_code == 200
    assert "来源 [1]" in r.text
    assert "引用来源" in r.text
    assert "未接入模型" in r.text


def test_api_ask_json(client):
    c, *_ = client
    r = c.get("/api/ask", params={"q": "门票多少", "top_k": 2})
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "fallback"
    assert data["badge"] == "未接入模型·本地摘录"
    assert isinstance(data["citations"], list) and data["citations"]
    cit = data["citations"][0]
    for k in ("index", "chunk_id", "doc_id", "doc_title", "url", "quote", "score"):
        assert k in cit


def test_api_status_exposes_llm_mode(client):
    c, *_ = client
    d = c.get("/api/status").json()
    assert d["llm"]["configured"] is False
    assert d["llm"]["badge"] == "未接入模型·本地摘录"
    assert d["index_meta"]["total_vectors"] >= 1
    assert d["stats"]["spots"] == 1


def test_review_page_lists_queue_items(client):
    c, sid, *_ = client
    r = c.get("/review")
    assert r.status_code == 200
    assert "甲峰景区" in r.text
    assert "门票价格" in r.text
    assert "conflict" in r.text or "low_confidence" in r.text


def test_healthz(client):
    c, *_ = client
    assert c.get("/healthz").json() == {"ok": True}


def test_404_page_renders(client):
    c, *_ = client
    r = c.get("/no-such-page")
    assert r.status_code == 404
    assert "页面不存在" in r.text


def test_no_empty_route_all_pages_have_body(client):
    """所有页面都必须渲染出实质内容，不能是空壳。"""
    c, sid, d1, _ = client
    for path in ("/", "/spots", f"/spot/{sid}", f"/doc/{d1}", "/ask", "/review"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert len(r.text) > 1500, f"{path} 页面内容过少，疑似空壳"
