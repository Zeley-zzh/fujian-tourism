"""utils 单测：URL 归一化、清洗、哈希、日期区间。"""

from __future__ import annotations

from crawler.utils import (
    cjk_ratio,
    clean_text,
    collapse_for_hash,
    content_hash,
    in_effective_range,
    normalize_url,
    parse_date,
    truncate,
    url_hash,
)


def test_normalize_url_strips_tracking_and_sorts_query():
    a = normalize_url("https://Example.GOV.cn/a/b.html?b=2&a=1&utm_source=x#frag")
    b = normalize_url("https://example.gov.cn/a/b.html?a=1&b=2")
    assert a == b
    assert "utm_source" not in a
    assert "#" not in a


def test_normalize_url_keeps_meaningful_query():
    u = normalize_url("https://x.gov.cn/list.html?page=3")
    assert "page=3" in u


def test_url_hash_stable_across_equivalent_urls():
    assert url_hash("https://a.gov.cn/x?utm_medium=y") == url_hash("https://a.gov.cn/x")


def test_content_hash_ignores_whitespace():
    assert content_hash("景区  公告\n内容") == content_hash("景区公告内容")


def test_clean_text_collapses_blank_and_nbsp():
    assert clean_text("a\u3000\u00a0 b\n\n\nc  ") == "a b\n\nc"


def test_collapse_for_hash_drops_all_whitespace():
    assert collapse_for_hash(" a b\nc ") == "abc"


def test_parse_date_supports_chinese_and_dash():
    assert parse_date("2026年3月20日") == "2026-03-20"
    assert parse_date("2026-03-20") == "2026-03-20"
    assert parse_date("2026/3/20") == "2026-03-20"
    assert parse_date("") is None
    assert parse_date("无日期") is None


def test_in_effective_range():
    # 区间为闭区间；None 表示该侧无界
    assert in_effective_range("2020-01-01", "2030-01-01") is True
    assert in_effective_range(None, None) is True
    assert in_effective_range("2020-01-01", None) is True
    assert in_effective_range(None, "1999-01-01") is False


def test_truncate_lands_on_punctuation_and_respects_limit():
    text = "第一句话在这里结束。第二句很长很长" * 20
    out = truncate(text, 40)
    assert len(out) <= 40
    assert out.endswith(("。", "；", "！", "？", "，")) or len(out) == 40


def test_cjk_ratio():
    assert cjk_ratio("纯中文内容") == 1.0
    assert cjk_ratio("abcdef") == 0.0
