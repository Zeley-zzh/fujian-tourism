"""政务公告/政策文件解析器（省级文旅厅通用形态）。

政务站结构大同小异：列表页 ul > li > a + span.date，详情页 h1.title + div.article-content。
所以这个解析器只依赖配置里的 selector，不写死任何站点。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..utils import clean_text, normalize_url, parse_date
from .base import BaseParser, DocRecord


class GovNoticeParser(BaseParser):
    name = "gov_notice"

    # 发文单位常见写法
    ISSUER_PATTERNS = [
        r"发文单位[：:]\s*([^\s<｜|]{2,30})",
        r"来源[：:]\s*([^\s<｜|]{2,30})",
        r"发布机构[：:]\s*([^\s<｜|]{2,30})",
    ]
    DOCNO_PATTERNS = [
        r"([\u4e00-\u9fa5]{1,10}[〔\[［(（]\d{4}[〕\]］)）]\d{1,4}\s*号)",   # 闽文旅〔2026〕12号
        r"文号[：:]\s*([^\s<]{4,40})",
    ]
    EFFECTIVE_PATTERNS = [
        r"自\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*起(?:施行|执行|实施|生效)",
        r"自\s*(\d{4}-\d{1,2}-\d{1,2})\s*起(?:施行|执行|实施|生效)",
        r"有效期(?:自|至)?[：:]?\s*(\d{4}[\-年]\d{1,2}[\-月]\d{1,2})",
    ]
    EFFECTIVE_TO_PATTERNS = [
        r"有效期(?:至|到)[：:]?\s*(\d{4}[\-年]\d{1,2}[\-月]\d{1,2})",
        r"至\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*(?:止|截止)",
    ]

    def parse_list(self, html: str, page_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        ls = self.cfg.get("list_selector") or {}
        item_sel = ls.get("item") or "ul.list li"
        link_sel = ls.get("link") or "a"
        date_sel = ls.get("date") or "span.date"

        items: list[dict] = []
        seen: set[str] = set()
        for li in soup.select(item_sel):
            a = li.select_one(link_sel)
            if not a or not a.get("href"):
                continue
            href = a["href"].strip()
            if href.startswith(("javascript:", "#", "mailto:")):
                continue
            url = normalize_url(urljoin(page_url, href))
            if url in seen:
                continue
            seen.add(url)
            title = self.text_of(a) or a.get("title", "")
            d = li.select_one(date_sel)
            date_text = self.text_of(d) if d else self.text_of(li)
            items.append({
                "url": url,
                "title": title,
                "date": parse_date(date_text),
            })
        return items

    def parse_detail(self, html: str, url: str) -> DocRecord:
        soup = BeautifulSoup(html, "lxml")

        # 标题：优先配置 selector，退回 h1 / title
        title = ""
        if self.sel.get("title"):
            title = self.text_of(soup.select_one(self.sel["title"]))
        if not title:
            for cand in ("h1", "h1.article-title", ".article-title", "title"):
                node = soup.select_one(cand)
                if node and self.text_of(node):
                    title = self.text_of(node)
                    break
        title = re.sub(r"\s*[-—_|｜]\s*.*$", "", title).strip() if len(title) > 30 else title.strip()

        # 正文
        body_node = None
        if self.sel.get("body"):
            body_node = soup.select_one(self.sel["body"])
        if body_node is None:
            for cand in ("div.article-content", "div.content", "div.TRS_Editor",
                         "div.article", "#content", "article"):
                body_node = soup.select_one(cand)
                if body_node:
                    break
        if body_node is None:
            body_node = soup.body or soup

        # 去掉脚本/样式/导航，避免把菜单文本混进正文
        for junk in body_node.select("script, style, nav, .share, .related, .pagination, .footer"):
            junk.decompose()

        content_text = clean_text(body_node.get_text("\n", strip=True))
        content_html = str(body_node)

        # 元信息：从整页文本里找，因为很多站把发文单位放在正文外的 meta 区
        page_text = clean_text(soup.get_text("\n", strip=True))
        issuer = None
        if self.sel.get("issuer"):
            issuer = self.text_of(soup.select_one(self.sel["issuer"])) or None
        if not issuer:
            issuer = self.first_match(self.ISSUER_PATTERNS, page_text)

        doc_no = None
        if self.sel.get("doc_no"):
            doc_no = self.text_of(soup.select_one(self.sel["doc_no"])) or None
        if not doc_no:
            doc_no = self.first_match(self.DOCNO_PATTERNS, page_text)

        publish_date = None
        if self.sel.get("publish_date"):
            publish_date = parse_date(self.text_of(soup.select_one(self.sel["publish_date"])))
        if not publish_date:
            publish_date = parse_date(self.first_match(
                [r"发布时间[：:]\s*([\d年月日\-/]{8,12})",
                 r"日期[：:]\s*([\d年月日\-/]{8,12})",
                 r"(\d{4}-\d{2}-\d{2})"], page_text))

        eff_from = parse_date(self.first_match(self.EFFECTIVE_PATTERNS, content_text))
        eff_to = parse_date(self.first_match(self.EFFECTIVE_TO_PATTERNS, content_text))

        # 附件
        atts: list[str] = []
        att_sel = self.cfg.get("attachment_selector")
        if att_sel:
            for a in soup.select(att_sel):
                if a.get("href"):
                    atts.append(urljoin(url, a["href"]))

        return DocRecord(
            url=url,
            title=title or "[无标题]",
            content_text=content_text,
            content_html=content_html,
            doc_type=self.cfg.get("doc_type", "notice"),
            issuer=issuer,
            doc_no=doc_no,
            publish_date=publish_date,
            effective_from=eff_from,
            effective_to=eff_to,
            attachments=list(dict.fromkeys(atts)),
        )
