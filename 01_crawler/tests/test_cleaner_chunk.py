"""清洗与切片单测：正文清洗、字段抽取、三态判定、chunk 切法。

签名注意：`Cleaner.normalize_body(text, title)` 是位置参数，
`chunk_text(text, *, doc_id, doc_title, publish_date)` 是 keyword-only 的实例方法。
"""

from __future__ import annotations

from crawler.pipeline.cleaner import Cleaner
from crawler.utils import content_hash


def _cleaner(settings, db) -> Cleaner:
    return Cleaner(settings, db)


def test_normalize_body_drops_nav_and_footer(settings, db):
    c = _cleaner(settings, db)
    raw = """当前位置：首页 > 政务公开
首页 返回 打印
本景区开放时间为 08:00—17:30，门票 65 元。
主办单位：省文化和旅游厅
ICP备00000000号
1/3
"""
    out = c.normalize_body(raw, "")
    assert "开放时间" in out
    assert "当前位置" not in out
    assert "ICP备" not in out
    assert "主办单位" not in out
    assert "1/3" not in out


def test_normalize_body_drops_repeated_title_line(settings, db):
    c = _cleaner(settings, db)
    title = "甲峰景区门票公告"
    out = c.normalize_body(title + "\n正文第一段内容足够长。\n正文第二段。", title)
    assert out.startswith("正文第一段") or title not in out.split("\n")[0]


def test_extract_field_uses_regex_and_cuts_sentence(settings, db):
    c = _cleaner(settings, db)
    cfg = settings.scenic_fields
    text = "甲峰景区开放时间：08:00—17:30。门票价格：65 元。地址：福建省福州市湖东路 1 号。"

    v1, q1 = c._extract_field(text, cfg["opening_hours"])
    assert v1 == "08:00—17:30"
    assert "开放时间" in q1

    v2, _ = c._extract_field(text, cfg["ticket_price"])
    assert v2 == "65 元"

    v3, _ = c._extract_field(text, cfg["address"])
    # 值必须在本句结束，不能吞掉后文
    assert v3 == "福建省福州市湖东路 1 号"


def test_extract_field_strips_label_prefix(settings, db):
    """关键词命中长标签前半截时，残留的「方式：」前缀必须剥掉。"""
    c = _cleaner(settings, db)
    text = "预约方式：官方小程序实名预约。咨询电话：0591-00000000。"
    v, _ = c._extract_field(text, settings.scenic_fields["booking_method"])
    assert v == "官方小程序实名预约"


def test_judge_status_three_states(settings, db):
    c = _cleaner(settings, db)
    ok = dict(quote="门票 65 元。", source_url="https://x.gov.cn/a",
              publish_date="2026-03-20", eff_from=None, eff_to="2099-12-31")
    assert c._judge_status(**ok) == "已核验"
    assert c._judge_status(**{**ok, "publish_date": None}) == "待核验"
    assert c._judge_status(**{**ok, "quote": ""}) == "待核验"
    assert c._judge_status(**{**ok, "eff_to": "2000-01-01"}) == "待核验"


def test_chunk_text_ids_and_limits(settings, db):
    c = _cleaner(settings, db)
    body = "第一段内容。" * 30 + "\n\n" + "第二段内容。" * 30 + "\n\n" + "第三段内容。" * 30
    chunks = c.chunk_text(body, doc_id="d0007", doc_title="测试标题", publish_date="2026-03-20")
    assert chunks, "长正文必须切片"
    ids = [ch["chunk_id"] for ch in chunks]
    assert ids[0] == "chunk_d0007_0000"
    assert all(cid.startswith("chunk_d0007_") for cid in ids)
    assert len(ids) == len(set(ids)), "chunk_id 不能重复"
    assert all(len(ch["text"]) <= settings.chunking.chunk_size + 120 for ch in chunks)
    assert all(ch["token_estimate"] > 0 for ch in chunks)


def test_chunk_text_short_body_single_chunk(settings, db):
    c = _cleaner(settings, db)
    chunks = c.chunk_text("很短的一段正文，仅用于验证短文不硬切。",
                          doc_id="d0001", doc_title="t", publish_date=None)
    assert len(chunks) == 1
    assert chunks[0]["chunk_id"] == "chunk_d0001_0000"


def test_chunk_text_splits_on_subheading_and_records_path(settings, db):
    c = _cleaner(settings, db)
    body = (
        "一、适用范围\n本通知适用于甲峰景区，全文内容足够长以避免被并入相邻块。\n\n"
        "二、票价标准\n甲峰景区门票价格为 65 元，优惠政策另行公布，此处内容足够长。\n\n"
        "三、执行时间\n本通知自2026年4月1日起执行，有效期至2028年3月31日。\n"
    )
    chunks = c.chunk_text(body, doc_id="d0002", doc_title="测试通知", publish_date="2026-03-20")
    assert any(ch.get("heading_path") for ch in chunks), "按小标题切分后应记录 heading_path"


def test_near_duplicate_key_ignores_whitespace(settings, db):
    """两篇只差排版空白的文档应被判为近重复（同一粗指纹桶）。"""
    body = "甲峰景区门票价格 65 元。开放时间 08:00—17:30。"
    # 与 Cleaner.run 里的判定口径一致：去空白后的前 500 字指纹
    assert content_hash(body[:500]) == content_hash(body.replace("。", "。\n")[:500])
