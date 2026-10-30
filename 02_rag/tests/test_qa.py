"""检索、引用归一化、字段聚合（三态与冲突并排）。"""
from __future__ import annotations

import pytest

from app.embeddings import Embedder
from app.ingest import build_index
from app.llm import LLMClient
from app.qa import FIELD_LABELS, QAService, group_fields
from app.store import Retriever, TourismDB

from conftest import seed


@pytest.fixture()
def ready(tmp_path, schema_path, monkeypatch):
    """一个索引就绪的小库：两篇文档、两个景区字段、一处冲突。"""
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "faiss"))
    from crawler.db import Database
    d = Database(tmp_path / "r.db")
    d.init_schema()

    d1, _ = seed(d, doc_id="d0001", url="https://example.gov.cn/1.html",
                 title="甲峰景区门票公告",
                 text="甲峰景区开放时间：08:00—17:30。门票价格：65 元。",
                 publish_date="2026-03-20")
    d2, _ = seed(d, doc_id="d0002", url="https://example.gov.cn/2.html",
                 title="甲峰景区调价通知",
                 text="甲峰景区开放时间：08:30—17:00。门票价格：80 元。",
                 publish_date="2026-05-25")
    d.conn.commit()

    sid = d.upsert_spot(name="甲峰景区", city="福州市", level="5A")
    d.add_provenance(spot_id=sid, field_name="ticket_price", field_value="65 元",
                     source_doc_id=d1, source_url="https://example.gov.cn/1.html",
                     doc_title="甲峰景区门票公告", issuer="省文化和旅游厅",
                     publish_date="2026-03-20", effective_from="2026-04-01",
                     effective_to="2028-03-31", quote="门票价格：65 元。", status="已核验")
    d.add_provenance(spot_id=sid, field_name="ticket_price", field_value="80 元",
                     source_doc_id=d2, source_url="https://example.gov.cn/2.html",
                     doc_title="甲峰景区调价通知", issuer="省文化和旅游厅",
                     publish_date="2026-05-25", effective_from="2026-06-01",
                     effective_to=None, quote="门票价格：80 元。", status="待核验")
    d.add_provenance(spot_id=sid, field_name="phone", field_value="0591-00000000",
                     source_doc_id=d1, source_url="https://example.gov.cn/1.html",
                     doc_title="甲峰景区门票公告", issuer="省文化和旅游厅",
                     publish_date="2026-03-20", effective_from=None, effective_to=None,
                     quote="咨询电话：0591-00000000。", status="已核验")
    d.close()

    db = TourismDB(tmp_path / "r.db")
    build_index(db, Embedder(backend="hash"), force=True)
    yield db, d1, d2, sid
    db.close()


# ------------------------------------------------------------------ 检索
def test_search_returns_hits_with_metadata(ready):
    db, d1, d2, sid = ready
    r = Retriever(db)
    hits = r.search("甲峰景区门票多少钱", top_k=2)
    assert hits, "必须能召回到文档"
    top = hits[0]
    for k in ("chunk_id", "doc_id", "text", "doc_title", "url", "score",
              "vec_score", "kw_score", "publish_date"):
        assert k in top
    assert top["score"] >= top["vec_score"] - 1e-6
    assert 0.0 <= top["score"] <= 2.0


def test_search_prefers_literal_match(ready):
    """专有名词命中要能压过泛化相似：问「甲峰」时不该先给出无关文本。"""
    db, *_ = ready
    hits = Retriever(db).search("甲峰景区门票价格", top_k=2)
    assert all("甲峰景区" in h["text"] for h in hits[:1])


def test_search_top_k_respected(ready):
    db, *_ = ready
    assert len(Retriever(db).search("景区", top_k=1)) <= 1


def test_search_no_index_returns_empty(tmp_path, schema_path, monkeypatch):
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "none"))
    from crawler.db import Database
    d = Database(tmp_path / "n.db"); d.init_schema()
    seed(d); d.close()
    db = TourismDB(tmp_path / "n.db")
    assert Retriever(db).search("任意", top_k=3) == []
    db.close()


# ------------------------------------------------------------------ 问答
def test_ask_returns_citations_and_fallback_badge(ready):
    db, *_ = ready
    svc = QAService(db, llm=LLMClient())
    res = svc.ask("甲峰景区门票多少钱？", top_k=2)
    assert res.mode == "fallback"
    assert res.badge == "未接入模型·本地摘录"
    assert res.citations, "必须给出引用"
    c = res.citations[0]
    assert c.index == 1
    assert c.chunk_id.startswith("chunk_")
    assert c.url.startswith("https://")
    assert len(c.quote) <= 200, "摘录必须截断到 200 字以内"
    assert res.used_chunks == len(res.citations)


