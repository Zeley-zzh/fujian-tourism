"""数据库层单测：版本演进、冲突保留、三态、审阅队列。

注意：Database 的方法多为 keyword-only，调用时写全参数名。
"""

from __future__ import annotations

from crawler.parsers.base import DocRecord


def _rec(url: str, title: str, text: str, publish: str | None = "2026-03-20") -> DocRecord:
    return DocRecord(url=url, title=title, content_text=text,
                     publish_date=publish, doc_type="policy")


def _insert_doc(db, doc_id: str, url: str):
    db.conn.execute(
        "INSERT INTO documents(doc_id,url,url_hash,doc_type,title,content_text,"
        "content_hash,fetch_status,first_seen_at,last_fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (doc_id, url, f"h_{doc_id}", "policy", "t", "body", f"c_{doc_id}", "ok", "x", "x"),
    )
    db.conn.commit()


def test_upsert_document_new_then_unchanged(db):
    rec = _rec("https://x.gov.cn/a.html", "标题", "正文内容足够长以免被判无效。")
    doc_id, state = db.upsert_document(rec)
    assert state == "new"
    assert doc_id == "d0001"

    doc_id2, state2 = db.upsert_document(rec)
    assert state2 == "unchanged"
    assert doc_id2 == doc_id


def test_upsert_document_bumps_version_on_content_change(db):
    rec = _rec("https://x.gov.cn/a.html", "标题", "旧正文：票价 65 元。")
    doc_id, _ = db.upsert_document(rec)

    changed = _rec("https://x.gov.cn/a.html", "标题", "新正文：票价 80 元。")
    doc_id2, state = db.upsert_document(changed)
    assert state == "updated"
    assert doc_id2 == doc_id

    row = db.conn.execute(
        "SELECT current_version FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    assert row["current_version"] == 2

    versions = db.conn.execute(
        "SELECT version_no, diff_summary FROM doc_versions WHERE doc_id=? ORDER BY version_no",
        (doc_id,),
    ).fetchall()
    assert len(versions) == 2
    # 变更对比必须给出「新增/删除」，不能只是一句空话
    assert "80" in versions[-1]["diff_summary"] or "65" in versions[-1]["diff_summary"]


def test_add_provenance_dedupes_same_value_same_doc(db):
    spot = db.upsert_spot(name="甲峰景区")
    _insert_doc(db, "d0001", "https://x.gov.cn/a.html")
    kw = dict(spot_id=spot, field_name="ticket_price", field_value="65 元",
              source_doc_id="d0001", source_url="https://x.gov.cn/a.html",
              doc_title="t", issuer="i", publish_date="2026-03-20",
              effective_from=None, effective_to=None, quote="门票价格：65 元。",
              status="已核验")
    p1 = db.add_provenance(**kw)
    p2 = db.add_provenance(**kw)
    assert p1 == p2
    n = db.conn.execute("SELECT COUNT(*) c FROM field_provenance").fetchone()["c"]
    assert n == 1


def test_add_provenance_conflict_keeps_both_values_and_flags_both(db):
    spot = db.upsert_spot(name="甲峰景区")
    _insert_doc(db, "d0001", "https://x.gov.cn/a.html")
    _insert_doc(db, "d0002", "https://x.gov.cn/b.html")
    base = dict(spot_id=spot, field_name="ticket_price",
                source_url="https://x.gov.cn/a.html", doc_title="t", issuer="i",
                publish_date="2026-03-20", effective_from=None, effective_to=None,
                status="已核验")
    db.add_provenance(field_value="65 元", source_doc_id="d0001",
                      quote="门票价格：65 元。", **base)
    db.add_provenance(field_value="80 元", source_doc_id="d0002",
                      quote="门票价格：80 元。", **base)

    rows = db.conn.execute(
        "SELECT field_value, status, conflict_group, is_current FROM field_provenance "
        "WHERE spot_id=? AND field_name='ticket_price' ORDER BY prov_id", (spot,)
    ).fetchall()
    # 旧值不许被静默丢弃
    assert [r["field_value"] for r in rows] == ["65 元", "80 元"]
    assert all(r["status"] == "冲突" for r in rows)
    assert rows[0]["conflict_group"] == rows[1]["conflict_group"]
    assert rows[0]["conflict_group"]

    queue = db.conn.execute("SELECT reason, severity FROM review_queue").fetchall()
    assert any(r["reason"] == "conflict" for r in queue)


def test_provenance_has_all_trace_columns(db):
    """溯源元数据完整性：这 10 个字段是硬约束，列必须存在。"""
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(field_provenance)")}
    for c in ("source_doc_id", "source_url", "doc_title", "issuer", "publish_date",
              "effective_from", "effective_to", "quote", "fetched_at", "status"):
        assert c in cols


def test_replace_chunks_is_idempotent(db):
    _insert_doc(db, "d0001", "https://x.gov.cn/a.html")
    chunks = [
        {"chunk_id": "chunk_d0001_0000", "chunk_index": 0, "text": "第一片"},
        {"chunk_id": "chunk_d0001_0001", "chunk_index": 1, "text": "第二片"},
    ]
    db.replace_chunks("d0001", chunks)
    db.replace_chunks("d0001", chunks)
    n = db.conn.execute("SELECT COUNT(*) c FROM chunks WHERE doc_id='d0001'").fetchone()["c"]
    assert n == 2


def test_chunk_id_format_matches_contract(db):
    """CONTRACT.md 规定 chunk_id = chunk_{doc_id}_{index:04d}。"""
    _insert_doc(db, "d0007", "https://x.gov.cn/b.html")
    db.replace_chunks("d0007", [{"chunk_id": "chunk_d0007_0000", "chunk_index": 0, "text": "x"}])
    cid = db.conn.execute("SELECT chunk_id FROM chunks").fetchone()["chunk_id"]
    assert cid == "chunk_d0007_0000"


def test_stats_shape(db):
    s = db.stats()
    for key in ("documents", "spots", "provenance", "verified", "pending",
                "conflicts", "chunks", "open_review"):
        assert key in s
