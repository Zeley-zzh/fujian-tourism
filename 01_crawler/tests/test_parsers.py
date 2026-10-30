"""解析器单测：从固定 HTML 里抽字段，不联网。"""

from __future__ import annotations

from crawler.parsers import get_parser
from crawler.parsers.gov_notice import GovNoticeParser
from crawler.parsers.scenic_list import ScenicListParser

SITE_CFG = {
    "key": "t",
    "base_url": "https://x.gov.cn",
    "list_selector": {"item": "ul.list li", "link": "a", "date": "span.date"},
    "detail_selector": {
        "title": "h1.title",
        "body": "div.article-content",
        "publish_date": "div.meta .date",
    },
    "parser": "gov_notice",
}


def test_parse_list_extracts_items_and_resolves_relative_urls():
    html = """<ul class="list">
      <li><a href="/zwgk/content_1.html">关于甲峰景区门票的通知</a>
          <span class="date">2026-03-20</span></li>
      <li><a href="/zwgk/content_2.html">关于乙湖景区的公告</a>
          <span class="date">2026-05-25</span></li>
    </ul>"""
    p = GovNoticeParser(SITE_CFG)
    items = p.parse_list(html, "https://x.gov.cn/zwgk/index.html")
    assert len(items) == 2
    assert items[0]["url"] == "https://x.gov.cn/zwgk/content_1.html"
    assert items[0]["date"] == "2026-03-20"
    assert "甲峰景区" in items[0]["title"]


def test_parse_detail_extracts_meta_body_and_dates():
    html = """<html><body>
      <div class="meta"><span class="issuer">省文化和旅游厅</span>
        <span class="date">2026年3月20日</span>
        <span class="docno">文旅测〔2026〕1号</span></div>
      <h1 class="title">甲峰景区门票与开放时间公告</h1>
      <div class="article-content">
        <p>开放时间：08:00—17:30。门票价格：65 元。</p>
        <p>自2026年4月1日起执行，有效期至2028年3月31日。</p>
      </div></body></html>"""
    p = GovNoticeParser(SITE_CFG)
    rec = p.parse_detail(html, "https://x.gov.cn/zwgk/content_1.html")
    assert rec.title == "甲峰景区门票与开放时间公告"
    assert rec.publish_date == "2026-03-20"
    assert rec.doc_no == "文旅测〔2026〕1号"
    assert "65 元" in rec.content_text
    assert rec.effective_from == "2026-04-01"
    assert rec.effective_to == "2028-03-31"
    assert rec.is_useful()


def test_parse_detail_strips_nav_and_script():
    html = """<html><body><nav>首页 返回</nav>
      <h1 class="title">正文标题</h1>
      <div class="article-content"><p>真正的正文内容，足够长以满足有效判断。</p>
      <script>var a=1;</script></div></body></html>"""
    rec = GovNoticeParser(SITE_CFG).parse_detail(html, "https://x.gov.cn/a.html")
    assert "var a=1" not in rec.content_text
    assert "真正的正文" in rec.content_text


def test_parse_detail_marks_useless_when_body_too_short():
    html = '<h1 class="title">短</h1><div class="article-content"><p>太短</p></div>'
    rec = GovNoticeParser(SITE_CFG).parse_detail(html, "https://x.gov.cn/a.html")
    assert rec.is_useful() is False


def test_scenic_list_parses_table_and_level_and_city():
    html = """<table class="data">
      <tr><th>序号</th><th>名称</th><th>等级</th><th>城市</th></tr>
      <tr><td>1</td><td>甲峰景区</td><td>5A</td><td>福州</td></tr>
      <tr><td>2</td><td>乙湖景区</td><td>AAAA</td><td>厦门</td></tr>
    </table>"""
    cfg = dict(SITE_CFG, parser="scenic_list",
               list_selector={"item": "table.data tr", "link": "a", "date": "td:nth-child(4)"})
    items = ScenicListParser(cfg).parse_list(html, "https://x.gov.cn/ml/index.html")
    assert len(items) == 2
    assert items[0]["title"] == "甲峰景区"


def test_scenic_detail_detects_level_and_city():
    html = """<h1 class="title">甲峰景区</h1>
      <div class="article-content">
        <p>甲峰景区为国家 5A 级旅游景区，位于福建省福州市。</p>
        <p>地址：福建省福州市湖东路 1 号。</p>
      </div>"""
    cfg = dict(SITE_CFG, parser="scenic_list")
    rec = ScenicListParser(cfg).parse_detail(html, "https://x.gov.cn/a.html")
    assert rec.extra.get("level") == "5A"
    assert rec.extra.get("city") == "福州"


def test_parser_registry_falls_back_to_gov_notice():
    assert isinstance(get_parser("gov_notice", SITE_CFG), GovNoticeParser)
    assert isinstance(get_parser("scenic_list", SITE_CFG), ScenicListParser)
    # 未知名字不炸，退到默认解析器
    assert isinstance(get_parser("nonexistent", SITE_CFG), GovNoticeParser)