def test_ask_empty_question(ready):
    db, *_ = ready
    res = QAService(db, llm=LLMClient()).ask("   ")
    assert res.citations == []
    assert "请输入问题" in res.answer


def test_sanitize_refs_drops_out_of_range():
    assert QAService._sanitize_refs("结论 A[1]，结论 B[9]。", 2) == "结论 A[1]，结论 B。"
    assert QAService._sanitize_refs("[1][2][3]", 2) == "[1][2]"
    assert QAService._sanitize_refs("无引用", 3) == "无引用"


def test_citation_quote_truncated_to_200():
    ctx = [{"chunk_id": "c", "doc_id": "d", "text": "长" * 900,
            "doc_title": "t", "url": "u", "score": 1.0}]
    cites = QAService._build_citations(ctx)
    assert len(cites[0].quote) == 200


def test_ask_answer_has_no_dangling_number_refs(ready):
    db, *_ = ready
    res = QAService(db, llm=LLMClient()).ask("门票价格", top_k=1)
    import re
    for m in re.finditer(r"\[(\d+)\]", res.answer):
        assert 1 <= int(m.group(1)) <= len(res.citations)


# ------------------------------------------------------------------ 字段聚合
def _entry(field, value, status="已核验", group=None, fetched="2026-06-01"):
    return {"field_name": field, "field_value": value, "status": status,
            "source_doc_id": "d1", "source_url": "https://x", "doc_title": "T",
            "issuer": "I", "publish_date": "2026-03-20", "effective_from": None,
            "effective_to": None, "quote": "q", "fetched_at": fetched,
            "conflict_group": group}


def test_group_fields_marks_conflict():
    items = [_entry("ticket_price", "65 元", "冲突", "g1"),
             _entry("ticket_price", "80 元", "冲突", "g1")]
    g = group_fields(items)[0]
    assert g["status"] == "冲突"
    assert g["status_class"] == "conflict"
    assert g["is_conflict"] is True
    assert len(g["values"]) == 2
    assert g["label"] == "门票价格"


def test_group_fields_verified_wins_as_current():
    items = [_entry("ticket_price", "65 元", "已核验", fetched="2026-01-01"),
             _entry("ticket_price", "80 元", "待核验", fetched="2026-09-01")]
    g = group_fields(items)[0]
    assert g["current"] == "65 元", "已核验优先作为当前值，即使抓取更早"
    assert g["status"] == "待核验"


def test_group_fields_all_verified():
    g = group_fields([_entry("phone", "0591-00000000")])[0]
    assert g["status"] == "已核验"
    assert g["is_conflict"] is False
    assert g["label"] == "咨询电话"


def test_group_fields_missing_quote_is_pending():
    e = _entry("address", "湖东路 1 号", "待核验")
    e["quote"] = None
    g = group_fields([e])[0]
    assert g["status"] == "待核验"
    assert g["values"][0]["quote"] is None


def test_group_fields_sorted_by_field_order():
    items = [_entry("phone", "1"), _entry("opening_hours", "2"), _entry("ticket_price", "3")]
    order = [g["field"] for g in group_fields(items)]
    assert order == ["opening_hours", "ticket_price", "phone"]


def test_field_labels_cover_six_volatile_fields():
    # 六个易变字段是硬约束要求必须能展示的，名录类公告另外产出 category 与 level。
    assert {"opening_hours", "ticket_price", "policy_validity",
            "address", "booking_method", "phone"} <= set(FIELD_LABELS)
    assert {"category", "level"} <= set(FIELD_LABELS)
    # 顺序即页面展示顺序：名录只给得出类别与等级，放最前。
    assert list(FIELD_LABELS)[:2] == ["category", "level"]


def test_db_conflicts_groups_two_values(ready):
    db, *_ = ready
    cs = db.conflicts()
    assert cs, "冲突组必须能被检出"
    c = cs[0]
    assert c["field_name"] == "ticket_price"
    assert set(c["values"]) == {"65 元", "80 元"}
    assert len(c["items"]) == 2
    assert c["items"][0]["conflict_group"] == c["items"][1]["conflict_group"]
